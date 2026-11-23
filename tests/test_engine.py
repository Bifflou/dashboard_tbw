"""Engine and aggregation tests with a fake adapter (no network)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from scripts.adapters import Adapter, Transfer
from scripts.lib import aggregate, engine, fx, paths, pipeline
from scripts.lib import project as proj
from scripts.lib.jsonio import load_json, save_json

A, B, C, X = ("0x" + c * 40 for c in "abce")  # X is an excluded address
SLUG = "demo"


class FakeAdapter(Adapter):
    """Serves a fixed list of transfers; the cursor is the index of the next transfer."""

    def __init__(self, line, transfers, onchain=None, decimals=2, non_holders=()):
        super().__init__(line)
        self.transfers = [Transfer(day, s, r, amount, {"index": i + 1})
                          for i, (day, s, r, amount) in enumerate(transfers)]
        self.onchain = onchain
        self._decimals = decimals
        self._non_holders = set(non_holders)

    def decimals(self):
        return self._decimals

    def start_position(self):
        return {"index": 0}

    def fetch_transfers(self, position):
        return self.transfers[position["index"]:]

    def onchain_supply_raw(self):
        return self.onchain

    def non_holder_addresses(self):
        return self._non_holders


def make_line(token_id, address, excluded=()):
    identifier = {"address": address}
    return {"id": proj.make_line_id(token_id, "ethereum", identifier), "chain": "ethereum",
            "identifier": identifier, "excluded_addresses": list(excluded)}


TOKEN = {"id": "eurx", "currency": "EUR", "category": "stablecoin"}
TRANSFERS = [
    ("2026-01-01", None, A, 10_000),      # mint 100.00 to A
    ("2026-01-01", A, B, 2_500),
    ("2026-01-03", None, X, 5_000),       # mint 50.00 to the excluded address
    ("2026-01-04", B, None, 500),         # burn 5.00
    ("2026-01-05", B, A, 2_000),          # B leaves
    ("2026-01-07", A, C, 1_000),
    ("2026-01-08", A, B, 100),            # B comes back
    ("2026-01-09", C, A, 1_000),          # C leaves
    ("2026-01-10", None, C, 300),
]


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(paths, "PROJECTS_DATA_DIR", Path(self.tmp.name) / "projects")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.line = make_line("eurx", "0x" + "11" * 20, excluded=[X])

    def run_line(self, mode, last_day, transfers=TRANSFERS, onchain=None, line=None):
        line = line or self.line
        return engine.process_line(SLUG, TOKEN, line, mode=mode, last_day=last_day,
                                   adapter=FakeAdapter(line, transfers, onchain), reconcile=True)

    def series(self, line=None):
        return load_json(paths.line_series_file(SLUG, (line or self.line)["id"]))

    def state(self, line=None):
        return load_json(paths.line_state_file(SLUG, (line or self.line)["id"]))


class InitTest(EngineTestCase):
    def test_full_series(self):
        result = self.run_line("init", date(2026, 1, 10))
        days = {d["date"]: (d["supply"], d["holders"]) for d in self.series()["days"]}
        self.assertEqual(result.first_date, "2026-01-01")
        self.assertEqual(len(days), 10)                       # dense, no gap
        self.assertEqual(days["2026-01-01"], (100.0, 2))      # A, B
        self.assertEqual(days["2026-01-02"], (100.0, 2))      # carried day without transfer
        self.assertEqual(days["2026-01-03"], (100.0, 2))      # mint to X excluded from supply and holders
        self.assertEqual(days["2026-01-04"], (95.0, 2))       # burn
        self.assertEqual(days["2026-01-05"], (95.0, 1))       # B left
        self.assertEqual(days["2026-01-07"], (95.0, 2))       # C arrived
        self.assertEqual(days["2026-01-08"], (95.0, 3))       # B back
        self.assertEqual(days["2026-01-09"], (95.0, 2))       # C left
        self.assertEqual(days["2026-01-10"], (98.0, 3))       # C back with a mint

    def test_holder_periods(self):
        self.run_line("init", date(2026, 1, 10))
        periods = self.state()["holder_periods"]
        self.assertEqual(periods[B], [["2026-01-01", "2026-01-04"], ["2026-01-08", None]])
        self.assertEqual(periods[C], [["2026-01-07", "2026-01-08"], ["2026-01-10", None]])
        self.assertNotIn(X, periods)

    def test_checkpoint_three_days_behind(self):
        self.run_line("init", date(2026, 1, 10))
        checkpoint = self.state()["checkpoint"]
        self.assertEqual(checkpoint["date"], "2026-01-07")
        self.assertEqual(checkpoint["position"], {"index": 6})  # after the 6th transfer (2026-01-07)
        self.assertEqual(checkpoint["balances"][X], "5000")     # exclusions applied at read time, not stored

    def test_today_transfers_ignored_but_reconciled(self):
        transfers = TRANSFERS + [("2026-01-11", None, A, 700)]  # "today"
        result = self.run_line("init", date(2026, 1, 10), transfers=transfers, onchain=15_500)
        self.assertEqual(self.series()["last_date"], "2026-01-10")
        self.assertEqual(result.state, "ok")
        self.assertEqual(result.reconciliation["diff_pct"], 0.0)  # 148 on the last day + 7 today = 155

    def test_reconciliation_mismatch_is_a_warning(self):
        result = self.run_line("init", date(2026, 1, 10), onchain=20_000)
        self.assertEqual(result.state, "warning")
        self.assertIn("differs", result.message)

    def test_no_transfer_is_an_error(self):
        with self.assertRaisesRegex(engine.LineEngineError, "no transfer found"):
            self.run_line("init", date(2026, 1, 10), transfers=[])

    def test_negative_balance_is_an_error(self):
        with self.assertRaisesRegex(engine.LineEngineError, "negative balance"):
            self.run_line("init", date(2026, 1, 10), transfers=[("2026-01-01", A, B, 1)])


class ResumeTest(EngineTestCase):
    def test_resume_equals_full_rebuild(self):
        # Reference: one full init up to the 10th.
        self.run_line("init", date(2026, 1, 10))
        reference_series, reference_periods = self.series()["days"], self.state()["holder_periods"]

        # Same result through init on the 4th, then updates on the 6th, 6th again (idempotent) and 10th.
        self.run_line("init", date(2026, 1, 4))
        self.run_line("update", date(2026, 1, 6))
        self.run_line("update", date(2026, 1, 6))
        self.run_line("update", date(2026, 1, 10))
        self.assertEqual(self.series()["days"], reference_series)
        self.assertEqual(self.state()["holder_periods"], reference_periods)

    def test_long_outage_catches_up(self):
        self.run_line("init", date(2026, 1, 2))
        self.run_line("update", date(2026, 1, 10))   # 8 days later
        days = self.series()["days"]
        self.assertEqual([d["date"] for d in days][-1], "2026-01-10")
        self.assertEqual(len(days), 10)

    def test_update_requires_matching_state(self):
        with self.assertRaisesRegex(engine.LineEngineError, "must be initialized"):
            self.run_line("update", date(2026, 1, 10))

    def test_truncate_periods(self):
        periods = {A: [["2026-01-01", "2026-01-03"], ["2026-01-05", "2026-01-09"]], B: [["2026-01-08", None]]}
        self.assertEqual(engine.truncate_periods(periods, "2026-01-06"),
                         {A: [["2026-01-01", "2026-01-03"], ["2026-01-05", None]]})


class AggregateTest(EngineTestCase):
    def test_dashboard(self):
        eur = make_line("eurx", "0x" + "11" * 20)
        usd = make_line("usdx", "0x" + "22" * 20)
        config = {
            "slug": SLUG, "name": "Demo", "image": "img/p.png",
            "tokens": [
                {"id": "eurx", "name": "Euro X", "symbol": "EURX", "logo": None, "currency": "EUR",
                 "category": "stablecoin", "nav_type": None, "lines": [eur]},
                {"id": "usdx", "name": "Dollar X", "symbol": "USDX", "logo": None, "currency": "USD",
                 "category": "stablecoin", "nav_type": None, "lines": [usd]},
            ],
        }
        eur_transfers = [("2026-01-01", None, A, 10_000), ("2026-01-02", None, B, 10_000)]
        usd_transfers = [("2026-01-02", None, A, 22_000), ("2026-01-03", None, C, 11_000)]
        for token, line, transfers in ((config["tokens"][0], eur, eur_transfers),
                                       (config["tokens"][1], usd, usd_transfers)):
            result = engine.process_line(SLUG, token, line, mode="init", last_day=date(2026, 1, 3),
                                         adapter=FakeAdapter(line, transfers), reconcile=False)
            pipeline.save_line_status(SLUG, line, result)

        fx_file = Path(self.tmp.name) / "rates.json"
        save_json(fx_file, {"start": "2026-01-01", "end": "2026-01-03", "last_published": "2026-01-03",
                            "rates": {d: {"USD": 1.1, "CHF": 1.0, "GBP": 0.9}
                                      for d in ("2026-01-01", "2026-01-02", "2026-01-03")}})
        with mock.patch.object(fx, "FX_FILE", fx_file):
            dash = aggregate.build_dashboard(SLUG, config)

        self.assertEqual(dash["dates"], ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(dash["currencies"], ["EUR", "USD"])
        self.assertEqual(dash["default_currency"], "USD")              # 330 USD > 200 EUR (=220 USD)
        self.assertEqual(dash["image"], "projects/demo/img/p.png")
        self.assertEqual(dash["mcap"]["EUR"]["by_token"]["usdx"], [None, 200.0, 300.0])
        self.assertEqual(dash["mcap"]["USD"]["total"], [110.0, 440.0, 550.0])
        # A holds both tokens on Ethereum: counted in each token, once for the chain and the total.
        self.assertEqual(dash["holders"]["by_token"]["eurx"], [1, 2, 2])
        self.assertEqual(dash["holders"]["by_token"]["usdx"], [None, 1, 2])
        self.assertEqual(dash["holders"]["by_chain"]["ethereum"], [1, 2, 3])
        self.assertEqual(dash["holders"]["total"], [1, 2, 3])
        self.assertTrue(dash["tokens"][0]["lines"][0]["explorer_url"].startswith("https://etherscan.io/token/0x"))


if __name__ == "__main__":
    unittest.main()
