import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import random_schedule as schedule
from glados_cookie import FatalError

SOURCE = '''name: Checkin
on:
  schedule:
    # BEGIN GLADOS RANDOM SCHEDULE
    - cron: "0 1 * * *"
    # END GLADOS RANDOM SCHEDULE
  workflow_dispatch:
'''


def config():
    return {"base_time": "09:00", "min_offset_minutes": 0, "max_offset_minutes": 90,
            "next_offset_minutes": None, "next_run_at": None, "cron": "0 1 * * *"}


def at(value):
    return datetime.fromisoformat(value)


class PlanTests(unittest.TestCase):
    def test_tomorrow_not_later_today_and_utc_conversion(self):
        rng = Mock()
        rng.randint.return_value = 90
        original = config()
        updated, workflow = schedule.plan_next(original, SOURCE, at("2026-10-18T09:01:00+08:00"), rng=rng)
        self.assertEqual(updated["next_run_at"], "2026-10-19T10:30:00+08:00")
        self.assertEqual(updated["next_offset_minutes"], 90)
        self.assertEqual(updated["cron"], "30 2 19 10 *")
        self.assertEqual(schedule.workflow_cron(workflow), updated["cron"])
        self.assertEqual(original, config())
        rng.randint.assert_called_once_with(0, 90)

    def test_month_year_and_leap_day_boundaries(self):
        rng = Mock()
        rng.randint.return_value = 0
        cases = (("2026-10-31", "2026-11-01", "0 1 1 11 *"),
                 ("2026-12-31", "2027-01-01", "0 1 1 1 *"),
                 ("2028-02-28", "2028-02-29", "0 1 29 2 *"),
                 ("2028-02-29", "2028-03-01", "0 1 1 3 *"))
        for today, tomorrow, cron in cases:
            with self.subTest(today=today):
                updated, _ = schedule.plan_next(config(), SOURCE, at(today + "T23:59:00+08:00"), rng=rng)
                self.assertEqual(updated["next_run_at"], tomorrow + "T09:00:00+08:00")
                self.assertEqual(updated["cron"], cron)

    def test_early_beijing_time_can_be_previous_utc_date(self):
        value = dict(config(), base_time="00:30", max_offset_minutes=0)
        updated, _ = schedule.plan_next(value, SOURCE, at("2026-12-31T12:00:00+08:00"))
        self.assertEqual(updated["next_run_at"], "2027-01-01T00:30:00+08:00")
        self.assertEqual(updated["cron"], "30 16 31 12 *")

    def test_random_offsets_stay_in_config_window(self):
        value = dict(config(), min_offset_minutes=20, max_offset_minutes=40)
        for _ in range(100):
            updated, _ = schedule.plan_next(value, SOURCE, at("2026-10-18T09:00:00+08:00"))
            self.assertTrue(20 <= updated["next_offset_minutes"] <= 40)
            self.assertEqual(at(updated["next_run_at"]).date().isoformat(), "2026-10-19")

    def test_bad_config_is_rejected_before_updates(self):
        for values in ({"base_time": "9:00"}, {"min_offset_minutes": True},
                       {"min_offset_minutes": -1}, {"min_offset_minutes": 91},
                       {"max_offset_minutes": 121}, {"base_time": "23:30"},
                       {"next_offset_minutes": 5}, {"next_run_at": "2026-10-19T09:00:00"},
                       {"next_run_at": "2026-10-19T09:00:00+08:00", "next_offset_minutes": -1}):
            with self.subTest(values=values), self.assertRaises(FatalError):
                schedule.validate_config(dict(config(), **values))

    def test_workflow_edit_preserves_everything_outside_marker(self):
        for source in (SOURCE, SOURCE.replace("\n", "\r\n")):
            updated = schedule.replace_cron(source, "37 1 19 10 *")
            self.assertEqual(updated, source.replace("0 1 * * *", "37 1 19 10 *"))
        for source in (SOURCE.replace("# BEGIN", "# INVALID"), SOURCE + SOURCE,
                       SOURCE.replace('    - cron: "0 1 * * *"', '    - cron: "0 1 * * *"\n    - cron: "1 1 * * *"')):
            with self.assertRaises(FatalError):
                schedule.replace_cron(source, "37 1 19 10 *")

    def test_keepalive_leaves_bootstrap_and_pending_today_alone(self):
        now = at("2026-10-18T15:00:00+08:00")
        self.assertIsNone(schedule.plan_next(config(), SOURCE, now, repair=True))
        value = dict(config(), next_offset_minutes=30, next_run_at="2026-10-18T09:30:00+08:00",
                     cron="30 1 18 10 *")
        source = schedule.replace_cron(SOURCE, value["cron"])
        self.assertIsNone(schedule.plan_next(value, source, now, repair=True))

    def test_keepalive_repairs_expired_schedule_into_today_with_margin(self):
        value = dict(config(), next_offset_minutes=0, next_run_at="2026-10-17T09:00:00+08:00",
                     cron="0 1 17 10 *")
        rng = Mock()
        rng.randint.return_value = 8
        updated, _ = schedule.plan_next(value, SOURCE, at("2026-10-18T09:02:01+08:00"), repair=True, rng=rng)
        rng.randint.assert_called_once_with(8, 90)
        self.assertEqual(updated["next_run_at"], "2026-10-18T09:08:00+08:00")

    def test_keepalive_after_window_repairs_to_tomorrow(self):
        value = dict(config(), next_offset_minutes=0, next_run_at="2026-10-17T09:00:00+08:00",
                     cron="0 1 17 10 *")
        updated, _ = schedule.plan_next(value, SOURCE, at("2026-10-18T12:00:00+08:00"), repair=True)
        self.assertEqual(at(updated["next_run_at"]).date().isoformat(), "2026-10-19")

    def test_keepalive_resynchronizes_cron_without_rerandomizing_future_plan(self):
        value = dict(config(), next_offset_minutes=37, next_run_at="2026-10-19T09:37:00+08:00",
                     cron="bad stale cron")
        updated, source = schedule.plan_next(value, SOURCE, at("2026-10-18T12:00:00+08:00"), repair=True)
        self.assertEqual(updated["next_run_at"], value["next_run_at"])
        self.assertEqual(updated["next_offset_minutes"], 37)
        self.assertEqual(schedule.workflow_cron(source), "37 1 19 10 *")


