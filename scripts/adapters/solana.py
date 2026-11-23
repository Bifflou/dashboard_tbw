"""Solana adapter for SPL Token and Token-2022 mints.

Snapshot first, then history up to that snapshot:
1. all current token accounts of the mint (getProgramAccounts, with the slot they were read at) and the supply:
   that slot is the head of the fetch, so the reconciliation compares the same moment;
2. the signatures of the mint and of every token account of the mint, back to the resume slot. Token accounts
   seen in transactions but closed since are discovered on the way and scanned too;
3. each transaction's pre/post token balances of the mint give the exact balance change of each token account,
   summed per **owner** (holders are owners, SPEC §4.3).

Changes are netted per slot (all transactions of a slot share a day), which also avoids depending on the order
of transactions inside a slot. An increase is a mint to the owner, a decrease a burn from it, so the reconstructed
supply is the sum of owner balances.

RPC: Helius when HELIUS_API_KEY is set (needed for busy tokens), else the public endpoint (small tokens only:
above MAX_PUBLIC_TRANSACTIONS transactions to read, the line fails at once instead of running for hours).
An RPC node sometimes answers `null` for a transaction it does have (archive lookup): it is asked again, then the
other endpoint is tried, before the line fails.
Transactions are immutable: parsed ones are cached for the process (engine retries, engine check resume).
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from scripts.lib import chains, http

from .base import Adapter, Transfer

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SIGNATURE_PAGE = 1000
HELIUS_RATE = 8.0      # requests per second (free plan allows 10)
PUBLIC_RATE = 1.5
HELIUS_WORKERS = 16    # archive reads take ~1 s: enough requests in flight to use the whole rate
PUBLIC_WORKERS = 3
MAX_PUBLIC_TRANSACTIONS = 4000   # ~45 min at the public rate
MISSING_TX_RETRIES = 4           # extra reads of a transaction answered `null`, before trying the other endpoint
MISSING_TX_PAUSE = 3.0
PROGRESS_EVERY = 1000

_TX_CACHE: dict[tuple[str, str], dict | None] = {}


class SolanaError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_time)
            self.next_time = start + self.interval
        if start > now:
            time.sleep(start - now)


class SolanaAdapter(Adapter):
    family = "solana"

    def __init__(self, line: dict) -> None:
        super().__init__(line)
        self.mint = line["identifier"]["mint"]
        key = os.environ.get("HELIUS_API_KEY", "").strip()
        base = chains.get_chain(self.chain_id)["solana"]["helius_rpc_url"]
        self.helius = bool(key)
        self.url = f"{base}?api-key={key}" if key else PUBLIC_RPC
        self.limiter = _RateLimiter(HELIUS_RATE if key else PUBLIC_RATE)
        self.workers = HELIUS_WORKERS if key else PUBLIC_WORKERS
        self._fallback: tuple[str, _RateLimiter] | None = None
        self._program: str | None = None
        self._snapshot: dict | None = None
        self._lock = threading.Lock()
        self.stats = {"rpc": "helius" if key else "public", "token_accounts": 0, "signatures": 0,
                      "transactions_read": 0, "cached": 0, "calls": 0, "throttled": 0, "missing_retries": 0}

    # ── Adapter interface ────────────────────────────────────────────────────

    def decimals(self) -> int:
        return int(self._rpc("getTokenSupply", [self.mint])["value"]["decimals"])

    def start_position(self) -> dict:
        return {"slot": 0}

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        self._snapshot = self._take_snapshot()
        head = self._snapshot["slot"]
        since = int(position.get("slot", 0))

        to_scan = [self.mint, *self._snapshot["token_accounts"]]
        scanned: set[str] = set()
        signatures: dict[str, int] = {}
        changes: dict[int, dict] = {}       # slot -> {"time": blockTime, "owners": {owner: delta}}
        while to_scan:
            batch, to_scan = [a for a in dict.fromkeys(to_scan) if a not in scanned], []
            scanned.update(batch)
            new_signatures: dict[str, int] = {}
            for address in batch:
                for signature, slot in self._signatures(address, since, head):
                    if signature not in signatures:
                        signatures[signature] = slot
                        new_signatures[signature] = slot
            for parsed in self._transactions(list(new_signatures)):
                if parsed is None:
                    continue
                slot_changes = changes.setdefault(parsed["slot"], {"time": parsed["time"], "owners": {}})
                for owner, delta in parsed["owners"].items():
                    slot_changes["owners"][owner] = slot_changes["owners"].get(owner, 0) + delta
                to_scan.extend(a for a in parsed["token_accounts"] if a not in scanned)
        self.stats["token_accounts"] = len(scanned) - 1
        self.stats["signatures"] = len(signatures)

        transfers = []
        for slot in sorted(changes):
            entry = changes[slot]
            if entry["time"] is None:
                entry["time"] = self._rpc("getBlockTime", [slot])
            day = datetime.fromtimestamp(entry["time"], tz=timezone.utc).strftime("%Y-%m-%d")
            for owner, delta in sorted(entry["owners"].items()):
                if delta > 0:
                    transfers.append(Transfer(day, None, owner, delta, {"slot": slot + 1}))
                elif delta < 0:
                    transfers.append(Transfer(day, owner, None, -delta, {"slot": slot + 1}))
        return transfers

    def onchain_supply_raw(self) -> int | None:
        return self._snapshot["supply"] if self._snapshot else None

    def onchain_balances_raw(self) -> dict[str, int] | None:
        return dict(self._snapshot["owners"]) if self._snapshot else None

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def _take_snapshot(self) -> dict:
        program = self._token_program()
        filters = [{"memcmp": {"offset": 0, "bytes": self.mint}}]
        if program == TOKEN_PROGRAM:
            filters.insert(0, {"dataSize": 165})
        answer = self._rpc("getProgramAccounts", [program, {"encoding": "jsonParsed", "withContext": True,
                                                            "commitment": "finalized", "filters": filters}])
        owners: dict[str, int] = {}
        token_accounts = []
        for item in answer["value"]:
            info = item["account"]["data"]["parsed"]["info"]
            token_accounts.append(item["pubkey"])
            amount = int(info["tokenAmount"]["amount"])
            if amount:
                owners[info["owner"]] = owners.get(info["owner"], 0) + amount
        supply = self._rpc("getTokenSupply", [self.mint, {"commitment": "finalized"}])
        return {
            "slot": min(int(answer["context"]["slot"]), int(supply["context"]["slot"])),
            "supply": int(supply["value"]["amount"]),
            "owners": owners,
            "token_accounts": token_accounts,
        }

    def _token_program(self) -> str:
        if not self._program:
            info = self._rpc("getAccountInfo", [self.mint, {"encoding": "jsonParsed"}])["value"]
            if not info or info.get("owner") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                raise SolanaError(f"{self.mint} is not an SPL Token / Token-2022 mint")
            self._program = info["owner"]
        return self._program

    # ── History ──────────────────────────────────────────────────────────────

    def _signatures(self, address: str, since: int, head: int):
        """(signature, slot) of successful transactions of `address` with since <= slot <= head."""
        before = None
        while True:
            params = {"limit": SIGNATURE_PAGE, "commitment": "finalized"}
            if before:
                params["before"] = before
            page = self._rpc("getSignaturesForAddress", [address, params])
            for item in page:
                if item["slot"] < since:
                    return
                if item["slot"] <= head and item.get("err") is None:
                    yield item["signature"], item["slot"]
            if len(page) < SIGNATURE_PAGE:
                return
            before = page[-1]["signature"]

    def _transactions(self, signatures: list[str]) -> list[dict | None]:
        todo = [s for s in signatures if (self.mint, s) not in _TX_CACHE]
        self.stats["cached"] += len(signatures) - len(todo)
        if not self.helius and self.stats["transactions_read"] + len(todo) > MAX_PUBLIC_TRANSACTIONS:
            raise SolanaError(f"{self.stats['transactions_read'] + len(todo)}+ transactions to read: too many for "
                              f"the public RPC (limit {MAX_PUBLIC_TRANSACTIONS}); set the HELIUS_API_KEY secret")
        if todo:
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for count, (signature, parsed) in enumerate(zip(todo, pool.map(self._read_transaction, todo)), 1):
                    _TX_CACHE[(self.mint, signature)] = parsed
                    if count % PROGRESS_EVERY == 0:
                        print(f"  solana {self.mint[:8]}: {count}/{len(todo)} transactions read "
                              f"({count / (time.monotonic() - started):.1f}/s)", flush=True)
            self.stats["transactions_read"] += len(todo)
        return [_TX_CACHE[(self.mint, s)] for s in signatures]

    def _get_transaction(self, signature: str):
        params = [signature, {"encoding": "jsonParsed", "commitment": "finalized",
                              "maxSupportedTransactionVersion": 0}]
        for attempt in range(MISSING_TX_RETRIES + 1):
            tx = self._rpc("getTransaction", params)
            if tx:
                return tx
            if attempt < MISSING_TX_RETRIES:
                self._count("missing_retries")
                time.sleep(MISSING_TX_PAUSE * (attempt + 1))
        # Last resort: the other endpoint (the public RPC when on Helius; there is no other one without a key).
        if self.helius:
            self._count("missing_retries")
            return self._rpc("getTransaction", params, endpoint=self._public_endpoint())
        return None

    def _read_transaction(self, signature: str) -> dict | None:
        tx = self._get_transaction(signature)
        if not tx:
            raise SolanaError(f"transaction {signature} not found")
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        balances: dict[str, list] = {}     # token account -> [owner, pre, post]
        for field, position in (("preTokenBalances", 1), ("postTokenBalances", 2)):
            for entry in meta.get(field) or []:
                if entry.get("mint") != self.mint:
                    continue
                account = keys[entry["accountIndex"]]
                row = balances.setdefault(account, [entry.get("owner"), 0, 0])
                row[0] = row[0] or entry.get("owner")
                row[position] = int(entry["uiTokenAmount"]["amount"])
        owners: dict[str, int] = {}
        for owner, pre, post in balances.values():
            if post != pre:
                if not owner:
                    raise SolanaError(f"transaction {signature}: token balance without owner")
                owners[owner] = owners.get(owner, 0) + post - pre
        return {"slot": tx["slot"], "time": tx.get("blockTime"), "owners": owners,
                "token_accounts": list(balances)}

    # ── RPC ──────────────────────────────────────────────────────────────────

    def _public_endpoint(self) -> tuple[str, _RateLimiter]:
        with self._lock:
            if self._fallback is None:
                self._fallback = (PUBLIC_RPC, _RateLimiter(PUBLIC_RATE))
            return self._fallback

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def _rpc(self, method: str, params: list, endpoint: tuple[str, _RateLimiter] | None = None):
        url, limiter = endpoint or (self.url, self.limiter)
        for attempt in range(6):
            limiter.wait()
            self._count("calls")
            try:
                answer = http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                        timeout=90, max_attempts=3)
            except http.HttpError as exc:
                self._count("throttled")
                if attempt == 5:
                    raise SolanaError(f"{method}: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if "error" in answer:
                code = answer["error"].get("code")
                if code in (429, -32429, -32005, -32004) and attempt < 5:  # rate limit / node behind
                    self._count("throttled")
                    time.sleep(2 * (attempt + 1))
                    continue
                raise SolanaError(f"{method}: {http.redact(str(answer['error']))[:200]}")
            return answer.get("result")
        raise SolanaError(f"{method}: no answer")
