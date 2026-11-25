"""Tests for SyncOrchestrator (TOCTOU lock, cancellation, single-folder sync, dry-run, multi-service metrics)."""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync import sync_metrics
from sp_sync.sync_orchestrator import SyncOrchestrator


class TestOrchestratorAndCancellation(unittest.TestCase):
    def setUp(self):
        sync_metrics.reset_metrics()
        self.logs = []
        self.reports = []
        self.orch = SyncOrchestrator(
            log_fn=lambda src, msg: self.logs.append((src, msg)),
            save_report_fn=lambda svc, rep: self.reports.append((svc, rep)),
        )

    def tearDown(self):
        sync_metrics.reset_metrics()

    def test_prevents_concurrent_same_service_run_and_supports_stop(self):
        started = threading.Event()

        def slow_sp_sync(*_args, cancel_event=None, **_kwargs):
            started.set()
            while cancel_event and not cancel_event.is_set():
                time.sleep(0.02)
            return 0

        cfgs = [{"name": "C1", "rel_url": "/personal/u/Documents/C1", "local_path": "/tmp/c1"}]
        with patch("sp_sync.sharepoint.sync_engine.start_sync_from_config", side_effect=slow_sp_sync), patch(
            "sp_sync.sharepoint.sync_engine._cleanup_playwright"
        ):
            ok1, _m1, t1 = self.orch.start_sharepoint(
                fedauth="A" * 60, rtfa="B" * 25, site_url="https://t.sharepoint.com/personal/u", configs=cfgs
            )
            self.assertTrue(ok1)
            self.assertTrue(started.wait(timeout=2.0))

            # Second simultaneous launch MUST be blocked atomically!
            ok2, msg2, t2 = self.orch.start_sharepoint(
                fedauth="A" * 60, rtfa="B" * 25, site_url="https://t.sharepoint.com/personal/u", configs=cfgs
            )
            self.assertFalse(ok2)
            self.assertIsNone(t2)
            self.assertIn("already running", msg2.lower())

            # Request stop
            stop_res = self.orch.request_stop("sharepoint")
            self.assertEqual(stop_res["stopping"], ["sharepoint"])
            t1.join(timeout=3.0)

        self.assertFalse(self.orch.is_running("sharepoint"))
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.reports[0][1]["status"], "cancelled")

    def test_concurrent_sp_and_gdrive_metrics_do_not_clobber_each_other(self):
        sync_metrics.set_sync_active(True, service="sharepoint", queue_total=2)
        sync_metrics.set_sync_active(True, service="gdrive", queue_total=3)
        sync_metrics.start_file_progress("SP_F", "lec.mp4", 1000, service="sharepoint")
        sync_metrics.update_file_progress(500, 1000, service="sharepoint")
        sync_metrics.start_file_progress("GD_F", "doc.pdf", 2000, service="gdrive")
        sync_metrics.update_file_progress(1000, 2000, service="gdrive")

        snap = sync_metrics.get_live_metrics()
        self.assertTrue(snap["is_active"])
        self.assertEqual(snap["service"], "all")
        self.assertEqual(snap["services"]["sharepoint"]["file_progress_pct"], 50)
        self.assertEqual(snap["services"]["gdrive"]["file_progress_pct"], 50)

        # Stopping gdrive must keep sharepoint active!
        sync_metrics.set_sync_active(False, service="gdrive")
        snap2 = sync_metrics.get_live_metrics()
        self.assertTrue(snap2["is_active"])
        self.assertEqual(snap2["service"], "sharepoint")
        self.assertFalse(snap2["services"]["gdrive"]["is_active"])
        self.assertTrue(snap2["services"]["sharepoint"]["is_active"])

    def test_single_folder_sync_only_runs_requested_index(self):
        ran = []

        def fake_sp(_f, _r, _s, cfg, **_kw):
            ran.append(cfg["name"])
            return 1

        cfgs = [
            {"name": "First", "rel_url": "/personal/u/Documents/1", "local_path": "/tmp/1", "sync_enabled": True},
            {"name": "Second", "rel_url": "/personal/u/Documents/2", "local_path": "/tmp/2", "sync_enabled": False},
        ]
        with patch("sp_sync.sharepoint.sync_engine.start_sync_from_config", side_effect=fake_sp), patch(
            "sp_sync.sharepoint.sync_engine._cleanup_playwright"
        ):
            ok, _m, t = self.orch.start_sharepoint(
                fedauth="A" * 60,
                rtfa="B" * 25,
                site_url="https://t.sharepoint.com/personal/u",
                configs=cfgs,
                config_index=1,
            )
            self.assertTrue(ok)
            t.join(timeout=3.0)

        self.assertEqual(ran, ["Second"])
        self.assertEqual(self.reports[0][1]["downloaded_files"], 1)


if __name__ == "__main__":
    unittest.main()
