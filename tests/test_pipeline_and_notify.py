import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import notify
from scripts.lib import paths, pipeline
from scripts.lib.jsonio import save_json
from tests.test_project import valid_config


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(paths, "PROJECTS_DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.config = valid_config()
        self.lines = [l for t in self.config["tokens"] for l in t["lines"]]

    def write_status(self, lines: dict):
        save_json(paths.status_file("demo-project"), {"schema_version": 1, "project": "demo-project", "lines": lines})

    def entry(self, line, **extra):
        return {"fingerprint": pipeline.line_fingerprint(line), "state": "ok", "last_date": "2026-11-22", **extra}

    def test_everything_pending_without_status(self):
        self.assertEqual(len(pipeline.pending_lines("demo-project", self.config)), 4)
        self.assertEqual(pipeline.project_state("demo-project", self.config), "pending")

    def test_initialized_and_changed_lines(self):
        first, second = self.lines[0], self.lines[1]
        self.write_status({
            first["id"]: self.entry(first),
            second["id"]: {**self.entry(second), "fingerprint": "old"},
        })
        pending_ids = {l["id"] for _, l in pipeline.pending_lines("demo-project", self.config)}
        self.assertNotIn(first["id"], pending_ids)
        self.assertIn(second["id"], pending_ids)

    def test_failed_init_stays_pending(self):
        first = self.lines[0]
        self.write_status({first["id"]: {**self.entry(first), "state": "error", "last_date": None}})
        pending_ids = {l["id"] for _, l in pipeline.pending_lines("demo-project", self.config)}
        self.assertIn(first["id"], pending_ids)

    def test_worst_state_and_orphans(self):
        self.write_status({
            self.lines[0]["id"]: self.entry(self.lines[0]),
            self.lines[1]["id"]: self.entry(self.lines[1], state="warning"),
            "gone_ethereum_12345678": {"state": "ok", "last_date": "2026-11-22"},
        })
        self.assertEqual(pipeline.project_state("demo-project", self.config), "warning")
        self.assertEqual(pipeline.orphan_line_ids("demo-project", self.config), ["gone_ethereum_12345678"])


class NotifyTest(unittest.TestCase):
    def test_quiet_daily_sends_nothing(self):
        report = {"workflow": "daily", "entries": [{"level": "info", "scope": "fx", "message": "ok"}]}
        self.assertIsNone(notify.build_message(report, "success", None))

    def test_daily_with_error(self):
        report = {"workflow": "daily", "entries": [{"level": "error", "scope": "p/l", "message": "boom"}]}
        message = notify.build_message(report, "success", "https://example.invalid/run")
        embed = message["embeds"][0]
        self.assertIn("issues", embed["title"])
        self.assertIn("boom", embed["description"])
        self.assertEqual(embed["color"], notify.COLORS["error"])

    def test_init_always_notifies(self):
        report = {
            "workflow": "init",
            "project_hint": "demo-project",
            "entries": [],
            "initialized": [{"project": "demo-project", "line_id": "x", "first_date": "2024-01-01", "last_date": "2026-11-22"}],
        }
        with mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "Owner/repo"}):
            message = notify.build_message(report, "success", None)
        embed = message["embeds"][0]
        self.assertEqual(embed["title"], "Initialization finished — demo-project")
        self.assertIn("https://owner.github.io/repo/dashboard.html?p=demo-project", embed["description"])

    def test_failed_job(self):
        message = notify.build_message({"workflow": "init", "entries": []}, "failure", None)
        self.assertIn("failed", message["embeds"][0]["title"])


if __name__ == "__main__":
    unittest.main()
