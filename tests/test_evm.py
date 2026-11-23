"""EVM adapter tests against a fake Etherscan (no network)."""

import unittest
from datetime import date

from scripts.adapters.base import Transfer
from scripts.adapters.evm import PAGE_SIZE, TRANSFER_TOPIC, EvmAdapter
from scripts.lib import engine
from tests.test_engine import A, B, EngineTestCase, FakeAdapter, TOKEN, make_line

CONTRACT = "0x" + "11" * 20
ZERO_TOPIC = "0x" + "0" * 64


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class FakeEtherscan:
    """Logs at given blocks; getLogs answers at most PAGE_SIZE logs, like Etherscan."""

    def __init__(self, blocks: list[int], head: int, creation: int = 100):
        self.logs = []
        for i, block in enumerate(blocks):
            self.logs.append({
                "blockNumber": hex(block), "logIndex": hex(i), "transactionHash": f"0xtx{i}",
                "timeStamp": hex(1767225600 + block),  # 2026-01-01 + block seconds
                "topics": [TRANSFER_TOPIC, ZERO_TOPIC, topic(A)], "data": hex(1),
            })
        self.head = head
        self.creation = creation
        self.calls = []

    def etherscan(self, params):
        self.calls.append(params)
        if params["action"] == "getcontractcreation":
            return {"status": "1", "result": [{"blockNumber": str(self.creation)}]}
        if params["action"] == "getLogs":
            start, end, page = params["fromBlock"], params["toBlock"], params["page"]
            hits = [l for l in self.logs if start <= int(l["blockNumber"], 16) <= end]
            return {"status": "1", "result": hits[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]}
        raise AssertionError(params)

    def rpc(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_call":
            return hex(len([l for l in self.logs if int(l["blockNumber"], 16) <= self.head]))
        raise AssertionError(method)


def make_adapter(fake: FakeEtherscan) -> EvmAdapter:
    adapter = EvmAdapter(make_line("eurx", CONTRACT))
    adapter.api_key = "test"
    adapter._etherscan = fake.etherscan
    adapter._rpc = fake.rpc
    return adapter


class EvmFetchTest(unittest.TestCase):
    def test_dense_history_is_complete(self):
        # 3,500 logs packed in a few hundred blocks: windows must split until each fits in one page.
        blocks = [1_000 + i // 10 for i in range(3_500)]
        fake = FakeEtherscan(blocks, head=5_000_000)
        transfers = make_adapter(fake).fetch_transfers({"block": 0})
        self.assertEqual(len(transfers), 3_500)
        getlogs = [c for c in fake.calls if c["action"] == "getLogs"]
        self.assertTrue(all(c["page"] == 1 for c in getlogs))          # never page/offset pagination
        self.assertEqual(getlogs[0]["fromBlock"], 100)                   # starts at contract creation

    def test_single_block_with_more_than_a_page(self):
        fake = FakeEtherscan([500] * 1_500 + [501], head=1_000)
        transfers = make_adapter(fake).fetch_transfers({"block": 0})
        self.assertEqual(len(transfers), 1_501)

    def test_resume_position_and_head(self):
        fake = FakeEtherscan([200, 300, 900], head=800)
        adapter = make_adapter(fake)
        transfers = adapter.fetch_transfers({"block": 250})
        self.assertEqual([t.cursor["block"] for t in transfers], [301])  # 200 before position, 900 after head
        self.assertEqual(transfers[0].sender, None)                        # mint from the zero address
        self.assertEqual(adapter.onchain_supply_raw(), 2)                  # read at the fetch head (800)

    def test_day_from_timestamp(self):
        fake = FakeEtherscan([86_400 + 5], head=100_000)
        transfer = make_adapter(fake).fetch_transfers({"block": 0})[0]
        self.assertEqual(transfer.day, "2026-01-02")


class RetryTest(EngineTestCase):
    def test_refetch_when_supply_does_not_reconcile(self):
        class Flaky(FakeAdapter):
            fetches = 0

            def fetch_transfers(self, position):
                Flaky.fetches += 1
                full = super().fetch_transfers(position)
                return full[:-1] if Flaky.fetches == 1 else full   # first answer drops a transfer

        transfers = [("2026-01-01", None, A, 100), ("2026-01-02", A, B, 40), ("2026-01-03", None, B, 10)]
        result = engine.process_line("demo", TOKEN, self.line, mode="init", last_day=date(2026, 1, 3),
                                     adapter=Flaky(self.line, transfers, onchain=110))
        self.assertEqual(Flaky.fetches, 2)
        self.assertEqual(result.state, "ok")
        self.assertEqual(self.series()["days"][-1]["supply"], 1.1)


if __name__ == "__main__":
    unittest.main()
