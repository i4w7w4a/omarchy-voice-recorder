"""Exercise the CLI without requiring a microphone, model download or audio server."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
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
import wave

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLUGIN_ID = "re_pick.voice-recorder"

# A synthetic 48 kHz source with the same SIGINT/exit contract as pw-record.
FAKE_RECORD = """
import math, os, pathlib, signal, struct, sys, time, wave
running = True
def stop(*_):
    global running
    running = False
signal.signal(signal.SIGINT, stop)
runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
(runtime / "controller-pid").write_text(str(os.getppid()))
slow_input = runtime / "slow-input"
frames = b"".join(struct.pack("<h", int(3000 * math.sin(i * math.pi / 24))) for i in range(960))
with wave.open(sys.argv[-1], "wb") as output:
    output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
    if slow_input.exists():
        output.writeframes(frames[:32])
    (runtime / "capture-pid").write_text(str(os.getpid()))
    while running and slow_input.exists():
        time.sleep(0.02)
    while running:
        output.writeframes(frames)
        time.sleep(0.02)
sys.exit(1)
"""

FAKE_ENHANCE = """
import os, pathlib, shutil, sys, time
runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
(runtime / "strength").write_text(sys.argv[-1])
(runtime / "model").write_text(sys.argv[sys.argv.index("--model") + 1])
while (runtime / "delay-engine").exists(): time.sleep(.02)
if (runtime / "fail-engine").exists():
    pathlib.Path(sys.argv[3]).write_bytes(b"incomplete WAV")
    sys.exit(1)
