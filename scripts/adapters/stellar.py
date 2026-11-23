"""Stellar adapter for classic assets (code + issuer), through Horizon. Soroban custom tokens are out of the MVP.

Horizon cannot list the movements of one asset across the network, and stellar.expert's API became paid
(HTTP 402, 2026-11-23). So the history is rebuilt from the **effects of the accounts that can hold the asset**:

- accounts with a trustline to the asset that is authorized or holds a balance (for an issuer with
  `auth_required`, an unauthorized trustline can never have received the asset); without `auth_required`,
  every trustline account;
- plus every account met in the issuer's operations (payments, authorizations, clawbacks, SAC balance changes).

Each account's effects are read from the issuer's creation ledger (nothing can predate it), or from the resume
cursor. Credits, debits, DEX trades and liquidity pool deposits/withdrawals of the asset become balance changes:
an increase is a mint to the account, a decrease a burn from it, so the reconstructed supply is the sum of account
balances. Every account balance is then checked against Horizon (adapter.onchain_balances_raw).

Limitation (SPEC §13 R5): an account that held the asset, then lost its authorization and its trustline
authorization, is not discovered. The per-account check cannot see it either since its balance is now 0.
"""

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from scripts.lib import chains, http

from .base import Adapter, Transfer

SCALE_DECIMALS = 7
PAGE_LIMIT = 200
MAX_PAGES_PER_ACCOUNT = 5_000   # 1,000,000 effects: beyond this an account is too busy for Horizon paging


class StellarError(RuntimeError):
    pass


def to_raw(value: str) -> int:
    return int(Decimal(value).scaleb(SCALE_DECIMALS).to_integral_value(rounding=ROUND_HALF_EVEN))


def token_key(token: str) -> tuple[int, int]:
    """Sort key of an effect paging token "<operation id>-<index>" (operation ids grow with the ledger)."""
    operation, _, index = token.partition("-")
    return int(operation), int(index or 0)


