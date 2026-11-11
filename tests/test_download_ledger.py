"""Download ledger, v2 lecture naming + sequence, backfill matching and re-download."""

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.db.store import AppStore, ledger_key
from sp_sync.sharepoint import ledger
from sp_sync.sharepoint.naming import (
    build_lecture_filename,
    next_lecture_sequence,
    parse_lecture_filename,
    preferred_download_path,
    subject_from_folder,
)

SUBJ = "إدارة التحول الرقمي"
FOLDER = f"{SUBJ} MBA-09"


def _long(stamp):
    return (f"ماجستير مهنى في إدارة الاعمال عربى MBA - {FOLDER} - {FOLDER}-"
            f"{stamp}UTC-Meeting Recording.mp4")


def _touch(path, size=10):
    with open(path, "wb") as f:
        f.write(b"x" * size)
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppStore(os.path.join(self.tmp.name, "test_ledger.sqlite"))
        self.dir = os.path.join(self.tmp.name, FOLDER)
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, stamp, item_id=None, size=0):
        return {"name": _long(stamp), "rel_url": f"/sites/x/{FOLDER}/{_long(stamp)}",
                "item_id": item_id or f"ID{stamp}", "size": size, "modified": None}


class TestNamingV2(_Base):
    def test_subject_from_folder(self):
        self.assertEqual(subject_from_folder(f"/a/b/{FOLDER}"), SUBJ)
        self.assertEqual(subject_from_folder("محاسبة إدارية متقدمة MBA-04/"), "محاسبة إدارية متقدمة")
        self.assertEqual(subject_from_folder("Plain Folder"), "Plain Folder")

    def test_build_and_parse(self):
        from datetime import datetime
        n = build_lecture_filename(SUBJ, datetime(2026, 10, 10, 16, 43), 1)
        self.assertEqual(n, f"{SUBJ}_2026-10-10_01.mp4")
        self.assertEqual(parse_lecture_filename(n)["seq"], 1)
        self.assertIsNone(parse_lecture_filename("MBA-09_2026-10-10_1643.mp4"))

    def test_sequence_from_disk_and_ledger_names(self):
        _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4"))
        _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-17_02.mp4"))
        self.assertEqual(next_lecture_sequence(self.dir), 3)
        # a deleted file still counts through the ledger
        self.assertEqual(next_lecture_sequence(self.dir, [f"{SUBJ}_2026-10-24_05.mp4"]), 6)
        p = preferred_download_path(self.dir, _long("20261024_153151"))
        self.assertEqual(os.path.basename(p), f"{SUBJ}_2026-10-24_03.mp4")

    def test_non_video_keeps_original_name(self):
        p = preferred_download_path(self.dir, "Syllabus.pdf")
        self.assertEqual(os.path.basename(p), "Syllabus.pdf")


class TestLedger(_Base):
    def test_skip_when_in_ledger_even_if_deleted(self):
        r = self.row("20261010_164311")
        path = _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4"))
        self.store.ledger_record(item_id=r["item_id"], rel_url=r["rel_url"], original_name=r["name"],
                                 local_path=path)
        os.remove(path)
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))
        # renamed / moved elsewhere: still skipped
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))

    def test_key_falls_back_to_rel_url(self):
        self.assertEqual(ledger_key(None, "/a/b.mp4"), "url:/a/b.mp4")
        self.assertEqual(ledger_key("X1", "/a/b.mp4"), "id:X1")
        r = self.row("20261010_164311")
        r["item_id"] = None
        self.store.ledger_record(item_id=None, rel_url=r["rel_url"], original_name=r["name"],
                                 local_path=os.path.join(self.dir, "a.mp4"))
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))

    def test_new_download_gets_next_sequence_and_is_recorded(self):
        _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4"))
        r = self.row("20261017_154834")
        target = ledger.plan_download(self.store, self.dir, r)
        self.assertEqual(os.path.basename(target), f"{SUBJ}_2026-10-17_02.mp4")
        _touch(target)
        self.assertTrue(ledger.record_after_download(self.store, r, target, 1, False))
        self.assertEqual(self.store.ledger_get(ledger_key(r["item_id"], None))["source"], "download")
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))

    def test_failed_download_not_recorded(self):
        r = self.row("20261017_154834")
        target = ledger.plan_download(self.store, self.dir, r)
        self.assertFalse(ledger.record_after_download(self.store, r, target, 0, False))
        self.assertIsNone(self.store.ledger_get(ledger_key(r["item_id"], None)))

    def test_redownload_request_keeps_old_copy(self):
        r = self.row("20261010_164311")
        path = _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4"))
        key = self.store.ledger_record(item_id=r["item_id"], rel_url=r["rel_url"],
                                       original_name=r["name"], local_path=path)
        self.assertTrue(self.store.ledger_request_redownload(key))
        target = ledger.plan_download(self.store, self.dir, r)
        self.assertEqual(target, os.path.abspath(path))
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01_old.mp4")))
        _touch(target)
        ledger.record_after_download(self.store, r, target, 1, False)
        self.assertEqual(self.store.ledger_get(key)["redownload_requested"], 0)
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))


