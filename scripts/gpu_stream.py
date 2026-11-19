"""CUDA inference for the DPDFNet streaming frontend (pinned package 0.6.0)."""
import argparse
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import site
import sys
import tempfile
import time


def prepare_libraries():
    """Let the CUDA provider and cuDNN dlopen the CUDA wheels in this venv."""
    dirs = [str(p) for root in site.getsitepackages() for p in sorted(Path(root).glob("nvidia/*/lib"))
            if p.is_dir()]
    current = os.environ.get("LD_LIBRARY_PATH", "")
    if not dirs or all(d in current.split(":") for d in dirs):
        return
    path = ":".join(dirs + ([current] if current else []))
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], dict(os.environ, LD_LIBRARY_PATH=path))


class CudaUnavailable(RuntimeError):
    pass


class DeviceFrames:
    """Keep recurrent state on the GPU, moving only one spectrum each way per hop."""

    def __init__(self, session, initial, capture):
        import numpy as np
        import onnxruntime as ort

        self.session = session
        spec, state = session.get_inputs()
        wet, next_state = session.get_outputs()
        self.spec_name = spec.name
        self.initial = initial
        self.spec = ort.OrtValue.ortvalue_from_shape_and_type(spec.shape, np.float32, "cuda", 0)
        self.wet = ort.OrtValue.ortvalue_from_shape_and_type(wet.shape, np.float32, "cuda", 0)
        self.state = [ort.OrtValue.ortvalue_from_numpy(initial, "cuda", 0) for _ in range(2)]
        self.bindings = []
        self.run_options = []
        for index in range(2):
            binding = session.io_binding()
            binding.bind_ortvalue_input(spec.name, self.spec)
            binding.bind_ortvalue_input(state.name, self.state[index])
            binding.bind_ortvalue_output(wet.name, self.wet)
            binding.bind_ortvalue_output(next_state.name, self.state[1 - index])
            self.bindings.append(binding)
            options = ort.RunOptions()
            if capture:
                # CUDA Graphs replay the exact device addresses recorded on the
                # first run. Each direction of state ping-pong needs its own ID.
                options.add_run_config_entry("gpu_graph_id", str(index))
            self.run_options.append(options)
        self.index = 0

    def reset(self):
        self.index = 0
        self.state[0].update_inplace(self.initial)

    def run(self, _output_names, feeds):
        self.spec.update_inplace(feeds[self.spec_name])
        self.session.run_with_iobinding(self.bindings[self.index], self.run_options[self.index])
        self.index = 1 - self.index
        # The frontend passes the state token back to this adapter. Actual state
        # stays in the alternating device buffers to avoid a host round trip.
        return self.wet.numpy(), None


class CudaStream:
    def __init__(self, model, path, profile_prefix=None, capture=True):
        import onnxruntime as ort
        from dpdfnet import StreamEnhancer

        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise CudaUnavailable("CUDA provider is unavailable")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        if profile_prefix:
            options.enable_profiling = True
            options.profile_file_prefix = str(profile_prefix)
        provider = ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC",
                                              "do_copy_in_default_stream": "1",
                                              "enable_cuda_graph": "1" if capture else "0"})
        session = ort.InferenceSession(str(path), sess_options=options,
                                       providers=[provider, "CPUExecutionProvider"])
        session.disable_fallback()
        if session.get_providers()[0] != "CUDAExecutionProvider":
            raise CudaUnavailable("CUDA provider could not initialize")
        # Upstream owns all STFT, buffering and overlap-add behavior. Its 0.6.0
        # frontend hardcodes a CPU session, so replace only the inference backend.
        self.frontend = StreamEnhancer(model=model, onnx_path=path)
        self.frames = DeviceFrames(session, self.frontend._runtime.init_state, capture)
        self.frontend._runtime = replace(self.frontend._runtime, session=self.frames)

    def process(self, signal):
        try:
            return self.frontend.process(signal)
        except Exception as exc:
            raise CudaUnavailable("CUDA inference failed") from exc

    def reset(self):
        self.frontend.reset()
        self.frames.reset()


def verify(model, seconds):
    import numpy as np
    from dpdfnet import StreamEnhancer
    from recorder import MODELS, model_dir

    path = model_dir() / (model + ".onnx")
    rate = MODELS[model][1]
    hop = rate // 100
    rng = np.random.default_rng(8)
    t = np.arange(hop * (max(1, seconds) * 100 + 20), dtype=np.float32) / rate
    x = (0.3 * np.sin(2 * np.pi * (160 * t + 22 * t**2)) *
         (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) + rng.normal(0, 0.02, len(t))).astype("float32")
    blocks = x.reshape(-1, hop)

    # An actual ORT profile distinguishes CUDA kernels from an installed but
    # unusable provider, which ORT can quietly replace with a CPU session.
    with tempfile.TemporaryDirectory(prefix="dpdfnet-cuda-") as work:
        gpu = CudaStream(model, path, Path(work) / "ort", capture=False)
        for block in blocks[:3]:
            gpu.process(block)
        report = json.loads(Path(gpu.frames.session.end_profiling()).read_text())
        kernels = Counter(event["args"]["op_name"] for event in report
                          if event.get("args", {}).get("provider") == "CUDAExecutionProvider"
                          and event.get("name", "").endswith("_kernel_time"))
        if not any(kernels[k] for k in ("Conv", "FusedConv", "Gemm", "MatMul", "GRU")):
            raise CudaUnavailable("no neural-network kernels were executed on CUDA")
    del gpu
    gpu = CudaStream(model, path)
    for block in blocks[:10]:
        gpu.process(block)
    gpu.reset()
    cpu = StreamEnhancer(model=model, onnx_path=path)
    reference = np.concatenate([cpu.process(block) for block in blocks[:80]])
    timings, outputs = [], []
    for i, block in enumerate(blocks):
        start = time.perf_counter()
        outputs.append(gpu.process(block))
        if i >= 20:
            timings.append(1000 * (time.perf_counter() - start))
    enhanced = np.concatenate(outputs[:80])
    relative_error = float(np.linalg.norm(enhanced - reference) / max(np.linalg.norm(reference), 1e-9))
    if not np.isfinite(relative_error) or relative_error > 0.02:
        raise CudaUnavailable(f"CPU/CUDA equivalence check failed ({relative_error:.4f})")
    gpu.reset()
    replay = np.concatenate([gpu.process(block) for block in blocks[:80]])
    if not np.allclose(replay, enhanced, rtol=1e-4, atol=1e-5):
        raise CudaUnavailable("CUDA state reset did not reproduce the recording")
    print(json.dumps(dict(model=model, cuda_ops=dict(kernels), relative_rms_error=relative_error,
                          measured_hops=len(timings),
                          hop_ms=10, median_ms=float(np.median(timings)), mean_ms=float(np.mean(timings)),
                          p95_ms=float(np.percentile(timings, 95)), max_ms=float(max(timings)))), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="dpdfnet8_48khz_hr")
    parser.add_argument("--seconds", type=int, default=3)
    args = parser.parse_args()
    prepare_libraries()
    verify(args.model, args.seconds)
