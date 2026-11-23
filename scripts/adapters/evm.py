"""EVM adapter (Ethereum and any chain of config/chains.json with "family": "evm").

History = ERC-20 Transfer logs, read **from nodes first** (SPEC §8, findings of 2026-11-23):
- public RPC `eth_getLogs` by windows of `logs_max_range` blocks. Recent windows can use any provider of
  `logs_rpc`; older ones only providers marked `"archive": true`. A window refused for having too many results
  is split in two;
- Etherscan API v2 only as a fallback, window by window, when no node answers. Etherscan is queried by block
  windows that each fit in one page (its page/offset pagination was seen to drop logs).

Every fetch stops at a fixed head block and totalSupply is read at that same block, so the reconciliation
compares the same moment. Mints come from the zero address, burns go to it (ERC-20 standard).
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

RECENT_BLOCKS = 50_000     # windows closer than this to the head may use non-archive nodes
PAGE_SIZE = 1000           # Etherscan maximum per call
MAX_PAGES_ONE_BLOCK = 10   # only used if a single block holds more than PAGE_SIZE transfers
ETHERSCAN_MAX_WINDOW = 1_000_000
CALL_INTERVAL = 0.35       # Etherscan free tier rate limit
RATE_LIMIT_HINTS = ("rate limit", "max calls", "timeout", "too many", "busy")
TOO_MANY_RESULTS_HINTS = ("more than", "too many", "response size", "query returned", "limit", "exceed")


class EvmError(RuntimeError):
    pass


def _hex_int(value: str | None) -> int:
    return int(value, 16) if value and value != "0x" else 0


class EvmAdapter(Adapter):
    family = "evm"

    def __init__(self, line: dict) -> None:
        super().__init__(line)
        evm = chains.get_chain(self.chain_id)["evm"]
        self.address = line["identifier"]["address"]
        self.chainid = evm["etherscan_chainid"]
        self.rpc_urls = evm["rpc_urls"]
        self.logs_rpc = evm.get("logs_rpc", [])
        self.logs_max_range = int(evm.get("logs_max_range", 10_000))
        self.api_key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
        self._last_call = 0.0
        self._head: int | None = None
        self.stats = {"rpc_windows": 0, "etherscan_windows": 0, "etherscan_calls": 0, "logs": 0}

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
        start = int(position.get("block", 0)) or self._creation_block()
        logs = self._fetch_logs(start, self._head)
        self.stats["logs"] = len(logs)
        timestamps = self._timestamps(logs)
        transfers = []
        for log in sorted(logs, key=lambda l: (_hex_int(l["blockNumber"]), _hex_int(l.get("logIndex")))):
            topics = log.get("topics") or []
            if len(topics) != 3:  # not an ERC-20 Transfer (e.g. ERC-721 has 4 topics)
                continue
            sender = "0x" + topics[1][-40:].lower()
            receiver = "0x" + topics[2][-40:].lower()
            block = _hex_int(log["blockNumber"])
            day = datetime.fromtimestamp(timestamps[block], tz=timezone.utc).strftime("%Y-%m-%d")
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
        a = start
        while a <= head:
            b = min(a + self.logs_max_range - 1, head)
            batch = self._rpc_logs(a, b, recent=head - b <= RECENT_BLOCKS)
            if batch is None:
                batch = self._etherscan_logs(a, b)
                self.stats["etherscan_windows"] += 1
            else:
                self.stats["rpc_windows"] += 1
            for log in batch:
                if log.get("removed"):
                    continue
                key = (log["transactionHash"], str(_hex_int(log.get("logIndex"))))
                if key not in seen:
                    seen.add(key)
                    logs.append(log)
            a = b + 1
        return logs

    def _rpc_logs(self, a: int, b: int, recent: bool) -> list[dict] | None:
        """Logs of [a, b] from the first node that answers, or None if none can."""
        query = {"address": self.address, "topics": [TRANSFER_TOPIC], "fromBlock": hex(a), "toBlock": hex(b)}
        for provider in self.logs_rpc:
            if not recent and not provider.get("archive"):
                continue
            try:
                answer = self._post(provider["url"], {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                                                      "params": [query]}, max_attempts=3, timeout=60)
            except http.HttpError:
                continue
            if isinstance(answer, dict) and isinstance(answer.get("result"), list):
                return answer["result"]
            message = str((answer or {}).get("error", "")).lower() if isinstance(answer, dict) else ""
            if b > a and any(hint in message for hint in TOO_MANY_RESULTS_HINTS):
                middle = (a + b) // 2
                left = self._rpc_logs(a, middle, recent)
                right = self._rpc_logs(middle + 1, b, recent) if left is not None else None
                if left is not None and right is not None:
                    return left + right
        return None

    def _timestamps(self, logs: list[dict]) -> dict[int, int]:
        """Block -> timestamp, from the logs themselves when the source provides it."""
        stamps: dict[int, int] = {}
        missing: set[int] = set()
        for log in logs:
            block = _hex_int(log["blockNumber"])
            value = log.get("blockTimestamp") or log.get("timeStamp")
            if value:
                stamps[block] = _hex_int(value)
            else:
                missing.add(block)
        for block in sorted(missing - set(stamps)):
            header = self._rpc("eth_getBlockByNumber", [hex(block), False])
            stamps[block] = _hex_int(header["timestamp"])
        return stamps

    # ── Etherscan (fallback) ─────────────────────────────────────────────────

    def _etherscan_logs(self, start: int, end: int) -> list[dict]:
        """Logs of [start, end] by sub-windows that each fit in a single Etherscan page."""
        logs: list[dict] = []
        window = end - start + 1
        while start <= end:
            stop = min(start + window - 1, end)
            batch = self._get_logs(start, stop)
            if len(batch) >= PAGE_SIZE:
                if stop > start:
                    window = max(1, (stop - start + 1) // 2)
                    continue
                batch = self._get_logs_one_block(start)
            logs.extend(batch)
            start = stop + 1
            if len(batch) < PAGE_SIZE // 4:
                window = min(window * 2, ETHERSCAN_MAX_WINDOW)
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
            self.stats["etherscan_calls"] += 1
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

    def _post(self, url: str, payload: dict, **kwargs):
        return http.post_json(url, payload, **kwargs)

    def _rpc(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        errors = []
        for url in self.rpc_urls:
            try:
                answer = self._post(url, payload, max_attempts=2, timeout=20)
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