class TestBackfill(_Base):
    def test_old_long_and_short_names(self):
        _touch(os.path.join(self.dir, _long("20261010_164311")))
        _touch(os.path.join(self.dir, "MBA-09_2026-10-17_1548.mp4"))
        rows = [self.row("20261010_164311"), self.row("20261017_154834")]
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, rows), 2)
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, rows), 0)
        for r in rows:
            self.assertIsNone(ledger.plan_download(self.store, self.dir, r))

    def test_v2_names_same_day_matched_in_order(self):
        a = _touch(os.path.join(self.dir, f"{SUBJ}_2026-11-07_05.mp4"), 5)
        b = _touch(os.path.join(self.dir, f"{SUBJ}_2026-11-07_06.mp4"), 6)
        late = self.row("20261107_171300", size=999)   # trimmed: sizes differ
        early = self.row("20261107_131300", size=998)
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, [late, early]), 2)
        self.assertEqual(self.store.ledger_get(ledger_key(early["item_id"], None))["local_path"], a)
        self.assertEqual(self.store.ledger_get(ledger_key(late["item_id"], None))["local_path"], b)

    def test_size_match_wins(self):
        a = _touch(os.path.join(self.dir, f"{SUBJ}_2026-11-07_01.mp4"), 7)
        early = self.row("20261107_131300", size=50)
        late = self.row("20261107_171300", size=7)
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, [early, late]), 1)
        self.assertEqual(self.store.ledger_get(ledger_key(late["item_id"], None))["local_path"], a)
        self.assertIsNone(self.store.ledger_get(ledger_key(early["item_id"], None)))

    def test_ambiguous_not_matched(self):
        _touch(os.path.join(self.dir, f"{SUBJ}_2026-11-07_01.mp4"), 3)
        rows = [self.row("20261107_131300", size=50), self.row("20261107_171300", size=60)]
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, rows), 0)

    def test_unmatched_remote_is_downloaded(self):
        rows = [self.row("20261114_131300")]
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, rows), 0)
        self.assertIsNotNone(ledger.plan_download(self.store, self.dir, rows[0]))


