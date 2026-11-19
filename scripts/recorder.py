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

APP = "omarchy-voice-recorder"
HOME = Path.home()
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / APP
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP / "settings.json"
STATE = RUNTIME / "state.json"
STOP = RUNTIME / "stop"
MODEL = "dpdfnet2_48khz_hr"
ACTIVE = {"starting", "recording", "processing"}
TAKE_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}")


class RecorderError(Exception):
    """A localization key understood by the panel."""


def read_json(path):
    try:
        with path.open() as source:
            return json.load(source)
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


def empty_state(folder, strength):
    return dict(phase="idle", folder=str(folder), take="", original="", cleaned="",
                startedAt=0, duration=0, error="", strength=strength)


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


def inspect_take(state):
    take = Path(state["take"])
    original, cleaned = take / "original.wav", take / "cleaned.wav"
    duration = wav_duration(original)
    state.update(original=str(original) if duration else "", duration=duration,
                 cleaned=str(cleaned) if duration and wav_duration(cleaned) else "")


def latest(folder, strength):
    state = empty_state(folder, strength)
    try:
        with os.scandir(folder) as entries:
            names = sorted((e.name for e in entries if TAKE_NAME.fullmatch(e.name)
                            and e.is_dir(follow_symlinks=False)), reverse=True)
        for name in names:
            state["take"] = str(folder / name)
            inspect_take(state)
            if state["original"]:
                state.update(phase="ready" if state["cleaned"] else "error",
                             error="" if state["cleaned"] else "interrupted")
                return state
    except FileNotFoundError:
        pass
    except OSError:
        state.update(phase="error", error="folder")
    state["take"] = ""
    return state


def engine():
    local = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "dpdfnet/venv/bin/dpdfnet"
    binary = str(local) if os.access(local, os.X_OK) else shutil.which("dpdfnet")
    if not binary:
        raise RecorderError("engine")
    cache = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "dpdfnet"
    cache = Path(os.environ.get("DPDFNET_CACHE_DIR", cache))
    models = Path(os.environ.get("DPDFNET_MODEL_DIR", cache / "models")).expanduser()
    weights = models / (MODEL + ".onnx")
    if not weights.is_file() or not weights.stat().st_size:
        raise RecorderError("model")
    return binary


def update(state, **fields):
    state.update(fields)
    with serialized():
        write_json(STATE, state)


def subprocess_guard(on_parent_death):
    # If the controller is killed, PipeWire must still finalize the WAV.
    parent = os.getpid()
    libc = ctypes.CDLL(None)

    def guard():
        if libc.prctl(1, on_parent_death, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)

    return guard


def capture(state):
    path = Path(state["take"]) / "original.wav"
    command = ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", path]
    with subprocess.Popen(command, preexec_fn=subprocess_guard(signal.SIGINT)) as recorder:
        stopped = False
        try:
            start = time.time()
            deadline = time.monotonic() + 6
            while recorder.poll() is None and not STOP.exists():
                if state["phase"] == "starting":
                    if path.is_file() and path.stat().st_size > 512:
                        update(state, phase="recording", startedAt=start)
                    elif time.monotonic() >= deadline:
                        raise RecorderError("no-input")
                time.sleep(0.08)
            stopped = STOP.exists()
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


def enhance(state):
    update(state, phase="processing", startedAt=0, error="")
    folder = Path(state["take"])
    temp = folder / ".cleaned.partial.wav"
    # CLI model resolution normally permits automatic downloads. Force a local-only
    # failure if weights disappear after our preflight check.
    env = dict(os.environ, DPDFNET_HF_BASE_URL="file:///dev/null", DPDFNET_DOWNLOAD_RETRIES="1")
    command = [engine(), "enhance", state["original"], temp, "--model", MODEL,
               "--attn-limit-db", str(state["strength"])]
    try:
        result = subprocess.run(command, env=env, preexec_fn=subprocess_guard(signal.SIGTERM))
        if result.returncode or not wav_duration(temp):
            raise RecorderError("enhance")
        cleaned = folder / "cleaned.wav"
        temp.replace(cleaned)
        update(state, phase="ready", cleaned=str(cleaned))
    finally:
        temp.unlink(missing_ok=True)


def worker(mode):
    with serialized():
        state = read_json(STATE)
    try:
        if mode == "capture":
            capture(state)
        enhance(state)
    except (OSError, RecorderError) as exc:
        inspect_take(state)
        error = str(exc) if isinstance(exc, RecorderError) else (
            "capture" if state["phase"] in {"starting", "recording"} else "enhance")
        update(state, phase="error", startedAt=0, error=error)


def launch(state, mode):
    # The child inherits the locked open file description before Popen returns.
    with (RUNTIME / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        STOP.unlink(missing_ok=True)
        state.update(phase="starting" if mode == "capture" else "processing", error="", startedAt=0)
        write_json(STATE, state)
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_worker", mode],
                         pass_fds=(lock.fileno(),), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def command(action, argument):
    with serialized():
        saved = read_json(STATE)
        settings = read_json(CONFIG)
        strength = settings.get("strength", 12)
        state = dict(saved) if saved else latest(selected_folder(settings), strength)
        state.setdefault("strength", strength)
        if worker_busy():
            if action == "stop" and state["phase"] in {"starting", "recording"}:
                STOP.touch()
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
                write_json(CONFIG, dict(settings, folder=str(folder), mountRoot=folder.is_mount()))
                if state["original"] and not state["cleaned"]:
                    state["folder"] = str(folder)
                else:
                    state = latest(folder, strength)
            elif action == "set-strength":
                write_json(CONFIG, dict(settings, strength=argument))
                state["strength"] = argument
            elif action == "start":
                engine()
                if not shutil.which("pw-record"):
                    raise RecorderError("recorder")
                folder = Path(state["folder"])
                if settings.get("mountRoot") and not folder.is_mount():
                    raise RecorderError("folder")
                target = folder / datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
                # A vanished user-selected destination may have been on a removable disk.
                target.mkdir(parents=not settings.get("folder"), mode=0o700)
                state = empty_state(target.parent, strength)
                state["take"] = str(target)
                launch(state, "capture")
                return state
            elif action == "retry" and state["original"] and not state["cleaned"]:
                engine()
                launch(state, "enhance")
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
        worker(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "start", "stop", "retry"):
        actions.add_parser(action)
    actions.add_parser("set-folder").add_argument("argument", metavar="PATH")
    actions.add_parser("set-strength").add_argument("argument", type=int, choices=(6, 12, 24))
    args = parser.parse_args()
    print(json.dumps(command(args.action, getattr(args, "argument", None)), ensure_ascii=False))


if __name__ == "__main__":
    main()
