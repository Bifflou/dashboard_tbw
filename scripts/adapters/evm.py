"""EVM adapter (Ethereum and any chain of config/chains.json with "family": "evm").

- History: ERC-20 Transfer logs from the Etherscan API v2 (one key for every chain, selected by `chainid`).
- Current state (decimals, head block, totalSupply): eth_call on public RPCs, Etherscan proxy as fallback.

Logs are fetched by **block windows that each fit in a single page** (< 1000 logs), never with page/offset
pagination: on a long history, Etherscan's page/offset pagination was seen to silently drop logs (SPEC §8).
A full window is split in two and fetched again; quiet windows grow, so empty ranges cost few calls.

Every fetch stops at a fixed head block, and totalSupply is read at that same block, so the reconciliation
compares the same moment.

Mints are transfers from the zero address, burns transfers to it (ERC-20 standard).
"""

import os
import time
from datetime import datetime, timezone

from scripts.lib import chains, http

from .base import Adapter, Transfer

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = "0x" + "0" * 40
SELECTOR_DECIMALS = "0x313ce567"
SELECTOR_TOTAL_SUPPLY = "0x18160ddd"

PAGE_SIZE = 1000          # Etherscan maximum per call
MAX_PAGES_ONE_BLOCK = 10  # only used if a single block holds more than PAGE_SIZE transfers
INITIAL_WINDOW = 50_000   # blocks
MAX_WINDOW = 1_000_000    # blocks
CALL_INTERVAL = 0.35      # stay under the free tier rate limit
RATE_LIMIT_HINTS = ("rate limit", "max calls", "timeout", "too many", "busy")


class EvmError(RuntimeError):
    pass


def _hex_int(value: str | None) -> int:
    return int(value, 16) if value and value != "0x" else 0


