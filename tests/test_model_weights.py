"""Exercise pinned downloads and model loading without network or CUDA."""

import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gpu_stream
import model_weights


MODEL = "baseline"
FILENAME = MODEL + ".onnx"


class ModelWeightsTests(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(work)
        self.models = self.root / "models"
        self.models.mkdir()
        self.path = self.models / FILENAME
        self.enterContext(patch.dict(os.environ, {"DPDFNET_MODEL_DIR": str(self.models)}))

    def use_fixture_weights(self):
        original = b"pinned bytes"
        weights = {MODEL: {"size": len(original), "sha256": hashlib.sha256(original).hexdigest()}}
        self.enterContext(patch.object(model_weights, "MODEL_WEIGHTS", weights))
        return original

    def test_cache_directory_precedence(self):
        with patch.dict(os.environ, {"HOME": str(self.root)}, clear=True):
            self.assertEqual(model_weights.model_dir(), self.root / ".cache/dpdfnet/models")
            os.environ["XDG_CACHE_HOME"] = str(self.root / "xdg")
            self.assertEqual(model_weights.model_dir(), self.root / "xdg/dpdfnet/models")
            os.environ["DPDFNET_CACHE_DIR"] = "~/cache"
            self.assertEqual(model_weights.model_dir(), self.root / "cache/models")
            os.environ["DPDFNET_MODEL_DIR"] = "~/other-models"
            self.assertEqual(model_weights.model_dir(), self.root / "other-models")

    def test_verified_snapshot_keeps_the_bytes_it_checked(self):
        original = self.use_fixture_weights()
        self.path.write_bytes(original)
        with model_weights.verified_model_path(MODEL) as verified:
            self.assertEqual(verified.name, FILENAME)
            self.path.write_bytes(b"changed")
            self.assertEqual(verified.read_bytes(), original)
        self.assertFalse(verified.exists())

    def test_cuda_rejects_same_size_model_corruption_before_ort(self):
        with self.path.open("wb") as data:
            data.truncate(model_weights.MODEL_WEIGHTS[MODEL]["size"])
        ort = Mock()
        ort.get_available_providers.return_value = ["CUDAExecutionProvider"]
        dpdfnet = Mock()
        with patch.dict(sys.modules, {"onnxruntime": ort, "dpdfnet": dpdfnet}):
            with self.assertRaisesRegex(model_weights.ModelIntegrityError, "SHA-256"):
                gpu_stream.CudaStream(MODEL, self.path)
        ort.InferenceSession.assert_not_called()
        dpdfnet.StreamEnhancer.assert_not_called()

    def test_invalid_or_partial_download_is_not_published_or_loaded(self):
        original = self.use_fixture_weights()
        corrupt = b"x" * len(original)
        self.path.write_bytes(corrupt)
        dpdfnet = Mock()
        for payload in (corrupt, original[:-1]):
            with self.subTest(payload=payload):
                with (patch.dict(sys.modules, {"dpdfnet": dpdfnet}),
                      patch.object(model_weights, "urlopen", return_value=io.BytesIO(payload))):
                    with self.assertRaisesRegex(model_weights.ModelIntegrityError, "Size or SHA-256"):
                        model_weights.install()
                self.assertEqual(self.path.read_bytes(), corrupt)
                self.assertEqual(list(self.models.iterdir()), [self.path])
        dpdfnet.StreamEnhancer.assert_not_called()

    def test_installer_refreshes_a_stale_symlink_using_only_the_pin(self):
        self.assertRegex(model_weights.REVISION, r"\A[0-9a-f]{40}\Z")
        original = self.use_fixture_weights()
        target = self.root / "unrelated"
        target.write_bytes(b"keep")
        self.path.symlink_to(target)

        def open_model(model, onnx_path):
            self.assertEqual(model, MODEL)
            self.assertEqual(onnx_path.read_bytes(), original)

        dpdfnet = Mock(StreamEnhancer=Mock(side_effect=open_model))
        with (patch.dict(sys.modules, {"dpdfnet": dpdfnet}),
              patch.dict(os.environ, {"DPDFNET_HF_BASE_URL": "https://example.invalid",
                                      "DPDFNET_HF_REPO": "fake/repo"}),
              patch.object(model_weights, "urlopen", return_value=io.BytesIO(original)) as download):
            model_weights.install()
            model_weights.install()
        download.assert_called_once_with(
            f"https://huggingface.co/Ceva-IP/DPDFNet/resolve/{model_weights.REVISION}/onnx/{FILENAME}",
            timeout=60,
        )
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(list(self.models.iterdir()), [self.path])
        self.assertEqual(dpdfnet.StreamEnhancer.call_count, 2)


if __name__ == "__main__":
    unittest.main()