class StellarAdapter(Adapter):
    family = "stellar"

    def __init__(self, line: dict) -> None:
        super().__init__(line)
        self.code = line["identifier"]["code"]
        self.issuer = line["identifier"]["issuer"]
        self.horizons = chains.get_chain(self.chain_id)["stellar"]["horizon_urls"]
        self._head: int | None = None
        self._trustlines: dict[str, dict] | None = None
        self.stats = {"accounts": 0, "pages": 0, "effects": 0, "asset_effects": 0}

    # ── Adapter interface ────────────────────────────────────────────────────

    def decimals(self) -> int:
        return SCALE_DECIMALS

    def start_position(self) -> dict:
        return {"token": None}

    def non_holder_addresses(self) -> set[str]:
        return {self.issuer}

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        self._head = int(self._get("/")["history_latest_ledger"])
        cursor = position.get("token") or str(self._issuer_creation_ledger() << 32)
        self._trustlines = self._load_trustlines()
        accounts = self._accounts_to_scan()
        self.stats["accounts"] = len(accounts)
        changes: list[tuple[tuple[int, int], str, str, str, int]] = []
        for account in sorted(accounts):
            for token, day, delta in self._account_changes(account, cursor):
                changes.append((token_key(token), token, day, account, delta))
        changes.sort()
        transfers = []
        for _, token, day, account, delta in changes:
            cursor_after = {"token": token}
            if delta > 0:
                transfers.append(Transfer(day, None, account, delta, cursor_after))
            else:
                transfers.append(Transfer(day, account, None, -delta, cursor_after))
        return transfers

    def onchain_supply_raw(self) -> int | None:
        records = self._get("/assets", {"asset_code": self.code, "asset_issuer": self.issuer})["_embedded"]["records"]
        if not records:
            return 0
        balances = records[0].get("balances") or {}
        return sum(to_raw(balances.get(k, "0")) for k in
                   ("authorized", "authorized_to_maintain_liabilities", "unauthorized"))

    def onchain_balances_raw(self) -> dict[str, int] | None:
        trustlines = self._load_trustlines()
        return {account: to_raw(t["balance"]) for account, t in trustlines.items() if to_raw(t["balance"])}

    # ── Discovery ────────────────────────────────────────────────────────────

    def _issuer_creation_ledger(self) -> int:
        first = self._get(f"/accounts/{self.issuer}/operations", {"order": "asc", "limit": 1})
        records = first["_embedded"]["records"]
        if not records:
            raise StellarError(f"issuer {self.issuer} has no operation: check the issuer address")
        return int(records[0]["paging_token"]) >> 32

    def _load_trustlines(self) -> dict[str, dict]:
        trustlines: dict[str, dict] = {}
        for record in self._pages("/accounts", {"asset": f"{self.code}:{self.issuer}", "limit": PAGE_LIMIT}):
            for balance in record.get("balances", []):
                if balance.get("asset_code") == self.code and balance.get("asset_issuer") == self.issuer:
                    trustlines[record["id"]] = balance
        return trustlines

    def _accounts_to_scan(self) -> set[str]:
        flags = self._get(f"/accounts/{self.issuer}").get("flags", {})
        accounts = set()
        for account, trustline in (self._trustlines or {}).items():
            can_hold = (trustline.get("is_authorized") or trustline.get("is_authorized_to_maintain_liabilities")
                        or to_raw(trustline["balance"]) > 0)
            if can_hold or not flags.get("auth_required"):
                accounts.add(account)
        for operation in self._pages(f"/accounts/{self.issuer}/operations", {"order": "asc", "limit": PAGE_LIMIT}):
            accounts |= self._accounts_in_operation(operation)
        accounts.discard(self.issuer)
        return {a for a in accounts if a and a.startswith("G")}

    def _accounts_in_operation(self, op: dict) -> set[str]:
        found = set()
        if self._is_asset(op, "asset"):
            found |= {op.get("from"), op.get("to"), op.get("trustor")}
        if op.get("type") in ("set_trust_line_flags", "allow_trust") and op.get("asset_code") == self.code:
            found.add(op.get("trustor"))
        for change in op.get("asset_balance_changes") or []:
            if change.get("asset_code") == self.code and change.get("asset_issuer") == self.issuer:
                found |= {change.get("from"), change.get("to")}
        return {a for a in found if a}

    # ── Effects ──────────────────────────────────────────────────────────────

    def _account_changes(self, account: str, cursor: str):
        """(paging token, day, balance change) of the asset for one account, after `cursor`, up to the head."""
        pages = 0
        params = {"order": "asc", "limit": PAGE_LIMIT, "cursor": cursor}
        path = f"/accounts/{account}/effects"
        while True:
            answer = self._get(path, params, allow_404=True)
            if answer is None:              # account merged/removed: no effects to read
                return
            records = answer.get("_embedded", {}).get("records", [])
            pages += 1
            self.stats["pages"] += 1
            self.stats["effects"] += len(records)
            for effect in records:
                if int(effect["paging_token"].partition("-")[0]) >> 32 > self._head:
                    return
                delta = self._delta(effect)
                if delta:
                    self.stats["asset_effects"] += 1
                    day = datetime.fromisoformat(effect["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
                    yield effect["paging_token"], day, delta
            if len(records) < PAGE_LIMIT:
                return
            if pages >= MAX_PAGES_PER_ACCOUNT:
                raise StellarError(f"account {account} has too many effects to read through Horizon")
            params = {"order": "asc", "limit": PAGE_LIMIT, "cursor": records[-1]["paging_token"]}

    def _delta(self, effect: dict) -> int:
        kind = effect.get("type")
        if kind == "account_credited" and self._is_asset(effect, "asset"):
            return to_raw(effect["amount"])
        if kind == "account_debited" and self._is_asset(effect, "asset"):
            return -to_raw(effect["amount"])
        if kind == "trade":
            delta = 0
            if self._is_asset(effect, "bought_asset"):
                delta += to_raw(effect["bought_amount"])
            if self._is_asset(effect, "sold_asset"):
                delta -= to_raw(effect["sold_amount"])
            return delta
        if kind in ("liquidity_pool_deposited", "liquidity_pool_withdrew"):
            reserves = effect.get("reserves_deposited") or effect.get("reserves_received") or []
            sign = -1 if kind == "liquidity_pool_deposited" else 1
            return sum(sign * to_raw(r["amount"]) for r in reserves if r.get("asset") == f"{self.code}:{self.issuer}")
        return 0

    def _is_asset(self, record: dict, prefix: str) -> bool:
        return record.get(f"{prefix}_code") == self.code and record.get(f"{prefix}_issuer") == self.issuer

    # ── Horizon ──────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None, allow_404: bool = False):
        errors = []
        for base in self.horizons:
            try:
                return http.get_json(base.rstrip("/") + path, params=params, timeout=60, max_attempts=4,
                                     allow_404=allow_404)
            except http.HttpError as exc:
                errors.append(str(exc))
        raise StellarError(f"Horizon {path} failed on every server: {'; '.join(errors)[:300]}")

    def _pages(self, path: str, params: dict):
        params = dict(params)
        while True:
            records = (self._get(path, params) or {}).get("_embedded", {}).get("records", [])
            yield from records
            if len(records) < params.get("limit", PAGE_LIMIT):
                return
            params["cursor"] = records[-1]["paging_token"]
