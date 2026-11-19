#!/usr/bin/env python3
"""Local recording controller for the Omarchy voice recorder bar widget."""

import argparse
import ctypes
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave

import plugin_lifecycle
from model_weights import ModelIntegrityError, model_dir, verified_model_path

SCRIPT = Path(__file__).resolve()
APP = "omarchy-voice-recorder"
HOME = Path.home()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP / "settings.json"
STATE = RUNTIME / "state.json"
STOP = RUNTIME / "stop"
DEFAULT_MODEL = "dpdfnet8_48khz_hr"
LEGACY_MODEL = "dpdfnet2_48khz_hr"
MODELS = {
    "dpdfnet2_48khz_hr": ("DPDFNet-2", 48000),
    "dpdfnet8_48khz_hr": ("DPDFNet-8", 48000),
    "baseline": ("Baseline", 16000),
    "dpdfnet2": ("DPDFNet-2", 16000),
    "dpdfnet4": ("DPDFNet-4", 16000),
    "dpdfnet8": ("DPDFNet-8", 16000),
    "dpdfnet2_8khz": ("DPDFNet-2", 8000),
    "dpdfnet8_8khz": ("DPDFNet-8", 8000),
}
STRENGTHS = (6, 12, 24)
LANGUAGES = ("system", "ru", "en")
DEVICES = ("auto", "cuda", "cpu")
ACTIVE = {"starting", "recording", "processing"}
TAKE_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}")


class RecorderError(Exception):
    """A localization key understood by the panel."""


def read_json(path):
    try:
        with path.open() as source:
            value = json.load(source)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


