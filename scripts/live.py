#!/usr/bin/env python3
"""Control the optional, session-local PipeWire microphone."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

import recorder

STATE = recorder.RUNTIME / "live.json"
LEASE = recorder.RUNTIME / "live-worker.lock"
STOP = recorder.RUNTIME / "live-stop"
AUDIO = Path(__file__).with_name("live_audio.py")
ERRORS = {"source", "pipewire", "engine", "model", "overload", "stream"}
PLUGIN_ID = "re_pick.voice-recorder"
OMARCHY = recorder.CONFIG.parents[1] / "omarchy"
INSTALLED = OMARCHY / "plugins" / PLUGIN_ID


@contextmanager
def serialized():
    with (recorder.RUNTIME / "live-commands.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def preferences():
    settings = recorder.read_json(recorder.CONFIG)
    model = settings.get("model")
    strength = settings.get("strength")
    if not isinstance(model, str) or model not in recorder.MODELS:
        model = recorder.DEFAULT_MODEL
    return dict(model=model, requestedModel=model, strength=strength if strength in recorder.STRENGTHS else 12)


def interpreter(model):
    binary = Path(recorder.engine(model)).resolve()
    python = binary.with_name("python")
    if not os.access(python, os.X_OK):
        raise recorder.RecorderError("engine")
    return python


def start(lease, state):
    python = interpreter(state["model"])
    STOP.unlink(missing_ok=True)
    state.update(phase="starting", error="")
    # The open file description and its lock pass directly to the supervisor.
    subprocess.Popen(
        [sys.executable, "-B", __file__, "_worker", str(lease.fileno()), str(python)],
        pass_fds=(lease.fileno(),), start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def command(action):
    with serialized(), LEASE.open("a") as lease:
        saved = recorder.read_json(STATE)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = False
        except BlockingIOError:
            busy = True

        if busy:
            state = dict(saved)
            if action == "stop":
                STOP.touch()
                state.update(phase="stopping", error="")
        else:
            state = dict(preferences(), phase="off", error="")
            if saved.get("phase") in {"starting", "on"}:
                state.update(phase="error", error="stream")
            elif saved.get("phase") == "error":
                state.update(phase="error", error=saved.get("error", "stream"))
            if action == "start":
                try:
                    start(lease, state)
                except (OSError, recorder.RecorderError) as exc:
                    state.update(phase="error", error=str(exc) if isinstance(exc, recorder.RecorderError) else "engine")
            elif action == "stop":
                state.update(phase="off", error="")
        if state != saved:
            recorder.write_json(STATE, state)
        return state


def stop_audio(audio):
    # Only signal our unreaped child's group; no persisted or reused PID is used.
    if audio.poll() is None:
        os.killpg(audio.pid, signal.SIGTERM)
        try:
            audio.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(audio.pid, signal.SIGKILL)
            audio.wait()


def installed_plugin():
    try:
        if (INSTALLED / "scripts/live.py").samefile(__file__):
            return INSTALLED.stat()
    except OSError:
        pass
    return None


def widget_enabled():
    # Return None while the shell configuration is incomplete or unreadable.
    try:
        config = recorder.read_json(OMARCHY / "shell.json")
    except OSError:
        return None
    disabled = config.get("disabledPlugins")
    if isinstance(disabled, list) and PLUGIN_ID in disabled:
        return False
    bar = config.get("bar")
    layout = bar.get("layout") if isinstance(bar, dict) else None
    if not isinstance(layout, dict) or not all(isinstance(items, list) for items in layout.values()):
        return None
    return any(item == PLUGIN_ID or isinstance(item, dict) and item.get("id") == PLUGIN_ID
               for items in layout.values() for item in items)


def supervise(python, state):
    installation = installed_plugin()
    watch_layout = installation is not None and widget_enabled() is True
    next_check = time.monotonic() + 2
    env = dict(os.environ, DPDFNET_HF_BASE_URL="file:///dev/null", DPDFNET_DOWNLOAD_RETRIES="1")
    args = [python, "-u", "-B", AUDIO, "--model", state["model"], "--strength", str(state["strength"])]
    with subprocess.Popen(args, stdout=subprocess.PIPE, env=env, start_new_session=True,
                          preexec_fn=recorder.subprocess_guard(signal.SIGTERM)) as audio:
        try:
            with selectors.DefaultSelector() as events:
                events.register(audio.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 45
                pending = b""
                while not STOP.exists():
                    now = time.monotonic()
                    if installation is not None and now >= next_check:
                        try:
                            if not os.path.samestat(installation, INSTALLED.stat()):
                                return ""
                        except FileNotFoundError:
                            return ""
                        if watch_layout and widget_enabled() is False:
                            return ""
                        next_check = now + 2
                    if state["phase"] == "starting" and now > deadline:
                        return "stream"
                    if not events.select(timeout=0.25):
                        continue
                    chunk = os.read(audio.stdout.fileno(), 512)
                    if not chunk or len(pending) + len(chunk) > 1024:
                        return "stream"
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line == b"READY" or line.startswith(b"READY "):
                            model = (line[6:].decode("ascii", errors="replace")
                                     if line.startswith(b"READY ") else state["requestedModel"])
                            if model not in recorder.MODELS:
                                return "stream"
                            with serialized():
                                if STOP.exists():
                                    return ""
                                state.update(phase="on", model=model, error="")
                                recorder.write_json(STATE, state)
                        elif line.startswith(b"ERROR "):
                            code = line[6:].decode("ascii", errors="replace")
                            return code if code in ERRORS else "stream"
                return ""
        finally:
            stop_audio(audio)


def worker(lease_fd, python):
    with serialized():
        state = recorder.read_json(STATE)
    error = ""
    try:
        if not STOP.exists():
            error = supervise(python, state)
    except OSError:
        error = "stream"
    finally:
        with serialized():
            if STOP.exists():
                error = ""
            state.update(phase="error" if error else "off", error=error)
            os.close(lease_fd)
            recorder.write_json(STATE, state)


def main():
    os.umask(0o077)
    recorder.RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.argv[1:2] == ["_worker"]:
        worker(int(sys.argv[2]), sys.argv[3])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "start", "stop"))
    print(json.dumps(command(parser.parse_args().action)))


if __name__ == "__main__":
    main()
