"""Tests for upfront filtering of excluded folders/files and top priority for Queued for Re-download."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync import sync_metrics
from sp_sync.sync_orchestrator import SyncOrchestrator


class TestSyncQueueFilteringAndPriority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("SP_SYNC_DB_PATH")
        os.environ["SP_SYNC_DB_PATH"] = os.path.join(self.tmp.name, "queue_prio.sqlite")
        from sp_sync.config.settings import clear_settings_cache
        import sp_sync.db.store as store_mod

        clear_settings_cache()
        store_mod._store_singleton = None
        self.store = store_mod.get_store()
        sync_metrics.reset_metrics()
        self.logs = []
        self.reports = []
        self.orch = SyncOrchestrator(
            log_fn=lambda src, msg: self.logs.append((src, msg)),
            save_report_fn=lambda svc, rep: self.reports.append((svc, rep)),
        )

    def tearDown(self):
        import sp_sync.db.store as store_mod
        from sp_sync.config.settings import clear_settings_cache

        sync_metrics.reset_metrics()
        store_mod._store_singleton = None
        if self._old_db is None:
            os.environ.pop("SP_SYNC_DB_PATH", None)
        else:
            os.environ["SP_SYNC_DB_PATH"] = self._old_db
        clear_settings_cache()
        self.tmp.cleanup()

    def test_paused_configs_filtered_upfront_without_spam(self):
        ran = []

        def fake_sp(_f, _r, _s, cfg, **_kw):
            ran.append(cfg["name"])
            return 0

        cfgs = [
            {"name": "Paused 1", "rel_url": "/personal/u/Documents/P1", "local_path": self.tmp.name, "sync_enabled": False},
            {"name": "Active A", "rel_url": "/personal/u/Documents/A", "local_path": self.tmp.name, "sync_enabled": True},
            {"name": "Paused 2", "rel_url": "/personal/u/Documents/P2", "local_path": self.tmp.name, "sync_enabled": False},
            {"name": "Active B", "rel_url": "/personal/u/Documents/B", "local_path": self.tmp.name, "sync_enabled": True},
        ]
        with patch("sp_sync.sharepoint.sync_engine.start_sync_from_config", side_effect=fake_sp), patch(
            "sp_sync.sharepoint.sync_engine._cleanup_playwright"
        ):
            ok, _msg, t = self.orch.start_sharepoint(
                fedauth="A" * 60, rtfa="B" * 25, site_url="https://t.sharepoint.com/personal/u", configs=cfgs
            )
            self.assertTrue(ok)
            t.join(timeout=3.0)

        self.assertEqual(ran, ["Active A", "Active B"])
        rep = self.reports[0][1]
        self.assertEqual(rep["configs_total"], 4)
        self.assertEqual(rep["configs_processed"], 2)
        self.assertEqual(rep["configs_skipped"], 2)
        # No per-folder "Skipped (sync off)" log spam; single concise summary line instead
        log_msgs = [m for _s, m in self.logs]
        self.assertFalse(any("Skipped (sync off)" in m for m in log_msgs))
        self.assertTrue(any("2 paused excluded" in m for m in log_msgs))

    def test_priority_redownload_runs_strictly_before_regular_folder_sync(self):
        # Record an item belonging to a PAUSED folder and mark it Queued for Re-download
        paused_dir = os.path.join(self.tmp.name, "PausedCourse")
        os.makedirs(paused_dir, exist_ok=True)
        existing_file = os.path.join(paused_dir, "lecture_old.mp4")
        with open(existing_file, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)

        key = self.store.ledger_record(
            item_id="ITEM-REDL-1",
            rel_url="/personal/u/Documents/PausedCourse/Week1.mp4",
            original_name="Week1.mp4",
            local_path=existing_file,
            size=1024,
        )
        self.store.ledger_request_redownload(key)

        events = []

        def fake_dl(_sess, _page, _site, file_rel, local_path, _log, **_kw):
            events.append(("priority_download", file_rel))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as fh:
                fh.write(b"\x00\x00\x00\x18ftypmp42" + b"1" * (1024 * 1024))
            return 1

        def fake_folder_sync(_f, _r, _s, cfg, **_kw):
            events.append(("folder_sync", cfg["name"]))
            return 0

        cfgs = [
            {"name": "Active First", "rel_url": "/personal/u/Documents/ActiveFirst", "local_path": self.tmp.name, "sync_enabled": True},
            {"name": "Paused Course", "rel_url": "/personal/u/Documents/PausedCourse", "local_path": paused_dir, "sync_enabled": False},
        ]

        with patch("sp_sync.sharepoint.sync_engine._download_one_file", side_effect=fake_dl), patch(
            "sp_sync.sharepoint.sync_engine.prime_sharepoint_session_for_browse"
        ), patch("sp_sync.sharepoint.sync_engine.start_sync_from_config", side_effect=fake_folder_sync), patch(
            "sp_sync.sharepoint.sync_engine._cleanup_playwright"
        ):
            ok, _msg, t = self.orch.start_sharepoint(
                fedauth="A" * 60, rtfa="B" * 25, site_url="https://t.sharepoint.com/personal/u", configs=cfgs
            )
            self.assertTrue(ok)
            t.join(timeout=3.0)

        # Priority re-download MUST happen BEFORE Active First folder sync, and Paused Course itself is NOT scanned
        self.assertEqual(
            events,
            [
                ("priority_download", "/personal/u/Documents/PausedCourse/Week1.mp4"),
                ("folder_sync", "Active First"),
            ],
        )
        # Re-download flag is cleared after successful priority download
        self.assertEqual(self.store.ledger_get(key)["redownload_requested"], 0)
        # Old copy was safely kept aside as *_old
        self.assertTrue(os.path.isfile(os.path.join(paused_dir, "lecture_old_old.mp4")))

    def test_recursive_folder_filters_excluded_upfront_and_prioritizes_redownload(self):
        from sp_sync.sharepoint.sync_engine import _sync_folder_recursive

        folder_dir = os.path.join(self.tmp.name, "CourseX")
        os.makedirs(folder_dir, exist_ok=True)

        # Already downloaded (should be filtered out up front — never passed to _download_one_file)
        self.store.ledger_record(
            item_id="ID-DONE",
            rel_url="/personal/u/Documents/CourseX/Done.mp4",
            original_name="Done.mp4",
            local_path=os.path.join(folder_dir, "Done.mp4"),
            size=5000,
        )
        # Queued for Re-download (appears LAST in server listing, MUST download FIRST!)
        redl_key = self.store.ledger_record(
            item_id="ID-REDL",
            rel_url="/personal/u/Documents/CourseX/Redo.mp4",
            original_name="Redo.mp4",
            local_path=os.path.join(folder_dir, "Redo.mp4"),
            size=5000,
        )
        self.store.ledger_request_redownload(redl_key)

        fake_listing = {
            "ok": True,
            "method": "rest",
            "folders": [{"name": "Forms", "rel_url": "/personal/u/Documents/CourseX/Forms"}],
            "files": [
                {"name": "Slides.pdf", "rel_url": "/personal/u/Documents/CourseX/Slides.pdf", "item_id": "ID-PDF", "size": 100},
                {"name": "Done.mp4", "rel_url": "/personal/u/Documents/CourseX/Done.mp4", "item_id": "ID-DONE", "size": 5000},
                {"name": "BrandNew.mp4", "rel_url": "/personal/u/Documents/CourseX/BrandNew.mp4", "item_id": "ID-NEW", "size": 7000},
                {"name": "Redo.mp4", "rel_url": "/personal/u/Documents/CourseX/Redo.mp4", "item_id": "ID-REDL", "size": 5000},
            ],
        }

        downloaded_order = []
        logs = []

        def fake_dl(_sess, _page, _site, file_rel, local_path, _log, **_kw):
            downloaded_order.append(os.path.basename(file_rel))
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as fh:
                fh.write(b"\x00\x00\x00\x18ftypmp42" + b"2" * (1024 * 1024))
            return 1

        stats = {"downloaded": 0, "skipped": 0, "failed": 0, "new_files": [], "errors": []}
        with patch("sp_sync.sharepoint.rest_folder_list.list_folder_children_rest", return_value=fake_listing), patch(
            "sp_sync.sharepoint.sync_engine._download_one_file", side_effect=fake_dl
        ):
            got = _sync_folder_recursive(
                session=None,
                page=None,
                sp_site_url="https://t.sharepoint.com/personal/u",
                folder_rel_url="/personal/u/Documents/CourseX",
                download_dir=folder_dir,
                log=logs.append,
                allow_non_mp4=False,  # video_only=True -> Slides.pdf filtered out UP FRONT!
                stats=stats,
            )

        self.assertEqual(got, 2)
        # Redo.mp4 (Queued for Re-download) MUST run strictly BEFORE BrandNew.mp4; Slides.pdf & Done.mp4 NEVER touched!
        self.assertEqual(downloaded_order, ["Redo.mp4", "BrandNew.mp4"])
        self.assertEqual(stats["skipped"], 2)  # 1 in ledger + 1 non-video excluded up front
        self.assertTrue(any("1 🔁 re-download, 1 new" in ln for ln in logs))
        self.assertFalse(any("Skip 'Done.mp4'" in ln for ln in logs))

    def test_single_folder_sync_only_runs_matching_priority_redownloads(self):
        other_dir = os.path.join(self.tmp.name, "OtherFolder")
        os.makedirs(other_dir, exist_ok=True)
        k_other = self.store.ledger_record(
            item_id="OTHER-1",
            rel_url="/personal/u/Documents/OtherFolder/Other.mp4",
            original_name="Other.mp4",
            local_path=os.path.join(other_dir, "Other.mp4"),
            size=1000,
        )
        self.store.ledger_request_redownload(k_other)

        dl_called = []

        def fake_dl(_s, _p, _site, file_rel, *_a, **_kw):
            dl_called.append(file_rel)
            return 1

        cfgs = [
            {"name": "Other", "rel_url": "/personal/u/Documents/OtherFolder", "local_path": other_dir},
            {"name": "Target", "rel_url": "/personal/u/Documents/TargetFolder", "local_path": os.path.join(self.tmp.name, "Target")},
        ]
        with patch("sp_sync.sharepoint.sync_engine._download_one_file", side_effect=fake_dl), patch(
            "sp_sync.sharepoint.sync_engine.start_sync_from_config", return_value=0
        ), patch("sp_sync.sharepoint.sync_engine._cleanup_playwright"):
            ok, _msg, t = self.orch.start_sharepoint(
                fedauth="A" * 60,
                rtfa="B" * 25,
                site_url="https://t.sharepoint.com/personal/u",
                configs=cfgs,
                config_index=1,  # Only TargetFolder requested!
            )
            self.assertTrue(ok)
            t.join(timeout=3.0)

        # OtherFolder's pending re-download must NOT be triggered when syncing only index 1
        self.assertEqual(dl_called, [])
        self.assertEqual(self.store.ledger_get(k_other)["redownload_requested"], 1)


if __name__ == "__main__":
    unittest.main()
