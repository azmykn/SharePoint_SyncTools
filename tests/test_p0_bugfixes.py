"""Regression tests for all P0 bugs fixed across SharePoint, Ledger, and Google Drive."""

import base64
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.db.store import AppStore
from sp_sync.gdrive.service import _safe_child_path, _safe_local_name, download_public_gdrive_file, sync_gdrive_folder
from sp_sync.sharepoint import ledger
from sp_sync.sharepoint.rest_folder_list import (
    _file_rows,
    encode_path_for_get_folder_param,
    list_folder_children_rest,
)
from sp_sync.sharepoint.sync_engine import (
    _download_one_file,
    _flush_capture_buffers,
    _stream_response_to_file,
)


class TestP0SharePointFixes(unittest.TestCase):
    def test_file_rows_coerces_string_length_and_extracts_unique_id(self):
        rows = _file_rows(
            [
                {
                    "Name": "slides.pdf",
                    "ServerRelativeUrl": "/sites/x/slides.pdf",
                    "Length": "204800",
                    "UniqueId": "abc-123-uid",
                    "TimeLastModified": "2026-11-10T10:00:00Z",
                }
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["size"], int)
        self.assertEqual(rows[0]["size"], 204800)
        self.assertEqual(rows[0]["item_id"], "abc-123-uid")

    def test_encode_path_escapes_odata_single_quote(self):
        enc = encode_path_for_get_folder_param("/sites/x/Dean's Office/Notes")
        self.assertIn("%27%27", enc)

    def test_download_one_file_handles_string_remote_size_without_typeerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "syllabus.pdf")
            with open(p, "wb") as f:
                f.write(b"%PDF-1.4" + b"0" * 92)
            logs = []
            # Previously crashed with TypeError: '>' not supported between 'str' and 'int'
            res = _download_one_file(
                session=MagicMock(),
                page=None,
                sp_site_url="https://contoso.sharepoint.com/sites/x",
                file_rel_url="/sites/x/syllabus.pdf",
                local_path=p,
                log=logs.append,
                allow_non_mp4=True,
                remote_size="100",
            )
            self.assertEqual(res, 0)
            self.assertTrue(any("Skip" in m for m in logs))

    def test_stream_response_rejects_truncated_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = os.path.join(tmp, "file.pdf.part")
            fake_resp = SimpleNamespace(
                headers={"Content-Length": "1000"},
                iter_content=lambda chunk_size=65536: iter([b"x" * 200]),
                close=lambda: None,
            )
            ok, dl, reason = _stream_response_to_file(fake_resp, part, 1000, is_video=False)
            self.assertFalse(ok)
            self.assertEqual(dl, 200)
            self.assertIn("truncated", reason)

    def test_stream_response_rejects_non_mp4_for_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = os.path.join(tmp, "vid.mp4.part")
            fake_resp = SimpleNamespace(
                headers={"Content-Length": "500"},
                iter_content=lambda chunk_size=65536: iter([b"not-a-valid-mp4" * 34][:1]),
                close=lambda: None,
            )
            ok, _dl, reason = _stream_response_to_file(fake_resp, part, 500, is_video=True)
            self.assertFalse(ok)
            self.assertIn("MP4", reason)

    def test_flush_capture_buffers_frees_js_memory_slots(self):
        # Simulate browser page object verifying buf[i] = null happens during flush
        buf = {
            "video": [{"b64": base64.b64encode(b"VID1").decode()}, {"b64": base64.b64encode(b"VID2").decode()}],
            "audio": [{"b64": base64.b64encode(b"AUD1").decode()}],
        }

        class FakePage:
            def evaluate(self, script, arg=None):
                if ".length" in script:
                    t = "video" if ".video." in script else "audio"
                    return len(buf[t])
                t, s, e = arg
                out = []
                for i in range(s, e):
                    if buf[t][i] and buf[t][i].get("b64"):
                        out.append(buf[t][i]["b64"])
                    buf[t][i] = None
                return out

        with tempfile.TemporaryDirectory() as tmp:
            vf = os.path.join(tmp, "v.fmp4")
            af = os.path.join(tmp, "a.fmp4")
            cursors = {"video": 0, "audio": 0}
            written = _flush_capture_buffers(FakePage(), vf, af, cursors, log=lambda *_: None)
            self.assertEqual(written["video"], 8)
            self.assertEqual(written["audio"], 4)
            self.assertEqual(buf["video"], [None, None])
            self.assertEqual(buf["audio"], [None])
            with open(vf, "rb") as f:
                self.assertEqual(f.read(), b"VID1VID2")

    def test_backfill_mixed_naive_and_aware_datetimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AppStore(os.path.join(tmp, "ledger.sqlite"))
            subj_dir = os.path.join(tmp, "Course MBA-09")
            os.makedirs(subj_dir)
            for seq in ("01", "02"):
                with open(os.path.join(subj_dir, f"Course_2026-11-10_{seq}.mp4"), "wb") as f:
                    f.write(b"x" * 100)
            # Row 1 has naive timestamp parsed from filename; Row 2 has UTC ISO-8601 aware modified time
            rows = [
                {
                    "name": "Lecture_2026-11-10_0800.mp4",
                    "rel_url": "/sites/x/l1.mp4",
                    "item_id": "ID1",
                    "size": 500,
                    "modified": None,
                },
                {
                    "name": "NoDateInTitle.mp4",
                    "rel_url": "/sites/x/l2.mp4",
                    "item_id": "ID2",
                    "size": 600,
                    "modified": "2026-11-10T10:00:00Z",
                },
            ]
            recorded = ledger.backfill_folder(store, subj_dir, rows, log=lambda *_: None)
            self.assertEqual(recorded, 2)

    def test_v2_shares_does_not_mutate_session_headers(self):
        class Sess:
            def __init__(self):
                self.headers = {"Accept": "application/json;odata=verbose"}
                self.seen_headers = []

            def get(self, url, headers=None, timeout=None, **kwargs):
                self.seen_headers.append(headers)
                r = SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"}, text='{"value":[]}')
                r.json = lambda: {"value": []}
                return r

        s = Sess()
        out = list_folder_children_rest(
            s,
            "https://tenant-my.sharepoint.com/personal/u",
            "/personal/u/Documents/T",
            sharing_link_url="https://tenant-my.sharepoint.com/:f:/g/personal/u/tok?e=1",
        )
        self.assertTrue(out["ok"])
        self.assertEqual(s.headers["Accept"], "application/json;odata=verbose")
        self.assertEqual(s.seen_headers[0], {"Accept": "application/json"})


