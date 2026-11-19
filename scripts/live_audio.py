#!/usr/bin/env python3
"""Forward the selected microphone through a causal DPDFNet to a virtual PipeWire source."""

import argparse
from collections import deque
from contextlib import ExitStack, contextmanager
import json
import os
import select
import signal
import statistics
import subprocess
import sys
import time

from recorder import MODELS, STRENGTHS, model_dir, subprocess_guard

SOURCE_NAME = "dpdfnet_clean_microphone"
SOURCE_DESCRIPTION = "DPDFNet Clean Microphone"
HOP_SECONDS = 0.010


class LiveError(Exception):
    """A stable error code for the panel."""


def source_serial():
    requested = os.environ.get("OMARCHY_VOICE_SOURCE")
    if not requested:
        requested = subprocess.check_output(["pactl", "get-default-source"], text=True, timeout=3).strip()
    if not requested or requested == SOURCE_NAME or requested.endswith(".monitor"):
        raise LiveError("source")
    nodes = json.loads(subprocess.check_output(["pw-dump"], timeout=3))
    serial = None
    for node in nodes:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        props = node["info"]["props"]
        if props.get("node.name") == SOURCE_NAME:
            raise LiveError("pipewire")
        if props.get("node.name") == requested and props.get("media.class") in {"Audio/Source", "Audio/Source/Virtual"}:
            serial = str(props["object.serial"])
    if serial is None:
        raise LiveError("source")
    return serial


def candidates(model):
    """Try the user's choice first, then cheaper models at the same rate."""
    rate = MODELS[model][1]
    fallback = {48000: ["dpdfnet2_48khz_hr"], 16000: ["dpdfnet2", "baseline"],
                8000: ["dpdfnet2_8khz"]}[rate]
    if model == "baseline":
        return [model]
    return [model] + [name for name in fallback if name != model]


def select_engine(preferred):
    import numpy as np
    from dpdfnet import StreamEnhancer

    rate = MODELS[preferred][1]
    noise = np.random.default_rng(5).normal(0, 0.06, rate // 100).astype("float32")
    for model in candidates(preferred):
        path = model_dir() / (model + ".onnx")
        if not path.is_file():
            if model == preferred:
                raise LiveError("model")
            continue
        try:
            enhancer = StreamEnhancer(model=model, onnx_path=path)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LiveError("model") from exc
        for _ in range(10):
            enhancer.process(noise)
        times = []
        for _ in range(3):
            start = time.monotonic()
            for _ in range(20):
                enhancer.process(noise)
            times.append((time.monotonic() - start) / 20)
        if statistics.median(times) <= HOP_SECONDS * 0.92:
            enhancer.reset()
            return model, enhancer
    raise LiveError("overload")


def pw_args(rate, properties):
    return ["pw-cat", "--raw", "--format", "f32", "--rate", str(rate), "--channels", "1",
            "--channel-map", "MONO", "--latency", "10ms", "-P", json.dumps(properties)]


@contextmanager
def pw_process(args, **pipes):
    # Bound queued PCM before the child can fill its pipe, even when a driver stalls.
    proc = subprocess.Popen(args, bufsize=0, pipesize=4096, stderr=subprocess.DEVNULL,
                            preexec_fn=subprocess_guard(signal.SIGTERM), **pipes)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()


def read_hop(fd, size):
    buf = bytearray()
    while len(buf) < size:
        if not select.select([fd], [], [], 2)[0]:
            raise LiveError("source")
        data = os.read(fd, size - len(buf))
        if not data:
            raise LiveError("source")
        buf.extend(data)
    return buf


def write_pcm(fd, pcm):
    if not select.select([], [fd], [], 2)[1]:
        raise LiveError("stream")
    # A 10 ms mono float block is < PIPE_BUF at all supported rates.
    if os.write(fd, pcm) != len(pcm):
        raise LiveError("stream")


def stream(serial, preferred, strength):
    import numpy as np
    from dpdfnet.audio import ATTN_LIMIT_NOISY_FRAME_OFFSET

    model, enhancer = select_engine(preferred)
    rate = MODELS[model][1]
    hop = rate // 100
    zero = np.zeros(hop, dtype="float32")
    # The graph outputs spectrum t-4. Match that dry hop to the emitted wet hop,
    # including the first analysis window for which process() returns no output.
    dry = deque([zero] * ATTN_LIMIT_NOISY_FRAME_OFFSET)
    original_gain = 10 ** (-strength / 20)

    capture = pw_args(rate, {"node.name": "dpdfnet_live_capture", "node.dont-reconnect": True,
                             "node.dont-fallback": True, "node.dont-move": True})
    output = pw_args(rate, {"media.class": "Audio/Source", "node.name": SOURCE_NAME,
                            "node.description": SOURCE_DESCRIPTION, "node.virtual": True,
                            "priority.session": 1, "node.always-process": True,
                            "node.pause-on-idle": False, "node.want-driver": True})
    with ExitStack() as processes:
        inp = processes.enter_context(pw_process(capture + ["-r", "--target", serial, "-"],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE))
        out = processes.enter_context(pw_process(output + ["-p", "--target", "0", "-"],
                                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL))
        read_fd, write_fd = inp.stdout.fileno(), out.stdin.fileno()
        os.set_blocking(write_fd, False)
        count = 0
        compute_time = 0.0
        ready = False
        startup_deadline = time.monotonic() + 4
        while True:
            block = np.frombuffer(read_hop(read_fd, hop * 4), dtype="float32")
            begin = time.monotonic()
            wet = enhancer.process(block)
            dry.append(block)
            if wet.size:
                mix = wet * (1 - original_gain) + dry.popleft() * original_gain
                pcm = np.clip(mix, -1, 1).tobytes()
            else:
                pcm = zero.tobytes()
            compute_time += time.monotonic() - begin
            write_pcm(write_fd, pcm)
            count += 1
            if not ready and wet.size and count % 5 == 0:
                if out.poll() is not None or time.monotonic() > startup_deadline:
                    raise LiveError("pipewire")
                sources = subprocess.check_output(["pactl", "list", "short", "sources"],
                                                  text=True, timeout=2)
                if any(line.split("\t")[1:2] == [SOURCE_NAME] for line in sources.splitlines()):
                    print("READY", model, flush=True)
                    ready = True
            if count == 100:
                if compute_time > HOP_SECONDS * count * 1.05:
                    raise LiveError("overload")
                count = 0
                compute_time = 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--strength", type=int, choices=STRENGTHS, required=True)
    args = parser.parse_args()

    def stop(_sig, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        stream(source_serial(), args.model, args.strength)
    except LiveError as exc:
        reason = str(exc)
    except ImportError:
        reason = "engine"
    except (subprocess.SubprocessError, FileNotFoundError):
        reason = "pipewire"
    except (OSError, RuntimeError, ValueError):
        reason = "stream"
    print("ERROR", reason, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
