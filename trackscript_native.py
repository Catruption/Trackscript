"""Optional compiled audio kernels for Trackscript's reference renderer."""
from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import numpy as np

_ROOT = Path(__file__).resolve().parent
_LIBRARY = None
_ATTEMPTED = False


def _load_library():
    global _LIBRARY, _ATTEMPTED
    if _ATTEMPTED:
        return _LIBRARY
    _ATTEMPTED = True
    compiler = shutil.which("g++") or shutil.which("clang++")
    source = _ROOT / "sinc_kernel.cpp"
    if not compiler or not source.is_file():
        print("  native sinc kernel unavailable; using slower NumPy sampler", file=sys.stderr)
        return None

    source_bytes = source.read_bytes()
    key = hashlib.sha256(source_bytes + compiler.encode()).hexdigest()[:20]
    cache = Path(tempfile.gettempdir()) / f"trackscript-native-{key}"
    cache.mkdir(parents=True, exist_ok=True)
    library_path = cache / "sinc_kernel.so"
    if not library_path.exists():
        command = [compiler, "-O3", "-std=c++17", "-fPIC", "-shared",
                   str(source), "-o", str(library_path)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None)
            if detail:
                detail = detail.strip().splitlines()[-1]
            print(
                "  native sinc kernel compilation failed; using slower NumPy sampler"
                + (f": {detail}" if detail else ""),
                file=sys.stderr,
            )
            return None

    try:
        lib = ctypes.CDLL(str(library_path))
        fn = lib.trackscript_sinc_interpolate
        double_ptr = ctypes.POINTER(ctypes.c_double)
        float_ptr = ctypes.POINTER(ctypes.c_float)
        fn.argtypes = [float_ptr, ctypes.c_int, ctypes.c_int,
                       double_ptr, double_ptr, double_ptr, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, float_ptr]
        fn.restype = None
        _LIBRARY = fn
    except (OSError, AttributeError):
        _LIBRARY = None
    return _LIBRARY


def sinc_interpolate(data, positions, steps, loop=None, raw_positions=None):
    """Run the eight-tap sinc sampler in native code, or return None if unavailable."""
    fn = _load_library()
    if fn is None:
        return None
    data = np.ascontiguousarray(data, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    positions = np.ascontiguousarray(positions, dtype=np.float64)
    steps = np.ascontiguousarray(steps, dtype=np.float64)
    if raw_positions is None:
        raw_positions = positions
    raw_positions = np.ascontiguousarray(raw_positions, dtype=np.float64)
    output = np.empty((len(positions), data.shape[1]), dtype=np.float32)
    loop_start, loop_end = loop if loop is not None else (-1, -1)
    fn(data.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
       data.shape[0], data.shape[1],
       positions.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
       steps.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
       raw_positions.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
       len(positions), loop_start, loop_end,
       output.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
    return output