class TestRecordingsFolder(_Base):
    """Videos live in <subject>/تسجيلات; the old top level is still recognised."""

    def setUp(self):
        super().setUp()
        self.rec = os.path.join(self.dir, "تسجيلات")

    def test_new_video_goes_to_recordings_and_pdf_stays(self):
        p = ledger.plan_download(self.store, self.dir, self.row("20261114_131300"))
        self.assertEqual(os.path.dirname(p), self.rec)
        self.assertTrue(os.path.isdir(self.rec))
        self.assertEqual(os.path.basename(p), f"{SUBJ}_2026-11-14_01.mp4")
        self.assertEqual(os.path.dirname(preferred_download_path(self.dir, "Slides.pdf")), self.dir)

    def test_sequence_counts_both_locations(self):
        os.makedirs(self.rec)
        _touch(os.path.join(self.rec, f"{SUBJ}_2026-10-10_01.mp4"))
        _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-17_02.mp4"))
        self.assertEqual(next_lecture_sequence(self.dir), 3)
        p = ledger.plan_download(self.store, self.dir, self.row("20261024_153151"))
        self.assertEqual(p, os.path.join(self.rec, f"{SUBJ}_2026-10-24_03.mp4"))

    def test_old_short_name_in_either_place_not_redownloaded(self):
        os.makedirs(self.rec)
        a = _touch(os.path.join(self.rec, "MBA-09_2026-10-10_1643.mp4"))
        b = _touch(os.path.join(self.dir, _long("20261017_154834")))
        self.assertEqual(preferred_download_path(self.dir, _long("20261010_164311")), a)
        self.assertEqual(preferred_download_path(self.dir, _long("20261017_154834")), b)

    def test_backfill_finds_v2_names_in_recordings(self):
        os.makedirs(self.rec)
        a = _touch(os.path.join(self.rec, f"{SUBJ}_2026-10-10_01.mp4"))
        r = self.row("20261010_164311", size=999)
        self.assertEqual(ledger.backfill_folder(self.store, self.dir, [r]), 1)
        self.assertEqual(self.store.ledger_get(ledger_key(r["item_id"], None))["local_path"], a)

    def test_ledger_path_fallback_after_move(self):
        old = _touch(os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4"))
        r = self.row("20261010_164311")
        key = self.store.ledger_record(item_id=r["item_id"], rel_url=r["rel_url"],
                                       original_name=r["name"], local_path=old)
        os.makedirs(self.rec)
        moved = os.path.join(self.rec, os.path.basename(old))
        os.rename(old, moved)
        self.assertEqual(ledger.resolve_ledger_path(old), moved)
        self.assertIsNone(ledger.plan_download(self.store, self.dir, r))
        # re-download of a moved file targets the recordings folder and keeps the copy
        self.store.ledger_request_redownload(key)
        target = ledger.plan_download(self.store, self.dir, r)
        self.assertEqual(target, moved)
        self.assertTrue(os.path.exists(os.path.join(self.rec, f"{SUBJ}_2026-10-10_01_old.mp4")))

    def test_redownload_of_deleted_file_goes_to_recordings(self):
        r = self.row("20261010_164311")
        old = os.path.join(self.dir, f"{SUBJ}_2026-10-10_01.mp4")
        key = self.store.ledger_record(item_id=r["item_id"], rel_url=r["rel_url"],
                                       original_name=r["name"], local_path=old)
        self.store.ledger_request_redownload(key)
        self.assertEqual(ledger.plan_download(self.store, self.dir, r),
                         os.path.join(self.rec, f"{SUBJ}_2026-10-10_01.mp4"))


class TestLedgerRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_db_env = os.environ.get("SP_SYNC_DB_PATH")
        os.environ["SP_SYNC_DB_PATH"] = os.path.join(self.tmp.name, "test_web_ledger.sqlite")
        from sp_sync.config.settings import clear_settings_cache
        clear_settings_cache()
        import sp_sync.db.store as store_mod
        store_mod._store_singleton = None
        from sp_sync.web.app import app
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = store_mod.get_store()
        d = os.path.join(self.tmp.name, FOLDER)
        os.makedirs(d)
        p = _touch(os.path.join(d, f"{SUBJ}_2026-10-10_01.mp4"))
        self.key = self.store.ledger_record(item_id="A1", rel_url="/x", original_name=_long("20261010_164311"),
                                            local_path=p, size=10)

    def tearDown(self):
        import sp_sync.db.store as store_mod
        from sp_sync.config.settings import clear_settings_cache
        if self._old_db_env is None:
            os.environ.pop("SP_SYNC_DB_PATH", None)
        else:
            os.environ["SP_SYNC_DB_PATH"] = self._old_db_env
        clear_settings_cache()
        store_mod._store_singleton = None
        self.tmp.cleanup()

    def test_page_and_api_grouped(self):
        res = self.client.get("/ledger")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn(SUBJ, html)
        self.assertIn("تحميل من جديد", html)
        data = self.client.get("/api/ledger").get_json()
        self.assertEqual(data["groups"][0]["subject"], SUBJ)
        self.assertTrue(data["groups"][0]["items"][0]["local_exists"])
        self.assertEqual(data["groups"][0]["items"][0]["seq"], 1)

    def test_page_finds_file_moved_to_recordings(self):
        d = os.path.join(self.tmp.name, FOLDER)
        rec = os.path.join(d, "تسجيلات")
        os.makedirs(rec)
        os.rename(os.path.join(d, f"{SUBJ}_2026-10-10_01.mp4"), os.path.join(rec, f"{SUBJ}_2026-10-10_01.mp4"))
        p2 = _touch(os.path.join(rec, f"{SUBJ}_2026-10-17_02.mp4"))
        self.store.ledger_record(item_id="A2", rel_url="/y", original_name=_long("20261017_154834"),
                                 local_path=p2, size=10)
        groups = self.client.get("/api/ledger").get_json()["groups"]
        self.assertEqual(len(groups), 1)  # recordings folder grouped under its subject
        self.assertEqual([it["seq"] for it in groups[0]["items"]], [1, 2])
        self.assertTrue(all(it["local_exists"] for it in groups[0]["items"]))

    def test_redownload_endpoint(self):
        res = self.client.post("/api/ledger/redownload", json={"item_key": self.key})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.store.ledger_get(self.key)["redownload_requested"], 1)
        res = self.client.post("/api/ledger/redownload", json={"item_key": self.key, "cancel": True})
        self.assertEqual(self.store.ledger_get(self.key)["redownload_requested"], 0)
        self.assertEqual(self.client.post("/api/ledger/redownload", json={"item_key": "id:nope"}).status_code, 404)
        self.assertEqual(self.client.post("/api/ledger/redownload", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
