import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from scripts.lib import fx
from scripts.lib.jsonio import load_json

ROW_A = {"CHF": 0.95, "GBP": 0.85, "USD": 1.10}
ROW_B = {"CHF": 0.96, "GBP": 0.86, "USD": 1.20}


class FillCalendarTest(unittest.TestCase):
    def test_weekend_carries_friday(self):
        published = {"2026-11-20": ROW_A, "2026-11-23": ROW_B}  # Friday, Monday
        cal = fx.fill_calendar(published, date(2026, 11, 20), date(2026, 11, 23))
        self.assertEqual(list(cal), ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"])
        self.assertEqual(cal["2026-11-22"], ROW_A)
        self.assertEqual(cal["2026-11-23"], ROW_B)

    def test_first_day_uses_earlier_publication(self):
        published = {"2018-12-31": ROW_A, "2019-01-02": ROW_B}
        cal = fx.fill_calendar(published, date(2019, 1, 1), date(2019, 1, 2))
        self.assertEqual(cal["2019-01-01"], ROW_A)

    def test_seed_used_when_nothing_before(self):
        cal = fx.fill_calendar({}, date(2026, 1, 1), date(2026, 1, 2), seed=ROW_A)
        self.assertEqual(cal["2026-01-02"], ROW_A)

    def test_no_rate_at_all_raises(self):
        with self.assertRaises(fx.FxError):
            fx.fill_calendar({}, date(2026, 1, 1), date(2026, 1, 2))


class ConversionTest(unittest.TestCase):
    rates = {"rates": {"2026-11-20": ROW_A}}

    def test_same_currency(self):
        self.assertEqual(fx.rate(self.rates, "2026-11-20", "USD", "USD"), 1.0)

    def test_from_base(self):
        self.assertAlmostEqual(fx.convert(self.rates, "2026-11-20", 100, "EUR", "USD"), 110.0)

    def test_to_base(self):
        self.assertAlmostEqual(fx.convert(self.rates, "2026-11-20", 110, "USD", "EUR"), 100.0)

    def test_cross_rate(self):
        # 1 GBP = 1/0.85 EUR = 1.10/0.85 USD
        self.assertAlmostEqual(fx.convert(self.rates, "2026-11-20", 1, "GBP", "USD"), 1.10 / 0.85)

    def test_missing_day(self):
        with self.assertRaises(fx.FxError):
            fx.rate(self.rates, "2020-01-01", "EUR", "USD")


class UpdateRatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "rates.json"
        patcher = mock.patch.object(fx, "FX_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def fake_fetch(published):
        def fetch(start, end):
            return {d: r for d, r in published.items() if start.isoformat() <= d <= end.isoformat()}
        return fetch

    def test_create_then_incremental(self):
        published = {"2025-12-31": ROW_A, "2026-01-02": ROW_A, "2026-01-05": ROW_B}
        with mock.patch.object(fx, "fetch_published", side_effect=self.fake_fetch(published)):
            data, written = fx.update_rates(start=date(2026, 1, 1), end=date(2026, 1, 5))
        self.assertTrue(written)
        self.assertEqual(data["start"], "2026-01-01")
        self.assertEqual(data["last_published"], "2026-01-05")
        self.assertEqual(len(data["rates"]), 5)
        self.assertEqual(data["rates"]["2026-01-01"], ROW_A)   # holiday, carried from 2025-12-31
        self.assertEqual(data["rates"]["2026-01-04"], ROW_A)   # Sunday

        # Next day: only the refresh window is fetched, file extended by one day.
        published["2026-01-06"] = ROW_B
        calls = []

        def tracking(start, end):
            calls.append((start, end))
            return self.fake_fetch(published)(start, end)

        with mock.patch.object(fx, "fetch_published", side_effect=tracking):
            data, _ = fx.update_rates(end=date(2026, 1, 6))
        self.assertEqual(calls, [(date(2026, 1, 2), date(2026, 1, 6))])
        self.assertEqual(data["end"], "2026-01-06")
        self.assertEqual(data["rates"]["2026-01-06"], ROW_B)
        self.assertEqual(load_json(self.file)["last_published"], "2026-01-06")

    def test_unchanged_content_is_not_rewritten(self):
        published = {"2026-01-02": ROW_A}
        with mock.patch.object(fx, "fetch_published", side_effect=self.fake_fetch(published)):
            fx.update_rates(start=date(2026, 1, 2), end=date(2026, 1, 3))
            _, written = fx.update_rates(end=date(2026, 1, 3))
        self.assertFalse(written)

    def test_backward_extension(self):
        published = {"2025-12-30": ROW_A, "2026-01-02": ROW_B}
        with mock.patch.object(fx, "fetch_published", side_effect=self.fake_fetch(published)):
            fx.update_rates(start=date(2026, 1, 2), end=date(2026, 1, 3))
            data, _ = fx.update_rates(start=date(2025, 12, 30), end=date(2026, 1, 3))
        self.assertEqual(data["start"], "2025-12-30")
        self.assertEqual(data["rates"]["2025-12-31"], ROW_A)
        self.assertEqual(data["rates"]["2026-01-03"], ROW_B)


if __name__ == "__main__":
    unittest.main()
