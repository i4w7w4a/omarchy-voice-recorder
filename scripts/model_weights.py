"""Install and verify the released DPDFNet 0.6.0 ONNX models."""

from contextlib import contextmanager
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from urllib.request import urlopen


REPO = "https://huggingface.co/Ceva-IP/DPDFNet"
_manifest = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
REVISION = _manifest["revision"]
MODEL_WEIGHTS = _manifest["models"]


class ModelIntegrityError(OSError):
    """A pinned ONNX model could not be verified."""


def model_dir():
    override = os.environ.get("DPDFNET_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "dpdfnet"
    return Path(os.environ.get("DPDFNET_CACHE_DIR") or cache).expanduser() / "models"


def _check_bytes(model, data):
    expected = MODEL_WEIGHTS[model]
    if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ModelIntegrityError(f"Size or SHA-256 mismatch for {model}.onnx")


def _read_weights(model, path):
    size = MODEL_WEIGHTS[model]["size"]
    # Open once and hash the bytes from that file descriptor. Skip special files
    # and symlinks, and bound reads even if the file grows after the size check.
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(path, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ModelIntegrityError("Expected a regular file of the pinned size")
            data = source.read(size + 1)
    except OSError as exc:
        raise ModelIntegrityError(f"Cannot read model {path}: {exc}") from exc
    _check_bytes(model, data)
    return data


@contextmanager
def _snapshot(model, data):
    # DPDFNet opens weights by path. Pass it a private copy of the exact bytes
    # checked above so cache changes between verification and loading are safe.
    with tempfile.TemporaryDirectory(prefix="dpdfnet-verified-") as directory:
        path = Path(directory) / (model + ".onnx")
        path.write_bytes(data)
        yield path


@contextmanager
def verified_model_path(model, path=None):
    if model not in MODEL_WEIGHTS:
        raise ModelIntegrityError(f"Unknown DPDFNet model: {model}")
    path = path if path is not None else model_dir() / (model + ".onnx")
    with _snapshot(model, _read_weights(model, path)) as verified:
        yield verified


def _download_weights(model):
    url = f"{REPO}/resolve/{REVISION}/onnx/{model}.onnx"
    with urlopen(url, timeout=60) as source:
        data = source.read(MODEL_WEIGHTS[model]["size"] + 1)
    _check_bytes(model, data)
    return data


def _publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            # A stale symlink is replaced as a directory entry, like a regular
            # file. Nothing is written through it to the old target.
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def install():
    from dpdfnet import StreamEnhancer

    directory = model_dir()
    for model in MODEL_WEIGHTS:
        path = directory / (model + ".onnx")
        try:
            data = _read_weights(model, path)
        except ModelIntegrityError:
            print(f"Fetching pinned model {model}.onnx", flush=True)
            data = _download_weights(model)
            _publish(path, data)
        with _snapshot(model, data) as verified:
            StreamEnhancer(model=model, onnx_path=verified)


if __name__ == "__main__":
    try:
        install()
    except (OSError, HTTPException, RuntimeError, ValueError) as exc:
        sys.exit(f"Could not install DPDFNet models. {exc}")