shutil.copyfile(sys.argv[2], sys.argv[3])
"""


class RecorderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scripts = self.root / "checkout/scripts"
        shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
        self.cli = scripts / "recorder.py"
        manifest = scripts / "model_weights.json"
        pins = json.loads(manifest.read_text())
        for entry in pins["models"].values():
            entry.update(size=len(b"fixture"), sha256=hashlib.sha256(b"fixture").hexdigest())
        manifest.write_text(json.dumps(pins))
        binary = self.root / "bin"
        binary.mkdir()
        self.runtime = self.root / "run"
        self.runtime.mkdir()
        models = self.root / "models"
        models.mkdir()
        self.weights = models / "dpdfnet8_48khz_hr.onnx"
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
        data = subprocess.check_output([sys.executable, self.cli, *args], env=self.env, timeout=5)
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

    def install_widget(self):
        omarchy = self.root / ".config/omarchy"
        plugin = omarchy / "plugins" / PLUGIN_ID
        plugin.parent.mkdir(parents=True)
        plugin.symlink_to(self.cli.parent.parent, target_is_directory=True)
        self.shell_config = omarchy / "shell.json"
        self.set_layout({"centerEnd": [{"id": PLUGIN_ID}]})
        return plugin

    def set_layout(self, layout, disabled=()):
        self.shell_config.write_text(json.dumps({
            "version": 1, "bar": {"layout": layout}, "disabledPlugins": list(disabled)
        }))

    def start_widget(self, plugin):
        command = [sys.executable, "-B", plugin / "scripts/recorder.py", "start"]
        return json.loads(subprocess.check_output(command, env=self.env, timeout=5))

    def published_state(self):
        return json.loads((self.runtime / "omarchy-voice-recorder/state.json").read_text())

    def wait_for_published_phase(self, phase):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            state = self.published_state()
            if state["phase"] == phase:
                return state
            time.sleep(0.04)
        self.fail(f"Expected worker to publish {phase}, last state was {state}")

    def wait_for_marker(self, name):
        until = time.monotonic() + 4
        path = self.runtime / name
        while time.monotonic() < until:
            try:
                value = path.read_text()
            except FileNotFoundError:
                value = ""
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"Expected {name}")

    def assert_finalized(self, state, pid):
        self.assertFalse(Path(f"/proc/{pid}").exists())
        with wave.open(state["original"], "rb") as audio:
            self.assertGreater(audio.getnframes(), 0)
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 48000))
            self.assertEqual(len(audio.readframes(audio.getnframes())), audio.getnframes() * 2)
        if state["phase"] == "ready":
            self.assertTrue(Path(state["cleaned"]).is_file())

    def test_removing_widget_finalizes_capture_after_move_and_config_recovery(self):
        plugin = self.install_widget()
        self.start_widget(plugin)
        original = self.wait_for_published_phase("recording")
        pid = self.wait_for_marker("capture-pid")

        self.shell_config.write_text("{")
        time.sleep(0.4)
        self.assertEqual(self.published_state(), original)
        self.shell_config.write_text(json.dumps({"bar": {"layout": {}}}))
        time.sleep(0.3)
        self.assertEqual(self.published_state(), original)
        self.set_layout({"centerEnd": [], "left": [{"id": PLUGIN_ID}], "tray": [PLUGIN_ID]})
        time.sleep(0.3)
        self.set_layout({"left": [], "tray": [PLUGIN_ID]})
        time.sleep(0.3)
        self.assertEqual(self.published_state(), original)

        self.set_layout({"left": [], "tray": ["omarchy.audio"]})
        saved = self.wait_for_published_phase("ready")
        self.assertEqual(saved["take"], original["take"])
        self.assert_finalized(saved, pid)

    def test_disabling_widget_while_recording_starts_finalizes_audio_already_captured(self):
        plugin = self.install_widget()
        (self.runtime / "slow-input").touch()
        self.start_widget(plugin)
        pid = self.wait_for_marker("capture-pid")
        self.assertEqual(self.published_state()["phase"], "starting")
        self.set_layout({"centerEnd": [{"id": PLUGIN_ID}]}, disabled=[PLUGIN_ID])
        self.assert_finalized(self.wait_for_published_phase("ready"), pid)

    def test_unlinking_widget_finalizes_original_with_missing_model(self):
        plugin = self.install_widget()
        self.start_widget(plugin)
        self.wait_for_published_phase("recording")
        pid = self.wait_for_marker("capture-pid")
        self.weights.unlink()
        plugin.unlink()
        failed = self.wait_for_published_phase("error")
        self.assertEqual(failed["error"], "model")
        self.assertEqual(failed["cleaned"], "")
        self.assert_finalized(failed, pid)

    def test_replacing_widget_link_finalizes_capture(self):
        plugin = self.install_widget()
        self.start_widget(plugin)
        self.wait_for_published_phase("recording")
        pid = self.wait_for_marker("capture-pid")
        replacement = plugin.with_name(".replacement")
        replacement.symlink_to(plugin.resolve(), target_is_directory=True)
        replacement.replace(plugin)
        self.assert_finalized(self.wait_for_published_phase("ready"), pid)

    def test_missing_shell_config_eventually_finalizes_capture(self):
        plugin = self.install_widget()
        self.start_widget(plugin)
        self.wait_for_published_phase("recording")
        pid = self.wait_for_marker("capture-pid")
        self.shell_config.unlink()
        self.assert_finalized(self.wait_for_published_phase("ready"), pid)

    def assert_start_canceled(self, change_installation):
        plugin = self.install_widget()
        # Block the detached interpreter before it reads the application state.
        (self.root / "sitecustomize.py").write_text("""
import os, pathlib, sys, time
if sys.argv[1:3] == ["_worker", "capture"]:
    runtime = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
    (runtime / "worker-waiting").write_text("ready")
    while (runtime / "delay-worker").exists():
        time.sleep(0.01)