class TestP0GoogleDriveFixes(unittest.TestCase):
    def test_safe_name_and_path_traversal_guard(self):
        cleaned = _safe_local_name("../evil.txt")
        self.assertEqual(cleaned, "_evil.txt")
        self.assertNotIn("..", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertEqual(_safe_local_name(".."), "Untitled")
        with tempfile.TemporaryDirectory() as tmp:
            p = _safe_child_path(tmp, "../../escape")
            self.assertTrue(os.path.commonpath([os.path.abspath(tmp), p]) == os.path.abspath(tmp))

    def test_public_download_does_not_read_text_for_binary_stream(self):
        class FakeBinaryResp:
            status_code = 200
            cookies = {}
            headers = {"Content-Type": "application/octet-stream", "Content-Length": "6"}

            @property
            def text(self):
                raise AssertionError("res.text must NEVER be accessed on binary stream!")

            def iter_content(self, chunk_size=65536):
                yield b"BINARY"

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "bin.dat")
            with patch("sp_sync.gdrive.service.requests.Session.get", return_value=FakeBinaryResp()):
                ok = download_public_gdrive_file("FILE123", out, log=lambda *_: None)
            self.assertTrue(ok)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"BINARY")

    def test_sync_gdrive_shortcut_cycle_does_not_recurse_infinitely(self):
        calls = []

        def fake_list(_creds, fid, **_kw):
            calls.append(fid)
            # Folder A points to Folder B, Folder B points back to Folder A
            nxt = "B" if fid == "A" else "A"
            return [{"id": nxt, "name": f"Link_{nxt}", "mimeType": "application/vnd.google-apps.folder"}]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sp_sync.gdrive.service.build", return_value=MagicMock()
        ), patch("sp_sync.gdrive.service.list_folder_contents", side_effect=fake_list):
            sync_gdrive_folder(MagicMock(), "A", tmp, log_callback=lambda *_: None)
        self.assertEqual(calls, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