class FakeGitHub:
    def __init__(self, conflict=False, error=None, current=None, source=SOURCE):
        self.conflict, self.error = conflict, error
        self.calls, self.reads = [], []
        self.head_reads = 0
        self.config = current or config()
        self.source = source

    def call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.startswith("/git/ref/heads/"):
            self.head_reads += 1
            return {"object": {"sha": f"head-{self.head_reads}"}}
        if method == "GET" and path.startswith("/git/commits/"):
            return {"tree": {"sha": "base-tree"}}
        if path == "/git/trees":
            return {"sha": "new-tree"}
        if path == "/git/commits":
            return {"sha": f"new-commit-{self.head_reads}"}
        if method == "PATCH":
            if self.error:
                raise schedule.APIError(self.error)
            if self.conflict and self.head_reads == 1:
                raise schedule.APIError(422)
            return {"object": payload}
        raise AssertionError((method, path))

    def text_at(self, path, head):
        self.reads.append((path, head))
        source = self.source.replace("name: Checkin", "name: User edit") if self.head_reads > 1 else self.source
        return json.dumps(self.config) if path == schedule.CONFIG_PATH else source


class RemoteTests(unittest.TestCase):
    def test_config_and_workflow_are_published_in_one_nonforced_commit(self):
        api = FakeGitHub()
        updated = schedule.update_remote(api, "main", at("2026-10-18T09:00:00+08:00"))
        tree = next(payload for method, path, payload in api.calls if path == "/git/trees")
        self.assertEqual(tree["base_tree"], "base-tree")
        files = {item["path"]: item["content"] for item in tree["tree"]}
        self.assertEqual(set(files), {schedule.CONFIG_PATH, schedule.WORKFLOW_PATH})
        self.assertEqual(json.loads(files[schedule.CONFIG_PATH]), updated)
        self.assertEqual(schedule.workflow_cron(files[schedule.WORKFLOW_PATH]), updated["cron"])
        self.assertEqual(api.calls[-1], ("PATCH", "/git/refs/heads/main", {"sha": "new-commit-1", "force": False}))
        self.assertTrue(all(head == "head-1" for _, head in api.reads))

    def test_branch_race_rereads_latest_files_and_preserves_user_changes(self):
        api = FakeGitHub(conflict=True)
        schedule.update_remote(api, "main", at("2026-10-18T09:00:00+08:00"))
        self.assertEqual(api.head_reads, 2)
        trees = [payload for method, path, payload in api.calls if path == "/git/trees"]
        self.assertIn("name: User edit", trees[-1]["tree"][1]["content"])
        commits = [payload for method, path, payload in api.calls if path == "/git/commits"]
        self.assertEqual(commits[-1]["parents"], ["head-2"])

    def test_permission_errors_do_not_force_or_repeat_publication(self):
        api = FakeGitHub(error=403)
        with self.assertRaises(schedule.APIError):
            schedule.update_remote(api, "main", at("2026-10-18T09:00:00+08:00"))
        self.assertEqual(api.head_reads, 1)
        self.assertFalse(api.calls[-1][2]["force"])

    def test_keepalive_noop_performs_no_writes(self):
        api = FakeGitHub()
        self.assertIsNone(schedule.update_remote(api, "main", at("2026-10-18T09:00:00+08:00"), repair=True))
        self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_missing_token_fails_advance_but_does_not_break_keepalive(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(schedule, "GitHub") as client:
            self.assertEqual(schedule.main([]), 1)
            self.assertEqual(schedule.main(["--repair"]), 0)
        client.assert_not_called()

    def test_checked_in_workflow_and_config_have_matching_bootstrap(self):
        value = json.loads(Path(schedule.CONFIG_PATH).read_text(encoding="utf-8"))
        source = Path(schedule.WORKFLOW_PATH).read_text(encoding="utf-8")
        schedule.validate_config(value)
        self.assertEqual(schedule.workflow_cron(source), value["cron"])
        self.assertIn("python -u checkin.py --no-jitter", source)
        self.assertIn("always() && !cancelled()", source)
        self.assertIn("python -u random_schedule.py", source)
        self.assertNotIn("GLADOS_JITTER_", source)


if __name__ == "__main__":
    unittest.main()