""")
        self.env["PYTHONPATH"] = str(self.root)
        gate = self.runtime / "delay-worker"
        gate.touch()
        try:
            started = self.start_widget(plugin)
            self.assertEqual(started["phase"], "starting")
            self.wait_for_marker("worker-waiting")
            change_installation(plugin)
        finally:
            gate.unlink()
        state = self.wait_for_published_phase("error")
        self.assertEqual(state["error"], "interrupted")
        self.assertFalse((self.runtime / "capture-pid").exists())
        self.assertFalse((Path(started["take"]) / "original.wav").exists())

    def test_disabling_widget_before_worker_begins_keeps_microphone_closed(self):
        self.assert_start_canceled(lambda _: self.set_layout({"centerEnd": []}))

    def test_unlinking_widget_before_worker_begins_keeps_microphone_closed(self):
        self.assert_start_canceled(Path.unlink)

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
        self.assertEqual(self.call("set-model", "baseline")["model"], "dpdfnet8_48khz_hr")
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

    def test_language_and_live_device_persist_through_worker_updates(self):
        state_file = self.runtime / "omarchy-voice-recorder/state.json"
        before = self.call("status")
        self.assertEqual((before["language"], before["device"]), ("system", "auto"))
        self.call("start")
        self.wait_for("recording")
        self.call("set-language", "ru")
        self.assertEqual(json.loads(state_file.read_text())["language"], "ru")
        delay = self.runtime / "delay-engine"
        delay.touch()
        try:
            self.call("stop")
            self.assertEqual(self.wait_for("processing")["language"], "ru")
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda args: self.call(*args), [("set-language", "en"), ("set-device", "cuda")]))
        finally:
            delay.unlink(missing_ok=True)
        # Read the worker's published state before any new command could repair it.
        until = time.monotonic() + 5
        while time.monotonic() < until:
            saved = json.loads(state_file.read_text())
            if saved["phase"] == "ready":
                break
            time.sleep(.04)
        self.assertEqual((saved["phase"], saved["language"], saved["device"]), ("ready", "en", "cuda"))
        # GPU selection only affects live audio, not the saved-file CPU engine.
        self.assertTrue(Path(saved["cleaned"]).is_file())
        shutil.rmtree(state_file.parent)
        self.assertEqual(self.call("status"), saved)

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
        shutil.rmtree(self.runtime / "omarchy-voice-recorder")
        self.assertEqual(self.call("status")["original"], failed["original"])
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

    def test_bad_cached_weights_never_reach_file_enhancer(self):
        self.call("start")
        self.wait_for("recording")
        self.weights.write_bytes(b"changed")  # Same length as the approved fixture.
        self.call("stop")
        stopped = self.wait_for("error")
        self.assertEqual(stopped["error"], "model")
        self.assertFalse((self.runtime / "model").exists())
        self.assertFalse(stopped["cleaned"])
        self.assertTrue(Path(stopped["original"]).is_file())
        self.weights.write_bytes(b"fixture")
        self.call("retry")
        self.assertTrue(self.wait_for("ready")["cleaned"])

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

    def test_model_registry_and_selection_validation(self):
        state = self.call("status")
        self.assertEqual((state["model"], state["strength"]), ("dpdfnet8_48khz_hr", 12))
        self.assertEqual([row["rate"] for row in state["models"]], [48000]*2 + [16000]*4 + [8000]*2)
        self.assertEqual([row["id"] for row in state["models"] if row["installed"]], [state["model"]])
        self.assertEqual(self.call("set-model", "bogus/../model")["error"], "model")
        self.assertEqual(self.call("status")["model"], state["model"])
        self.assertEqual(self.call("set-model", "baseline")["model"], "baseline")
        self.assertEqual(self.call("start")["error"], "model")
        shutil.rmtree(self.runtime / "omarchy-voice-recorder")
        self.assertEqual(self.call("status")["model"], "baseline")

    def test_compare_models_on_one_take_and_reuse_cached_render(self):
        for model in ("baseline", "dpdfnet2_8khz"):
            (self.weights.parent / f"{model}.onnx").write_bytes(b"fixture")
        self.call("set-model", "baseline")
        self.call("set-strength", "6")
        self.call("start")
        self.wait_for("recording")
        self.call("stop")
        first = self.wait_for("ready")
        self.assertTrue(first["matched"])
        self.assertEqual((first["cleanedModel"], first["cleanedStrength"]), ("baseline", 6))
        self.assertEqual((self.runtime / "model").read_text(), "baseline")
        take = Path(first["take"])
        meta = json.loads((take / "cleaned.json").read_text())
        self.assertEqual(meta, dict(model="baseline", strength=6))
        original, primary = take / "original.wav", take / "cleaned.wav"
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (original, primary)]
        fallback = self.call("set-model", "dpdfnet2_8khz")
        self.assertFalse(fallback["matched"])
        self.assertEqual((fallback["cleaned"], fallback["cleanedModel"]), (str(primary), "baseline"))
        self.call("set-strength", "24")
        self.call("reprocess")
        second = self.wait_for("ready")
        self.assertEqual((second["cleanedModel"], second["cleanedStrength"]), ("dpdfnet2_8khz", 24))
        self.assertTrue(second["matched"])
        self.assertEqual((self.runtime / "model").read_text(), "dpdfnet2_8khz")
        variant = Path(second["cleaned"])
        self.assertEqual(variant, take / "variants/dpdfnet2_8khz-24dB.wav")
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (original, primary)])
        (self.weights.parent / "dpdfnet2_8khz.onnx").unlink()
        when = variant.stat().st_mtime_ns
        self.assertEqual(self.call("reprocess")["phase"], "ready")
        self.assertEqual(variant.stat().st_mtime_ns, when)
        self.call("set-model", "baseline")
        self.assertEqual(self.call("set-strength", "6")["cleaned"], str(primary))
        self.call("set-model", "dpdfnet2_8khz")
        self.call("set-strength", "24")
        shutil.rmtree(self.runtime / "omarchy-voice-recorder")
        restored = self.call("status")
        self.assertTrue(restored["matched"])
        self.assertEqual(restored["cleaned"], str(variant))

    def test_legacy_recording_and_interrupted_comparison_keep_primary(self):
        folder = self.root / "Recordings"
        take = folder / "2026-11-19_00-00-00_000001"
        take.mkdir(parents=True)
        original, primary = take / "original.wav", take / "cleaned.wav"
        with wave.open(str(original), "wb") as out:
            out.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            out.writeframes(bytes(9600))
        shutil.copyfile(original, primary)
        before = (primary.read_bytes(), primary.stat().st_mtime_ns)
        legacy = self.call("set-folder", str(folder))
        self.assertEqual(legacy["cleanedModel"], "dpdfnet2_48khz_hr")
        self.assertEqual(legacy["cleanedStrength"], 0)
        self.assertFalse(legacy["matched"])
        (self.runtime / "fail-engine").touch()
        self.call("reprocess")
        failed = self.wait_for("error")
        self.assertEqual(failed["error"], "enhance")
        self.assertEqual(failed["cleaned"], str(primary))
        self.assertFalse(failed["matched"])
        self.assertEqual(list((take / "variants").iterdir()), [])
        (self.runtime / "fail-engine").unlink()
        self.call("retry")
        ready = self.wait_for("ready")
        self.assertTrue(ready["matched"])
        self.assertNotEqual(ready["cleaned"], str(primary))
        self.assertEqual((primary.read_bytes(), primary.stat().st_mtime_ns), before)
        self.assertFalse((take / "cleaned.json").exists())

    def test_switching_variants_after_primary_was_removed(self):
        self.call("start")
        self.wait_for("recording")
        self.call("stop")
        ready = self.wait_for("ready")
        self.call("set-strength", "6")
        self.call("reprocess")
        variant = self.wait_for("ready")["cleaned"]
        Path(ready["cleaned"]).unlink()

        for action, missing, available in [("set-strength", "12", "6"),
                                            ("set-model", "baseline", "dpdfnet8_48khz_hr")]:
            with self.subTest(action=action):
                absent = self.call(action, missing)
                self.assertEqual((absent["phase"], absent["error"]), ("error", "interrupted"))
                self.assertEqual(absent["cleaned"], "")
                self.assertTrue(absent["original"])
                self.assertEqual(self.call("status"), absent)
                restored = self.call(action, available)
                self.assertEqual((restored["phase"], restored["error"]), ("ready", ""))
                self.assertEqual(restored["cleaned"], variant)
                self.assertTrue(restored["matched"])

        self.call("set-folder", str(self.root / "absent"))
        self.call("set-strength", "24")
        self.assertEqual(self.call("set-strength", "6")["error"], "folder")


if __name__ == "__main__":
    unittest.main()
