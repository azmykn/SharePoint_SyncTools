"""
Scheduler Service Tests
=======================
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_scheduler.sqlite")
        os.environ["SP_SYNC_DB_PATH"] = self.db_path

        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()

        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None

        from sp_sync.scheduler.runner import (
            get_scheduler_settings,
            save_scheduler_settings,
            mark_scheduler_run_completed,
            is_run_due,
            start_scheduler_daemon,
            stop_scheduler_daemon,
            is_scheduler_daemon_alive,
            run_scheduler_now,
        )
        self.get_scheduler_settings = get_scheduler_settings
        self.save_scheduler_settings = save_scheduler_settings
        self.mark_scheduler_run_completed = mark_scheduler_run_completed
        self.is_run_due = is_run_due
        self.start_scheduler_daemon = start_scheduler_daemon
        self.stop_scheduler_daemon = stop_scheduler_daemon
        self.is_scheduler_daemon_alive = is_scheduler_daemon_alive
        self.run_scheduler_now = run_scheduler_now

    def tearDown(self):
        self.stop_scheduler_daemon()
        self.tmp_dir.cleanup()

    def test_default_settings(self):
        settings = self.get_scheduler_settings()
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["schedule_mode"], "daily")
        self.assertEqual(settings["daily_time"], "02:00")
        self.assertEqual(settings["interval_hours"], 6)
        self.assertEqual(settings["target"], "all")
        self.assertIsNone(settings["last_run"])
        self.assertIsNone(settings["next_run"])

    def test_save_and_retrieve_settings(self):
        updated = self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "interval",
            "interval_hours": 12,
            "target": "sharepoint"
        })
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["schedule_mode"], "interval")
        self.assertEqual(updated["interval_hours"], 12)
        self.assertEqual(updated["target"], "sharepoint")
        self.assertIsNotNone(updated["next_run"])

        fetched = self.get_scheduler_settings()
        self.assertTrue(fetched["enabled"])
        self.assertEqual(fetched["interval_hours"], 12)
        self.assertEqual(fetched["target"], "sharepoint")

    def test_daily_settings_and_due(self):
        updated = self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "daily",
            "daily_time": "03:30",
            "target": "all",
            "last_run": None,
        })
        self.assertEqual(updated["daily_time"], "03:30")
        self.assertIsNotNone(updated["next_run"])

        # Force past today's slot without a last_run → due
        early = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "daily",
            "daily_time": "00:00",
            "last_run": None,
        })
        self.assertTrue(self.is_run_due())

        # Already ran after today's midnight slot → not due
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "daily",
            "daily_time": "00:00",
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.assertFalse(self.is_run_due())

        # Future daily_time today → not due yet
        future_hhmm = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "daily",
            "daily_time": future_hhmm,
            "last_run": early,
        })
        self.assertFalse(self.is_run_due())

    def test_is_run_due_logic(self):
        # 1. Disabled -> never due
        self.save_scheduler_settings({"enabled": False})
        self.assertFalse(self.is_run_due())

        # 2. Interval mode: enabled and never run -> is due
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "interval",
            "interval_hours": 6,
            "last_run": None,
        })
        self.assertTrue(self.is_run_due())

        # 3. Enabled and run recently (1 hour ago, interval 6 hours) -> not due
        recent = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "interval",
            "interval_hours": 6,
            "last_run": recent,
        })
        self.assertFalse(self.is_run_due())

        # 4. Enabled and run 7 hours ago (interval 6 hours) -> is due
        past = (datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_scheduler_settings({
            "enabled": True,
            "schedule_mode": "interval",
            "interval_hours": 6,
            "last_run": past,
        })
        self.assertTrue(self.is_run_due())

        # 5. Mark run completed updates last_run and next_run
        self.mark_scheduler_run_completed("all")
        after_run = self.get_scheduler_settings()
        self.assertIsNotNone(after_run["last_run"])
        self.assertFalse(self.is_run_due())

    def test_scheduler_summary_helpers(self):
        from sp_sync.scheduler.runner import (
            save_scheduler_summary,
            get_scheduler_summary,
            build_download_summary_line,
        )
        payload = {
            "downloaded_files": 2,
            "new_files": ["MBA-09_2026-11-07_1649.mp4", "MBA-09_2026-11-08_1000.mp4"],
            "target": "sharepoint",
        }
        save_scheduler_summary(payload)
        got = get_scheduler_summary()
        self.assertEqual(got["downloaded_files"], 2)
        line = build_download_summary_line(got)
        self.assertIn("2 new lecture", line)
        self.assertIn("MBA-09_2026-11-07_1649.mp4", line)

    def test_scheduler_validation_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"interval_hours": 3})

        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"target": "dropbox"})

        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"enabled": "maybe"})

        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"last_run": "2026-99-99"})

        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"schedule_mode": "weekly"})

        with self.assertRaises(ValueError):
            self.save_scheduler_settings({"daily_time": "25:99"})

    def test_scheduler_daemon_lifecycle(self):
        triggered = []

        def dummy_callback(target):
            triggered.append(target)

        # Start daemon with short execution check
        self.start_scheduler_daemon(sync_callback=dummy_callback)
        time.sleep(0.1)
        self.assertTrue(self.is_scheduler_daemon_alive())

        # Stop daemon
        self.stop_scheduler_daemon()
        time.sleep(0.05)

    def test_run_scheduler_now(self):
        triggered = []

        def dummy_callback(target):
            triggered.append(target)

        self.start_scheduler_daemon(sync_callback=dummy_callback)
        out = self.run_scheduler_now("gdrive")
        self.assertTrue(out["success"])
        self.assertEqual(out["target"], "gdrive")
        self.assertGreaterEqual(len(triggered), 1)
        self.assertIn("last_run", out)


if __name__ == "__main__":
    unittest.main()
