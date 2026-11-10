"""Single-instance lock and stale-lock handling."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sp_sync.single_instance import (
    acquire_app_lock,
    port_in_use,
    release_app_lock,
)


class TestSingleInstance(unittest.TestCase):
    def test_acquire_and_block_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "app.lock")
            ok, msg = acquire_app_lock(lock_path=lock, check_port=False)
            self.assertTrue(ok, msg)
            self.assertTrue(os.path.isfile(lock))

            # Same process re-acquire is idempotent.
            ok_same, msg_same = acquire_app_lock(lock_path=lock, check_port=False)
            self.assertTrue(ok_same, msg_same)
            self.assertIn("Already holding", msg_same)

            # Simulate another live PID holding the lock.
            with open(lock, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
            with patch("sp_sync.single_instance.os.getpid", return_value=os.getpid() + 1), patch(
                "sp_sync.single_instance._pid_is_alive", return_value=True
            ):
                ok2, msg2 = acquire_app_lock(lock_path=lock, check_port=False)
            self.assertFalse(ok2)
            self.assertIn("already running", msg2)

            # Restore real ownership then release.
            with open(lock, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
            release_app_lock(lock)
            self.assertFalse(os.path.exists(lock))

            ok3, msg3 = acquire_app_lock(lock_path=lock, check_port=False)
            self.assertTrue(ok3, msg3)
            release_app_lock(lock)

    def test_stale_lock_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "app.lock")
            with open(lock, "w", encoding="utf-8") as f:
                f.write("999999\n")  # almost certainly dead
            ok, msg = acquire_app_lock(lock_path=lock, check_port=False)
            self.assertTrue(ok, msg)
            with open(lock, encoding="utf-8") as f:
                self.assertEqual(int(f.readline().strip()), os.getpid())
            release_app_lock(lock)

    def test_port_in_use_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "app.lock")
            with patch("sp_sync.single_instance.port_in_use", return_value=True):
                ok, msg = acquire_app_lock(
                    lock_path=lock, host="127.0.0.1", port=5000, check_port=True
                )
            self.assertFalse(ok)
            self.assertIn("5000", msg)
            self.assertFalse(os.path.exists(lock))

    def test_port_in_use_helper_free_port(self):
        # Bind an ephemeral listener then check that port reports in use;
        # a high unused port should be free.
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            self.assertTrue(port_in_use("127.0.0.1", port))
        finally:
            sock.close()
        self.assertFalse(port_in_use("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
