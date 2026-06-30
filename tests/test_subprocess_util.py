"""Tests for timeout-safe subprocess utilities."""

from __future__ import annotations

import os
import subprocess
import unittest

from cc_loop.subprocess_util import run_with_timeout


class SubprocessUtilTests(unittest.TestCase):
    def test_successful_command(self) -> None:
        result = run_with_timeout(["python3", "-c", "print('ok')"], timeout_seconds=5)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertIn("ok", result.stdout)

    def test_timeout_terminates_process_group(self) -> None:
        script = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: None)\n"
            "time.sleep(30)\n"
        )
        result = run_with_timeout(["python3", "-c", script], timeout_seconds=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, -1)

    def test_runs_in_new_session(self) -> None:
        script = "import os; print(os.getpgid(os.getpid()))"
        parent_pgid = os.getpgid(os.getpid())
        result = run_with_timeout(["python3", "-c", script], timeout_seconds=5)
        child_pgid = int(result.stdout.strip())
        self.assertNotEqual(child_pgid, parent_pgid)


if __name__ == "__main__":
    unittest.main()
