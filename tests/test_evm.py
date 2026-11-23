"""EVM adapter tests against fake nodes and a fake Etherscan (no network)."""

import unittest
from datetime import date
from unittest import mock

from scripts.adapters import evm
from scripts.adapters.evm import PAGE_SIZE, TRANSFER_TOPIC, EvmAdapter
from scripts.lib import engine, http
from tests.test_engine import A, B, EngineTestCase, FakeAdapter, TOKEN, make_line

CONTRACT = "0x" + "11" * 20
ZERO_TOPIC = "0x" + "0" * 64
RECENT = "https://recent.example"     # non-archive node
ARCHIVE = "https://archive.example"   # archive node, refuses more than NODE_MAX_RESULTS logs per query
NODE_MAX_RESULTS = 500
RECENT_DEPTH = 2_000                  # the non-archive node only serves this many blocks back


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class FakeChain:
    """Mints of 1 unit to A at the given blocks, served by two nodes and Etherscan."""

    def __init__(self, blocks: list[int], head: int, creation: int = 100):
        self.logs = [{
            "blockNumber": hex(block), "logIndex": hex(i), "transactionHash": f"0xtx{i}",
            "blockTimestamp": hex(1767225600 + block),  # 2026-01-01 + block seconds
            "topics": [TRANSFER_TOPIC, ZERO_TOPIC, topic(A)], "data": hex(1),
        } for i, block in enumerate(blocks)]
        self.head = head
        self.creation = creation
        self.nodes_down = False
        self.etherscan_calls = []
        self.node_calls = []

    def _in_range(self, start: int, end: int) -> list[dict]:
        return [l for l in self.logs if start <= int(l["blockNumber"], 16) <= end]

    def post(self, url, payload, **kwargs):
        method, params = payload["method"], payload["params"]
        if self.nodes_down:
            raise http.HttpError(f"{url} down")
        if method == "eth_blockNumber":
            return {"result": hex(self.head)}
        if method == "eth_call":
            tag = params[1]
            block = self.head if tag == "latest" else int(tag, 16)
            return {"result": hex(len(self._in_range(0, block)))}
        if method == "eth_getLogs":
            start, end = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            self.node_calls.append((url, start, end))
            if url == RECENT and self.head - start > RECENT_DEPTH:
                raise http.HttpError("HTTP 403: Archive requests require a personal token")
            hits = self._in_range(start, end)
            if url == ARCHIVE and len(hits) > NODE_MAX_RESULTS:
                return {"error": {"code": -32005, "message": f"query returned more than {NODE_MAX_RESULTS} results"}}
            return {"result": hits}
        raise AssertionError(method)

    def etherscan(self, params):
        self.etherscan_calls.append(params)
        if params["action"] == "getcontractcreation":
            return {"status": "1", "result": [{"blockNumber": str(self.creation)}]}
        if params["action"] == "getLogs":
            hits = self._in_range(params["fromBlock"], params["toBlock"])
            page = params["page"]
            return {"status": "1", "result": hits[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]}
        raise AssertionError(params)


def make_adapter(chain: FakeChain) -> EvmAdapter:
    adapter = EvmAdapter(make_line("eurx", CONTRACT))
    adapter.api_key = "test"
    adapter.rpc_urls = [RECENT]
    adapter.logs_rpc = [{"url": RECENT, "archive": False}, {"url": ARCHIVE, "archive": True}]
    adapter.logs_max_range = 1_000
    adapter._post = chain.post
    adapter._etherscan = chain.etherscan
    return adapter


class EvmFetchTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("RECENT_BLOCKS", RECENT_DEPTH), ("NODE_INTERVAL", 0), ("NODE_ROUND_PAUSES", (0, 0))):
            patcher = mock.patch.object(evm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_history_comes_from_nodes(self):
        chain = FakeChain([150 + i * 3 for i in range(3_000)], head=12_000)
        transfers = make_adapter(chain).fetch_transfers({"block": 0})
        self.assertEqual(len(transfers), 3_000)
        self.assertFalse([c for c in chain.etherscan_calls if c["action"] == "getLogs"])
        old = [c for c in chain.node_calls if chain.head - c[2] > RECENT_DEPTH]
        self.assertTrue(old and all(url == ARCHIVE for url, _, _ in old))   # old windows: archive node only
        self.assertEqual(chain.node_calls[0][1], 100)                       # starts at contract creation

    def test_window_with_too_many_results_is_split(self):
        chain = FakeChain([500 + i // 4 for i in range(1_800)], head=20_000)  # 1,800 logs in 450 blocks
        transfers = make_adapter(chain).fetch_transfers({"block": 0})
        self.assertEqual(len(transfers), 1_800)

    def test_etherscan_fallback_uses_single_page_windows(self):
        chain = FakeChain([1_000 + i // 10 for i in range(3_500)], head=20_000)
        chain.nodes_down = True
        adapter = make_adapter(chain)
        adapter._block_number = lambda: chain.head
        transfers = adapter.fetch_transfers({"block": 0})
        self.assertEqual(len(transfers), 3_500)
        getlogs = [c for c in chain.etherscan_calls if c["action"] == "getLogs"]
        self.assertTrue(getlogs and all(c["page"] == 1 for c in getlogs))

    def test_single_block_with_more_than_a_page_on_etherscan(self):
        chain = FakeChain([500] * 1_500 + [501], head=1_000)
        chain.nodes_down = True
        adapter = make_adapter(chain)
        adapter._block_number = lambda: chain.head
        self.assertEqual(len(adapter.fetch_transfers({"block": 0})), 1_501)

    def test_resume_position_head_and_supply(self):
        chain = FakeChain([200, 300, 900], head=800)
        adapter = make_adapter(chain)
        transfers = adapter.fetch_transfers({"block": 250})
        self.assertEqual([t.cursor["block"] for t in transfers], [301])  # 200 before position, 900 after head
        self.assertIsNone(transfers[0].sender)                             # mint from the zero address
        self.assertEqual(adapter.onchain_supply_raw(), 2)                  # read at the fetch head (800)

    def test_day_from_block_timestamp(self):
        chain = FakeChain([86_400 + 5], head=88_000)
        transfer = make_adapter(chain).fetch_transfers({"block": 86_000})[0]
        self.assertEqual(transfer.day, "2026-01-02")


class RetryTest(EngineTestCase):
    def flaky(self, drop_index):
        class Flaky(FakeAdapter):
            fetches = 0

            def fetch_transfers(self, position):
                Flaky.fetches += 1
                full = super().fetch_transfers(position)
                if Flaky.fetches == 1:                                   # first answer drops a transfer
                    return full[:drop_index] + full[drop_index + 1:]
                return full
        return Flaky

    def test_refetch_when_supply_does_not_reconcile(self):
        Flaky = self.flaky(2)
        transfers = [("2026-01-01", None, A, 100), ("2026-01-02", A, B, 40), ("2026-01-03", None, B, 10)]
        result = engine.process_line("demo", TOKEN, self.line, mode="init", last_day=date(2026, 1, 3),
                                     adapter=Flaky(self.line, transfers, onchain=110))
        self.assertEqual(Flaky.fetches, 2)
        self.assertEqual(result.state, "ok")
        self.assertEqual(self.series()["days"][-1]["supply"], 1.1)

    def test_refetch_when_a_balance_goes_negative(self):
        Flaky = self.flaky(0)  # dropping the mint makes A's transfer impossible
        transfers = [("2026-01-01", None, A, 100), ("2026-01-02", A, B, 40)]
        result = engine.process_line("demo", TOKEN, self.line, mode="init", last_day=date(2026, 1, 2),
                                     adapter=Flaky(self.line, transfers, onchain=100))
        self.assertEqual(Flaky.fetches, 2)
        self.assertEqual(result.state, "ok")


if __name__ == "__main__":
    unittest.main()
