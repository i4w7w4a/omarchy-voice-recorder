"""Exercise the CLI without requiring a microphone, model download or audio server."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

CLI = Path(__file__).resolve().parents[1] / "scripts/recorder.py"

# A synthetic 48 kHz source with the same SIGINT/exit contract as pw-record.
FAKE_RECORD = """
import math, os, pathlib, signal, struct, sys, time, wave
running = True
def stop(*_):
    global running
    running = False
signal.signal(signal.SIGINT, stop)
pathlib.Path(os.environ["XDG_RUNTIME_DIR"], "controller-pid").write_text(str(os.getppid()))
frames = b"".join(struct.pack("<h", int(3000 * math.sin(i * math.pi / 24))) for i in range(960))
with wave.open(sys.argv[-1], "wb") as output:
    output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
    while running:
        output.writeframes(frames)
        time.sleep(0.02)
sys.exit(1)
"""

FAKE_ENHANCE = """
import os, pathlib, shutil, sys
runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
(runtime / "strength").write_text(sys.argv[-1])
if (runtime / "fail-engine").exists():
    sys.exit(1)
shutil.copyfile(sys.argv[2], sys.argv[3])
"""


class RecorderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        binary = self.root / "bin"
        binary.mkdir()
        self.runtime = self.root / "run"
        self.runtime.mkdir()
        models = self.root / "models"
        models.mkdir()
        self.weights = models / "dpdfnet2_48khz_hr.onnx"
        self.weights.write_bytes(b"fixture")
        self.env = dict(os.environ, HOME=str(self.root), PATH=str(binary),
                        XDG_CONFIG_HOME=str(self.root / "config"), XDG_DATA_HOME=str(self.root / "data"),
                        XDG_RUNTIME_DIR=str(self.runtime), DPDFNET_MODEL_DIR=str(models))
        for name, body in [("pw-record", FAKE_RECORD), ("dpdfnet", FAKE_ENHANCE)]:
            path = binary / name
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o700)
        self.addCleanup(self.stop_worker)

    def call(self, *args):
        data = subprocess.check_output([sys.executable, CLI, *args], env=self.env, timeout=5)
        return json.loads(data)

    def wait_for(self, *phases):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            state = self.call("status")
            if state["phase"] in phases:
                return state
            time.sleep(0.04)
        self.fail(f"Expected {phases}, last state was {state}")

    def stop_worker(self):
        self.call("stop")
        self.wait_for("ready", "idle", "error")

    def test_concurrent_start_and_normal_stop(self):
        chosen = self.root / "Звук & Voice"
        chosen.mkdir()
        self.call("set-folder", str(chosen))
        self.call("set-strength", "6")
        with ThreadPoolExecutor(6) as pool:
            starts = list(pool.map(lambda _: self.call("start"), range(6)))
        self.assertEqual(len({s["take"] for s in starts}), 1)
        recording = self.wait_for("recording")
        self.assertGreater(recording["startedAt"], 0)
        self.assertFalse(recording["original"])
        self.assertEqual(self.call("set-strength", "24")["strength"], 6)
        self.call("stop")
        self.call("stop")
        ready = self.wait_for("ready")
        self.assertGreater(ready["duration"], 0)
        self.assertEqual(Path(ready["cleaned"]).read_bytes(), Path(ready["original"]).read_bytes())
        self.assertEqual((self.runtime / "strength").read_text(), "6")
        self.assertEqual(len(list(chosen.iterdir())), 1)
        # Runtime state is ephemeral. Saved settings and recordings survive login.
        shutil.rmtree(self.runtime / "omarchy-voice-recorder")
        self.assertEqual(self.call("status"), ready)

    def test_retry_after_engine_error_and_folder_change(self):
        (self.runtime / "fail-engine").touch()
        self.call("start")
        self.wait_for("recording")
        self.call("stop")
        failed = self.wait_for("error")
        self.assertEqual(failed["error"], "enhance")
        self.assertTrue(Path(failed["original"]).is_file())
        self.assertEqual(failed["cleaned"], "")
        chosen = self.root / "other"
        chosen.mkdir()
        self.assertEqual(self.call("set-folder", str(chosen))["take"], failed["take"])
        self.call("set-strength", "24")
        (self.runtime / "fail-engine").unlink()
        self.call("retry")
        ready = self.wait_for("ready")
        self.assertEqual(Path(ready["cleaned"]).parent, Path(failed["take"]))
        self.assertEqual(ready["folder"], str(chosen))
        self.assertEqual((self.runtime / "strength").read_text(), "24")

    def test_dead_controller_releases_microphone_and_preserves_source(self):
        self.call("start")
        self.wait_for("recording")
        pid = int((self.runtime / "controller-pid").read_text())
        os.kill(pid, signal.SIGKILL)
        orphan = self.wait_for("error")
        self.assertEqual(orphan["error"], "interrupted")
        self.assertTrue(orphan["original"])
        self.call("retry")
        self.wait_for("ready")

    def test_missing_model_and_read_only_status(self):
        self.weights.unlink()
        missing = self.call("start")
        self.assertEqual(missing["error"], "model")
        self.assertFalse(missing["take"])
        state_file = self.runtime / "omarchy-voice-recorder/state.json"
        when = state_file.stat().st_mtime_ns
        self.call("status")
        self.assertEqual(state_file.stat().st_mtime_ns, when)

    def test_missing_selected_folder_is_not_recreated(self):
        disk = self.root / "external"
        chosen = disk / "Recordings"
        chosen.mkdir(parents=True)
        self.call("set-folder", str(chosen))
        disk.rename(self.root / "unmounted")
        state = self.call("start")
        self.assertEqual((state["phase"], state["error"]), ("error", "folder"))
        self.assertEqual(state["folder"], str(chosen))
        self.assertFalse(disk.exists())
        self.assertFalse((self.runtime / "controller-pid").exists())

    def test_unmounted_volume_root_is_not_used_as_a_folder(self):
        mount = self.root / "mount"
        mount.mkdir()
        self.call("set-folder", str(mount))
        # Reproduce the saved setting for a previously mounted volume root.
        path = self.root / "config/omarchy-voice-recorder/settings.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text()), mountRoot=True)))
        self.assertEqual(self.call("start")["error"], "folder")
        self.assertEqual(list(mount.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
