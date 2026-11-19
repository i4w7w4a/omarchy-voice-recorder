"""Routing decisions that must not depend on the test machine's default microphone."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import live_audio


class LiveAudioTests(unittest.TestCase):
    def test_fallback_stays_at_capture_rate(self):
        for name, (_, rate) in live_audio.MODELS.items():
            candidates = live_audio.candidates(name)
            self.assertEqual(candidates[0], name)
            self.assertEqual(len(candidates), len(set(candidates)))
            self.assertTrue(all(live_audio.MODELS[n][1] == rate for n in candidates))
        self.assertEqual(live_audio.candidates("baseline"), ["baseline"])
        self.assertEqual(live_audio.candidates("dpdfnet8_48khz_hr"),
                         ["dpdfnet8_48khz_hr", "dpdfnet2_48khz_hr"])

    def test_requested_device_and_model_are_respected(self):
        preferred = "dpdfnet8_48khz_hr"
        cpu = MagicMock()
        gpu = MagicMock()
        with tempfile.TemporaryDirectory() as work:
            folder = Path(work)
            (folder / (preferred + ".onnx")).touch()
            with (patch.dict(sys.modules, {"numpy": MagicMock(), "dpdfnet": MagicMock(StreamEnhancer=cpu)}),
                  patch.object(live_audio, "model_dir", return_value=folder),
                  patch.object(live_audio, "CudaStream", gpu),
                  patch.object(live_audio, "meets_deadline", return_value=True) as speed):
                self.assertEqual(live_audio.select_engine(preferred, "auto")[:2], (preferred, "cuda"))
                gpu.side_effect = RuntimeError("no driver")
                with self.assertRaisesRegex(live_audio.LiveError, "gpu"):
                    live_audio.select_engine(preferred, "cuda")
                cpu.assert_not_called()
                self.assertEqual(live_audio.select_engine(preferred, "auto")[:2], (preferred, "cpu"))
                gpu.side_effect = None
                speed.return_value = False
                with self.assertRaisesRegex(live_audio.LiveError, "overload"):
                    live_audio.select_engine(preferred, "cuda")
                self.assertEqual(cpu.call_count, 1)

    def test_pinned_input_is_a_source_and_never_our_own_output(self):
        def node(name, media_class, serial):
            props = {"node.name": name, "media.class": media_class, "object.serial": serial}
            return {"type": "PipeWire:Interface:Node", "info": {"props": props}}

        hardware = node("usb-mic", "Audio/Source", 24)
        sink = node("speakers", "Audio/Sink", 25)
        output = node(live_audio.SOURCE_NAME, "Audio/Source", 26)
        with patch.dict(os.environ, {"OMARCHY_VOICE_SOURCE": "usb-mic"}):
            with patch("subprocess.check_output", return_value=json.dumps([hardware, sink])):
                self.assertEqual(live_audio.source_serial(), "24")
            with patch("subprocess.check_output", return_value=json.dumps([hardware, output])):
                with self.assertRaisesRegex(live_audio.LiveError, "pipewire"):
                    live_audio.source_serial()
        for target in (live_audio.SOURCE_NAME, "speakers.monitor", "speakers", "disconnected"):
            with self.subTest(target=target), patch.dict(os.environ, {"OMARCHY_VOICE_SOURCE": target}):
                with patch("subprocess.check_output", return_value=json.dumps([hardware, sink])):
                    with self.assertRaisesRegex(live_audio.LiveError, "source"):
                        live_audio.source_serial()
