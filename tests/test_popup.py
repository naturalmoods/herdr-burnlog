"""Exercise the actual terminal lifecycle, not just pane-open acceptance."""
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time
import unittest


class PopupTests(unittest.TestCase):
    def test_popup_keeps_report_and_error_visible_until_enter(self):
        script = Path(__file__).resolve().parents[1] / "burnlog.py"
        with tempfile.TemporaryDirectory() as state:
            for command, status in (("projects", 0), ("current", 2)):
                with self.subTest(command=command):
                    master, slave = pty.openpty()
                    env = os.environ | {
                        "HERDR_PLUGIN_STATE_DIR": state,
                        "HERDR_PLUGIN_ID": "herdr-burnlog",
                        "HERDR_PLUGIN_ENTRYPOINT_ID": command,
                        "HERDR_PLUGIN_CONTEXT_JSON": "{}",
                    }
                    process = subprocess.Popen(
                        ["python3", str(script), command], env=env,
                        stdin=slave, stdout=slave, stderr=slave)
                    os.close(slave)
                    try:
                        output = b""
                        deadline = time.monotonic() + 10
                        while b"Press Enter to close." not in output:
                            remaining = deadline - time.monotonic()
                            self.assertGreater(remaining, 0, output)
                            self.assertTrue(select.select([master], [], [], remaining)[0])
                            output += os.read(master, 65536)
                        self.assertIsNone(process.poll(), output)
                        self.assertIn(b"PROJECT" if status == 0 else b"burnlog:", output)
                        os.write(master, b"\n")
                        self.assertEqual(status, process.wait(timeout=5))
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                        os.close(master)