@contextmanager
def serialized():
    with (RUNTIME / "commands.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def worker_busy():
    with (RUNTIME / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def selected_folder(settings):
    folder = settings.get("folder")
    if folder and Path(folder).is_absolute():
        return Path(folder)
    music = HOME / "Music"
    if shutil.which("xdg-user-dir"):
        query = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True)
        result = Path(query.stdout.strip())
        if query.returncode == 0 and result.is_absolute():
            music = result
    return music / "DPDFNet"


def preferences(settings):
    model = settings.get("model")
    strength = settings.get("strength")
    language = settings.get("language")
    device = settings.get("device")
    return {
        "model": model if isinstance(model, str) and model in MODELS else DEFAULT_MODEL,
        "strength": strength if strength in STRENGTHS else 12,
        "language": language if language in LANGUAGES else "system",
        "device": device if device in DEVICES else "auto",
    }


def empty_state(folder, prefs):
    return dict(phase="idle", folder=str(folder), take="", original="", cleaned="",
                startedAt=0, duration=0, error="", models=list_models(), matched=False,
                cleanedModel="", cleanedStrength=0, **prefs)


def wav_duration(path):
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            if frames <= 0:
                return 0
            audio.setpos(frames - 1)
            if len(audio.readframes(1)) != audio.getnchannels() * audio.getsampwidth():
                return 0
            return frames / audio.getframerate()
    except (OSError, wave.Error, EOFError, ValueError, ZeroDivisionError):
        return 0


def variant_path(take, model, strength):
    return take / "variants" / f"{model}-{strength}dB.wav"


def inspect_take(state):
    take = Path(state["take"])
    original = take / "original.wav"
    duration = wav_duration(original)
    state.update(original=str(original) if duration else "", duration=duration,
                 cleaned="", matched=False, cleanedModel="", cleanedStrength=0)
    if not duration:
        return

    model, strength = state["model"], state["strength"]
    primary = take / "cleaned.wav"
    if wav_duration(primary):
        metadata = take / "cleaned.json"
        try:
            provenance = read_json(metadata)
        except OSError:
            provenance = {}
        # Older releases supported one model and did not save the attenuation limit.
        rendered_model = provenance.get("model", "" if metadata.exists() else LEGACY_MODEL)
        rendered_strength = provenance.get("strength", 0)
        if not isinstance(rendered_model, str) or rendered_model not in MODELS:
            rendered_model = ""
        if rendered_strength not in STRENGTHS:
            rendered_strength = 0
        match = (rendered_model, rendered_strength) == (model, strength)
        state.update(cleaned=str(primary), matched=match, cleanedModel=rendered_model,
                     cleanedStrength=rendered_strength)
        if match:
            return

    variant = variant_path(take, model, strength)
    if wav_duration(variant):
        state.update(cleaned=str(variant), matched=True, cleanedModel=model, cleanedStrength=strength)


def restore_take(state, take):
    state["take"] = str(take)
    inspect_take(state)
    if state["original"]:
        state.update(phase="ready" if state["cleaned"] else "error",
                     error="" if state["cleaned"] else "interrupted")
        return True
    return False


def latest(folder, prefs, review_take=None):
    state = empty_state(folder, prefs)
    try:
        if review_take:
            take = Path(review_take)
            if take.is_absolute() and TAKE_NAME.fullmatch(take.name) and restore_take(state, take):
                return state
        with os.scandir(folder) as entries:
            names = sorted((e.name for e in entries if TAKE_NAME.fullmatch(e.name)
                            and e.is_dir(follow_symlinks=False)), reverse=True)
        for name in names:
            if restore_take(state, folder / name):
                return state
    except FileNotFoundError:
        pass
    except OSError:
        state.update(phase="error", error="folder")
    state["take"] = ""
    return state


def weights_installed(directory, model):
    weights = directory / (model + ".onnx")
    try:
        return weights.is_file() and weights.stat().st_size > 0
    except OSError:
        return False


def list_models():
    directory = model_dir()
    return [dict(id=model, label=label, rate=rate, installed=weights_installed(directory, model))
            for model, (label, rate) in MODELS.items()]


def engine(model):
    local = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "dpdfnet/venv/bin/dpdfnet"
    binary = str(local) if os.access(local, os.X_OK) else shutil.which("dpdfnet")
    if not binary:
        raise RecorderError("engine")
    if not weights_installed(model_dir(), model):
        raise RecorderError("model")
    return binary


def update(state, **fields):
    with serialized():
        # A capture can outlive several language and live-device changes.
        # Preserve its selected model and strength, but publish current UI preferences.
        prefs = preferences(read_json(CONFIG))
        state.update(fields, language=prefs["language"], device=prefs["device"])
        write_json(STATE, state)


def subprocess_guard(on_parent_death):
    # If the controller is killed, PipeWire must still finalize the WAV.
    parent = os.getpid()
    libc = ctypes.CDLL(None)

    def guard():
        if libc.prctl(1, on_parent_death, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)

    return guard


def capture(state, installation):
    widget = plugin_lifecycle.WidgetLifecycle(__file__, installation)
    if STOP.exists() or not widget.available():
        raise RecorderError("interrupted")
    path = Path(state["take"]) / "original.wav"
    command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", path]
    with subprocess.Popen(command, preexec_fn=subprocess_guard(signal.SIGINT)) as recorder:
        stopped = False
        try:
            start = time.time()
            deadline = time.monotonic() + 6
            while recorder.poll() is None:
                if STOP.exists() or not widget.available():
                    stopped = True
                    break
                if state["phase"] == "starting":
                    if path.is_file() and path.stat().st_size > 512:
                        update(state, phase="recording", startedAt=start)
                    elif time.monotonic() >= deadline:
                        raise RecorderError("no-input")
                time.sleep(0.08)
        finally:
            if recorder.poll() is None:
                recorder.send_signal(signal.SIGINT)
                try:
                    recorder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait()
    # pw-record may report status 1 after an intentional SIGINT.
    if not stopped or recorder.returncode not in (0, 1, -signal.SIGINT):
        raise RecorderError("capture")
    inspect_take(state)
    if not state["original"]:
        raise RecorderError("empty")


def enhance(state, mode):
    update(state, phase="processing", startedAt=0, error="")
    folder = Path(state["take"])
    model, strength = state["model"], state["strength"]
    primary = folder / "cleaned.wav"
    if mode == "reprocess" or primary.exists():
        cleaned = variant_path(folder, model, strength)
        cleaned.parent.mkdir(mode=0o700, exist_ok=True)
    else:
        cleaned = primary
    temp = cleaned.with_name("." + cleaned.stem + ".partial.wav")
    command = [engine(model), "enhance", state["original"], temp, "--model", model,
               "--attn-limit-db", str(strength)]
    try:
        with verified_model_path(model) as weights:
            env = dict(os.environ, DPDFNET_MODEL_DIR=str(weights.parent),
                       DPDFNET_HF_BASE_URL="file:///dev/null", DPDFNET_DOWNLOAD_RETRIES="1")
            result = subprocess.run(command, env=env, preexec_fn=subprocess_guard(signal.SIGTERM))
        if result.returncode or not wav_duration(temp):
            raise RecorderError("enhance")
        if cleaned == primary:
            # Publish provenance before the first WAV. A crash between these writes
            # leaves no playable primary to be mistaken for a legacy recording.
            write_json(folder / "cleaned.json", {"model": model, "strength": strength})
        temp.replace(cleaned)
        update(state, phase="ready", cleaned=str(cleaned), cleanedModel=model,
               cleanedStrength=strength, matched=True)
    except ModelIntegrityError as exc:
        raise RecorderError("model") from exc
    finally:
        temp.unlink(missing_ok=True)


def worker(mode, installation):
    with serialized():
        state = read_json(STATE)
    try:
        if mode == "capture":
            capture(state, installation)
        enhance(state, mode)
    except (OSError, RecorderError) as exc:
        inspect_take(state)
        error = str(exc) if isinstance(exc, RecorderError) else (
            "capture" if state["phase"] in {"starting", "recording"} else "enhance")
        update(state, phase="error", startedAt=0, error=error)


def launch(state, mode, installation=None):
    # The child inherits the locked open file description before Popen returns.
    with (RUNTIME / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        STOP.unlink(missing_ok=True)
        state.update(phase="starting" if mode == "capture" else "processing", error="", startedAt=0)
        write_json(STATE, state)
        subprocess.Popen([sys.executable, "-B", SCRIPT, "_worker", mode,
                          json.dumps(installation)],
                         pass_fds=(lock.fileno(),), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def command(action, argument):
    with serialized():
        saved = read_json(STATE)
        settings = read_json(CONFIG)
        if action in {"set-language", "set-device"}:
            field = action.removeprefix("set-")
            if settings.get(field) != argument:
                settings[field] = argument
                write_json(CONFIG, settings)
        prefs = preferences(settings)
        model = prefs["model"]
        if saved:
            state = dict(saved, models=list_models(), **prefs)
            state.setdefault("matched", False)
            state.setdefault("cleanedModel", "")
            state.setdefault("cleanedStrength", 0)
        else:
            state = latest(selected_folder(settings), prefs, settings.get("reviewTake"))
        if worker_busy():
            if action == "stop" and state["phase"] in {"starting", "recording"}:
                STOP.touch()
            if state != saved:
                write_json(STATE, state)
            return state

        if state["phase"] in ACTIVE:
            inspect_take(state)
            state.update(phase="error", error="interrupted", startedAt=0)
        elif state["take"]:
            inspect_take(state)
            if state["phase"] == "ready" and not state["cleaned"]:
                state.update(phase="error", error="interrupted")

        try:
            if action == "set-folder":
                folder = Path(argument).expanduser()
                if not folder.is_absolute() or not folder.is_dir():
                    raise RecorderError("folder")
                folder = folder.resolve()
                # Test actual permissions, including ACLs and read-only mounts.
                with tempfile.TemporaryFile(dir=folder):
                    pass
                unfinished = state["original"] and not state["cleaned"]
                settings.pop("reviewTake", None)
                if unfinished and folder != Path(state["take"]).parent:
                    # Keep the source available for retry even after the next login.
                    settings["reviewTake"] = state["take"]
                write_json(CONFIG, dict(settings, folder=str(folder), mountRoot=folder.is_mount()))
                if unfinished:
                    state["folder"] = str(folder)
                else:
                    state = latest(folder, prefs)
            elif action in {"set-strength", "set-model"}:
                field = "strength" if action == "set-strength" else "model"
                if field == "model" and argument not in MODELS:
                    raise RecorderError("model")
                settings[field] = argument
                write_json(CONFIG, settings)
                state[field] = argument
                if state["take"]:
                    inspect_take(state)
                if state["phase"] == "ready" and not state["cleaned"]:
                    state.update(phase="error", error="interrupted")
                elif state["error"] in {"enhance", "model", "engine", "interrupted"}:
                    if state["cleaned"] or not state["take"]:
                        state.update(phase="ready" if state["cleaned"] else "idle", error="")
            elif action == "start":
                installation = plugin_lifecycle.installed_plugin(__file__, SCRIPT)
                if not plugin_lifecycle.WidgetLifecycle(__file__, installation).available():
                    raise RecorderError("interrupted")
                engine(model)
                if not shutil.which("pw-record"):
                    raise RecorderError("recorder")
                folder = Path(state["folder"])
                if settings.get("mountRoot") and not folder.is_mount():
                    raise RecorderError("folder")
                target = folder / datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
                # A vanished user-selected destination may have been on a removable disk.
                target.mkdir(parents=not settings.get("folder"), mode=0o700)
                if settings.pop("reviewTake", None):
                    write_json(CONFIG, settings)
                state = empty_state(target.parent, prefs)
                state["take"] = str(target)
                launch(state, "capture", installation)
                return state
            elif action in {"retry", "reprocess"} and state["original"]:
                if state["matched"]:
                    state.update(phase="ready", error="")
                else:
                    engine(model)
                    launch(state, action)
                    return state
        except (RecorderError, OSError) as exc:
            state.update(phase="error", error=str(exc) if isinstance(exc, RecorderError) else "folder",
                         startedAt=0)

        if state != saved:
            write_json(STATE, state)
        return state


def main():
    os.umask(0o077)
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.argv[1:2] == ["_worker"]:
        worker(sys.argv[2], json.loads(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "start", "stop", "retry", "reprocess"):
        actions.add_parser(action)
    actions.add_parser("set-folder").add_argument("argument", metavar="PATH")
    actions.add_parser("set-strength").add_argument("argument", type=int, choices=STRENGTHS)
    actions.add_parser("set-model").add_argument("argument", metavar="ID")
    actions.add_parser("set-language").add_argument("argument", choices=LANGUAGES)
    actions.add_parser("set-device").add_argument("argument", choices=DEVICES)
    args = parser.parse_args()
    print(json.dumps(command(args.action, getattr(args, "argument", None)), ensure_ascii=False))


if __name__ == "__main__":
    main()