class EvmAdapter(Adapter):
    family = "evm"

    def __init__(self, line: dict) -> None:
        super().__init__(line)
        chain = chains.get_chain(self.chain_id)
        self.address = line["identifier"]["address"]
        self.chainid = chain["evm"]["etherscan_chainid"]
        self.rpc_urls = chain["evm"]["rpc_urls"]
        self.api_key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
        self._last_call = 0.0
        self._head: int | None = None     # block the last fetch stopped at
        self.calls = 0                    # Etherscan calls made (for logs and tests)

    # ── Adapter interface ────────────────────────────────────────────────────

    def decimals(self) -> int:
        result = self._eth_call(SELECTOR_DECIMALS)
        if result in (None, "0x"):
            raise EvmError(f"{self.address} has no decimals(): is it an ERC-20 token on {self.chain_id}?")
        return _hex_int(result)

    def start_position(self) -> dict:
        return {"block": 0}

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        self._head = self._block_number()
        start = int(position.get("block", 0))
        if start == 0:
            start = self._creation_block()
        logs = self._fetch_logs(start, self._head)
        logs.sort(key=lambda log: (_hex_int(log["blockNumber"]), _hex_int(log.get("logIndex"))))
        transfers = []
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) != 3:  # not an ERC-20 Transfer (e.g. ERC-721 has 4 topics)
                continue
            sender = "0x" + topics[1][-40:].lower()
            receiver = "0x" + topics[2][-40:].lower()
            block = _hex_int(log["blockNumber"])
            day = datetime.fromtimestamp(_hex_int(log["timeStamp"]), tz=timezone.utc).strftime("%Y-%m-%d")
            transfers.append(Transfer(
                day=day,
                sender=None if sender == ZERO_ADDRESS else sender,
                receiver=None if receiver == ZERO_ADDRESS else receiver,
                amount=_hex_int(log.get("data")),
                cursor={"block": block + 1},
            ))
        return transfers

    def onchain_supply_raw(self) -> int | None:
        """totalSupply at the block the last fetch stopped at (latest if unavailable)."""
        for tag in ([hex(self._head)] if self._head else []) + ["latest"]:
            try:
                result = self._eth_call(SELECTOR_TOTAL_SUPPLY, tag)
            except (EvmError, http.HttpError):
                continue
            if result not in (None, "0x"):
                return _hex_int(result)
        return None

    # ── Log fetching ─────────────────────────────────────────────────────────

    def _fetch_logs(self, start: int, head: int) -> list[dict]:
        seen: set[tuple[str, str]] = set()
        logs: list[dict] = []
        window = INITIAL_WINDOW
        while start <= head:
            end = min(start + window - 1, head)
            batch = self._get_logs(start, end)
            if len(batch) >= PAGE_SIZE:
                if end > start:                      # too many logs: split the window and retry
                    window = max(1, (end - start + 1) // 2)
                    continue
                batch = self._get_logs_one_block(start)
            for log in batch:
                key = (log["transactionHash"], log.get("logIndex", ""))
                if key not in seen:
                    seen.add(key)
                    logs.append(log)
            start = end + 1
            if len(batch) < PAGE_SIZE // 4:
                window = min(window * 2, MAX_WINDOW)
        return logs

    def _get_logs(self, start: int, end: int, page: int = 1) -> list[dict]:
        batch = self._etherscan({
            "module": "logs", "action": "getLogs",
            "address": self.address, "topic0": TRANSFER_TOPIC,
            "fromBlock": start, "toBlock": end,
            "page": page, "offset": PAGE_SIZE,
        })["result"]
        if not isinstance(batch, list):
            raise EvmError(f"unexpected getLogs answer: {str(batch)[:200]}")
        return batch

    def _get_logs_one_block(self, block: int) -> list[dict]:
        logs = []
        for page in range(1, MAX_PAGES_ONE_BLOCK + 1):
            batch = self._get_logs(block, block, page)
            logs.extend(batch)
            if len(batch) < PAGE_SIZE:
                return logs
        raise EvmError(f"more than {PAGE_SIZE * MAX_PAGES_ONE_BLOCK} transfers in block {block}")

    def _creation_block(self) -> int:
        """Block where the contract was deployed (0 if Etherscan does not know it)."""
        try:
            result = self._etherscan({"module": "contract", "action": "getcontractcreation",
                                      "contractaddresses": self.address})["result"]
            return int(result[0]["blockNumber"])
        except (EvmError, http.HttpError, KeyError, IndexError, TypeError, ValueError):
            return 0

    # ── Etherscan ────────────────────────────────────────────────────────────

    def _etherscan(self, params: dict) -> dict:
        if not self.api_key:
            raise EvmError("ETHERSCAN_API_KEY is not set")
        query = {"chainid": self.chainid, **params, "apikey": self.api_key}
        last = ""
        for attempt in range(6):
            wait = CALL_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            payload = http.get_json(ETHERSCAN_URL, params=query, timeout=60)
            self._last_call = time.monotonic()
            self.calls += 1
            if "status" not in payload:          # proxy module: plain JSON-RPC answer
                return payload
            if payload["status"] == "1":
                return payload
            message = f"{payload.get('message', '')} {payload.get('result', '')}".strip()
            if "no records found" in message.lower():
                return {"status": "1", "result": []}
            if any(hint in message.lower() for hint in RATE_LIMIT_HINTS):
                last = message
                time.sleep(2 * (attempt + 1))
                continue
            raise EvmError(f"Etherscan error: {message[:200]}")
        raise EvmError(f"Etherscan kept refusing: {last[:200]}")

    # ── RPC ──────────────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        errors = []
        for url in self.rpc_urls:
            try:
                answer = http.post_json(url, payload, max_attempts=2, timeout=20)
            except http.HttpError as exc:
                errors.append(str(exc))
                continue
            if isinstance(answer, dict) and "result" in answer:
                return answer["result"]
            errors.append(str(answer.get("error") if isinstance(answer, dict) else answer)[:120])
        raise EvmError(f"{method} failed on every RPC: {'; '.join(errors)[:300]}")

    def _block_number(self) -> int:
        try:
            return _hex_int(self._rpc("eth_blockNumber", []))
        except EvmError:
            return _hex_int(self._etherscan({"module": "proxy", "action": "eth_blockNumber"})["result"])

    def _eth_call(self, data: str, tag: str = "latest") -> str | None:
        try:
            return self._rpc("eth_call", [{"to": self.address, "data": data}, tag])
        except EvmError:
            if not self.api_key:
                raise
            answer = self._etherscan({"module": "proxy", "action": "eth_call",
                                      "to": self.address, "data": data, "tag": tag})
            return answer.get("result")
