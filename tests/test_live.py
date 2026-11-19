"""Exercise microphone lifecycle and PipeWire worker protocol without opening a device."""
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

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FAKE_AUDIO = """
import argparse, os, pathlib, signal, sys, time
root = pathlib.Path(os.environ['XDG_RUNTIME_DIR'])
parser = argparse.ArgumentParser()
parser.add_argument('--model')
parser.add_argument('--strength')
parser.add_argument('--device', choices=('cpu', 'auto', 'cuda'), required=True)
args = parser.parse_args()
(root / 'audio-pid').write_text(str(os.getpid()))
(root / 'supervisor-pid').write_text(str(os.getppid()))
with (root / 'starts').open('a') as f: print(args.model, args.strength, file=f)
while (root / 'delay').exists(): time.sleep(.02)
if args.device != 'cpu' and (root / 'broken-gpu').exists():
    print('ERROR gpu', flush=True)
    sys.exit(1)
model = 'baseline' if (root / 'fallback').exists() else args.model
device = 'cpu' if args.device == 'cpu' else 'cuda'
print('READY', model, device, flush=True)
while not (root / 'overload').exists(): time.sleep(.02)
print('ERROR overload', flush=True)
"""


class LiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scripts = self.root / "scripts"
        shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
        (scripts / "live_audio.py").write_text(FAKE_AUDIO)
        self.cli = scripts / "live.py"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        data = self.root / "data"
        binary = data / "dpdfnet/venv/bin"
        binary.mkdir(parents=True)
        (binary / "python").symlink_to(sys.executable)
        (binary / "dpdfnet").touch(mode=0o700)
        self.models = self.root / "models"
        self.models.mkdir()
        (self.models / "baseline.onnx").write_bytes(b"fixture")
        config = self.root / "config"
        self.prefs = config / "omarchy-voice-recorder/settings.json"
        self.prefs.parent.mkdir(parents=True)
        self.save_settings("baseline", 12)
        self.env = dict(os.environ, HOME=str(self.root), XDG_RUNTIME_DIR=str(self.runtime),
                        XDG_CONFIG_HOME=str(config), XDG_DATA_HOME=str(data), DPDFNET_MODEL_DIR=str(self.models))
        self.addCleanup(self.shutdown)

    def save_settings(self, model, strength, device="auto"):
        self.prefs.write_text(json.dumps(dict(model=model, strength=strength, device=device)))

    def install_gpu(self):
        env = self.root / "data/dpdfnet/gpu-venv"
        (env / "bin").mkdir(parents=True)
        (env / "bin/python").symlink_to(sys.executable)
        (env / ".cuda-ready").touch()

    def call(self, verb):
        return json.loads(subprocess.check_output([sys.executable, self.cli, verb], env=self.env, timeout=4))

    def await_phase(self, phase):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.call("status")
            if snapshot["phase"] == phase:
                return snapshot
            time.sleep(.05)
        self.fail(f"Expected {phase}: {snapshot}")

    def shutdown(self):
        self.call("stop")
        self.await_phase("off")

    def test_simultaneous_start_and_stop(self):
        self.assertEqual(self.call("status")["phase"], "off")
        with ThreadPoolExecutor(4) as workers:
            list(workers.map(lambda _: self.call("start"), range(4)))
        self.assertEqual(self.await_phase("on")["strength"], 12)
        self.assertEqual((self.runtime / "starts").read_text().splitlines(), ["baseline 12"])
        self.save_settings("dpdfnet4", 6)
        self.assertEqual(self.call("status")["model"], "baseline")
        self.assertEqual(self.call("start")["strength"], 12)
        with ThreadPoolExecutor(3) as workers:
            list(workers.map(lambda _: self.call("stop"), range(3)))
        self.assertEqual(self.await_phase("off")["model"], "dpdfnet4")
        self.assertEqual(self.call("start")["error"], "model")
        self.assertEqual(self.call("stop")["phase"], "off")

    def test_cancel_starting(self):
        (self.runtime / "delay").touch()
        self.assertEqual(self.call("start")["phase"], "starting")
        self.assertIn(self.call("stop")["phase"], ("stopping", "off"))
        self.await_phase("off")

    def test_worker_fallback_failure_and_retry(self):
        (self.models / "dpdfnet8_48khz_hr.onnx").write_bytes(b"fixture")
        self.save_settings("dpdfnet8_48khz_hr", 24)
        (self.runtime / "fallback").touch()
        self.call("start")
        active = self.await_phase("on")
        self.assertEqual((active["model"], active["requestedModel"]), ("baseline", "dpdfnet8_48khz_hr"))
        self.assertEqual((active["requestedDevice"], active["device"]), ("auto", "cpu"))
        (self.runtime / "overload").touch()
        self.assertEqual(self.await_phase("error")["error"], "overload")
        (self.runtime / "overload").unlink()
        self.call("start")
        self.await_phase("on")

    def test_gpu_unavailable_and_ready_marker(self):
        self.save_settings("baseline", 12, "cuda")
        self.assertEqual(self.call("start")["error"], "gpu")
        self.assertFalse((self.runtime / "starts").exists())
        self.install_gpu()
        marker = self.root / "data/dpdfnet/gpu-venv/.cuda-ready"
        marker.unlink()
        self.assertEqual(self.call("start")["error"], "gpu")
        marker.touch()
        self.call("start")
        active = self.await_phase("on")
        self.assertEqual((active["requestedDevice"], active["device"]), ("cuda", "cuda"))
        self.shutdown()
        self.save_settings("baseline", 6, "cpu")
        self.call("start")
        active = self.await_phase("on")
        self.assertEqual((active["requestedDevice"], active["device"]), ("cpu", "cpu"))

    def test_auto_retries_broken_gpu_but_explicit_selection_fails(self):
        self.install_gpu()
        (self.runtime / "broken-gpu").touch()
        self.call("start")
        active = self.await_phase("on")
        self.assertEqual((active["requestedDevice"], active["device"]), ("auto", "cpu"))
        self.assertEqual(len((self.runtime / "starts").read_text().splitlines()), 2)
        self.shutdown()
        self.save_settings("baseline", 12, "cuda")
        self.call("start")
        self.assertEqual(self.await_phase("error")["error"], "gpu")
        self.assertEqual(len((self.runtime / "starts").read_text().splitlines()), 3)

    def test_explicit_cuda_refuses_substituted_model(self):
        self.install_gpu()
        (self.models / "dpdfnet8_48khz_hr.onnx").write_bytes(b"fixture")
        self.save_settings("dpdfnet8_48khz_hr", 12, "cuda")
        (self.runtime / "fallback").touch()
        self.call("start")
        self.assertEqual(self.await_phase("error")["error"], "overload")

    def test_dead_supervisor_stops_worker_and_releases_lease(self):
        self.call("start")
        self.await_phase("on")
        child = int((self.runtime / "audio-pid").read_text())
        supervisor = int((self.runtime / "supervisor-pid").read_text())
        os.kill(supervisor, signal.SIGKILL)
        self.assertEqual(self.await_phase("error")["error"], "stream")
        # A zombie has closed its descriptors and no longer captures audio.
        procstat = Path(f"/proc/{child}/stat")
        for _ in range(50):
            if not procstat.exists() or procstat.read_text().split()[2] == "Z":
                break
            time.sleep(.02)
        else:
            self.fail("Audio worker outlived its supervisor")
        self.call("start")
        self.await_phase("on")


    def install_widget(self):
        omarchy = self.root / "config/omarchy"
        plugin = omarchy / "plugins/re_pick.voice-recorder"
        plugin.parent.mkdir(parents=True)
        plugin.symlink_to(self.cli.parent.parent, target_is_directory=True)
        config = omarchy / "shell.json"
        config.write_text(json.dumps({"version": 1, "bar": {"layout": {"centerEnd": [{"id": plugin.name}]}}}))
        return plugin, config

    def test_uninstall_stops_audio(self):
        plugin, _ = self.install_widget()
        self.call("start")
        self.await_phase("on")
        child = int((self.runtime / "audio-pid").read_text())
        plugin.unlink()
        self.assertEqual(self.await_phase("off")["error"], "")
        self.assertFalse(Path(f"/proc/{child}").exists())

    def test_removing_widget_stops_after_config_recovers(self):
        _, config = self.install_widget()
        self.call("start")
        self.await_phase("on")
        config.write_text("{")
        time.sleep(2.4)
        self.assertEqual(self.call("status")["phase"], "on")
        config.write_text(json.dumps({"version": 1, "bar": {"layout": {"centerEnd": [{"id": "omarchy.elsewhen"}]}}}))
        self.assertEqual(self.await_phase("off")["error"], "")
        self.assertEqual(self.call("status")["phase"], "off")


if __name__ == "__main__":
    unittest.main()
