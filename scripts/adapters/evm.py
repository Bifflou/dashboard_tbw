"""EVM adapter (Ethereum and any chain of config/chains.json with "family": "evm").

- History: ERC-20 Transfer logs from the Etherscan API v2 (one key for every chain, selected by `chainid`).
- Current state (decimals, totalSupply): eth_call on public RPCs, Etherscan proxy as fallback.

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

PAGE_SIZE = 1000
MAX_PAGES = 10            # Etherscan caps page x offset at 10,000 results
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

    # ── Adapter interface ────────────────────────────────────────────────────

    def decimals(self) -> int:
        result = self._eth_call(SELECTOR_DECIMALS)
        if result in (None, "0x"):
            raise EvmError(f"{self.address} has no decimals(): is it an ERC-20 token on {self.chain_id}?")
        return _hex_int(result)

    def start_position(self) -> dict:
        return {"block": 0}

    def fetch_transfers(self, position: dict) -> list[Transfer]:
        logs = self._fetch_logs(int(position.get("block", 0)))
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
        try:
            result = self._eth_call(SELECTOR_TOTAL_SUPPLY)
        except (EvmError, http.HttpError):
            return None
        return _hex_int(result) if result not in (None, "0x") else None

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

    def _fetch_logs(self, from_block: int) -> list[dict]:
        seen: set[tuple[str, str]] = set()
        logs: list[dict] = []
        current = from_block
        while True:
            last_block = None
            for page in range(1, MAX_PAGES + 1):
                batch = self._etherscan({
                    "module": "logs", "action": "getLogs",
                    "address": self.address, "topic0": TRANSFER_TOPIC,
                    "fromBlock": current, "toBlock": "latest",
                    "page": page, "offset": PAGE_SIZE,
                })["result"]
                if not isinstance(batch, list):
                    raise EvmError(f"unexpected getLogs answer: {str(batch)[:200]}")
                for log in batch:
                    key = (log["transactionHash"], log.get("logIndex", ""))
                    if key not in seen:
                        seen.add(key)
                        logs.append(log)
                if len(batch) < PAGE_SIZE:
                    return logs
                last_block = _hex_int(batch[-1]["blockNumber"])
            # 10 full pages: start again from the last block seen (duplicates are skipped above).
            if last_block is None or last_block <= current:
                raise EvmError(f"more than {PAGE_SIZE * MAX_PAGES} transfers in block {current}: cannot paginate")
            current = last_block

    # ── RPC ──────────────────────────────────────────────────────────────────

    def _eth_call(self, data: str) -> str | None:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": self.address, "data": data}, "latest"]}
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
        if self.api_key:
            answer = self._etherscan({"module": "proxy", "action": "eth_call",
                                      "to": self.address, "data": data, "tag": "latest"})
            if "result" in answer:
                return answer["result"]
        raise EvmError(f"eth_call failed on every RPC: {'; '.join(errors)[:300]}")
