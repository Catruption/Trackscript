#!/usr/bin/env python3
"""compare_renditions - render .it via libopenmpt and .trackscript via
trackscript_play, align them, and report where they diverge.

Library API (used by timeline_report.py):
    SR = 44100
    render_it(path, sr=SR) -> (left, right)          # float32, one per channel
    render_ts(path, sr=SR) -> (left, right, info)    # float32, one per channel

CLI:
    python3 compare_renditions.py song.it song.trackscript
    python3 compare_renditions.py song.it song.trackscript --render-it-only
    python3 compare_renditions.py song.it song.trackscript --block-seconds 15
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import math
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import numpy as np
from scipy.io import wavfile

try:
    from scipy.io.wavfile import WavFileWarning
except ImportError:
    WavFileWarning = Warning
warnings.filterwarnings("ignore", category=WavFileWarning)

SR = 44100

BANDS = [
    (60, 120), (120, 240), (240, 480), (480, 960),
    (960, 1920), (1920, 3840), (3840, 7680), (7680, 15360),
]
BAND_NAMES = [f"{lo}-{hi}Hz" for lo, hi in BANDS]


# ── helpers ──────────────────────────────────────────────────────────────

def _to_stereo_float(data):
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        data = (data.astype(np.float32) - 128.0) / 128.0
    else:
        data = data.astype(np.float32)
    if data.ndim == 1:
        data = np.stack([data, data], axis=1)
    return data[:, 0].copy(), data[:, 1].copy()


def _write_wav(path, left, right, sr=SR):
    n = min(len(left), len(right))
    stereo = np.stack([left[:n], right[:n]], axis=1)
    peak = float(np.max(np.abs(stereo))) if stereo.size else 0.0
    if peak > 0.99:
        stereo = stereo * (0.99 / peak)
    wavfile.write(path, sr,
                  (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16))


# ── .it rendering (libopenmpt) ───────────────────────────────────────────

def _render_it_cli(path, sr):
    """Try openmpt123 as a subprocess. Returns (l, r) or None."""
    if not shutil.which('openmpt123'):
        return None

    tmp = tempfile.mktemp(suffix='.wav')
    try:
        attempts = [
            ['openmpt123', '-o', tmp, '--samplerate', str(sr),
             '--channels', '2', str(path)],
            ['openmpt123', f'--output={tmp}', f'--samplerate={sr}',
             '--channels=2', str(path)],
            ['openmpt123', '-o', tmp, str(path)],
            ['openmpt123', '--output', tmp, str(path)],
        ]
        for args in attempts:
            try:
                r = subprocess.run(
                    args, capture_output=True, timeout=1200,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if (r.returncode == 0
                    and os.path.exists(tmp)
                    and os.path.getsize(tmp) > 44):
                sr_read, data = wavfile.read(tmp)
                return _to_stereo_float(data)
        return None
    finally:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass


def _render_it_python(path, sr):
    """Try python bindings for libopenmpt. Defensive against API drift."""
    try:
        import libopenmpt
    except ImportError:
        return None

    try:
        mod = libopenmpt.Module(str(path))
    except Exception:
        return None

    try:
        try:
            mod.set_render_param(libopenmpt.RENDER_SAMPLERATE, sr)
        except Exception:
            pass

        chunks = []
        while True:
            chunk = None
            for call in (
                lambda: mod.read(sr, 4096),
                lambda: mod.read(4096),
            ):
                try:
                    chunk = call()
                    break
                except TypeError:
                    continue
                except Exception:
                    chunk = None
                    break
            if not chunk:
                break
            arr = np.frombuffer(chunk, dtype=np.int16)
            if arr.size == 0:
                break
            chunks.append(arr)

        if not chunks:
            return None

        pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
        if pcm.size % 2:
            pcm = pcm[:-1]
        stereo = pcm.reshape(-1, 2)
        return stereo[:, 0].copy(), stereo[:, 1].copy()
    except Exception:
        return None


def _render_it_ctypes(path, sr):
    """Render through libopenmpt's stable C API when bindings/CLI are absent."""
    candidates = [
        os.environ.get('LIBOPENMPT_LIBRARY'),
        ctypes.util.find_library('openmpt'),
        'libopenmpt.so',
    ]
    lib = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
            break
        except OSError:
            continue
    if lib is None:
        return None

    try:
        log_func_type = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_void_p)
        log_func = log_func_type(('openmpt_log_func_silent', lib))
        create = lib.openmpt_module_create_from_memory2
        create.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            log_func_type,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_void_p,
        ]
        create.restype = ctypes.c_void_p

        module_data = Path(path).read_bytes()
        module_buffer = ctypes.create_string_buffer(module_data)
        error = ctypes.c_int(0)
        error_message = ctypes.c_char_p()
        module = create(
            ctypes.cast(module_buffer, ctypes.c_void_p),
            len(module_data),
            log_func,
            None,
            None,
            None,
            ctypes.byref(error),
            ctypes.byref(error_message),
            None,
        )
        if not module:
            return None

        read = lib.openmpt_module_read_interleaved_stereo
        read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int16),
        ]
        read.restype = ctypes.c_size_t
        destroy = lib.openmpt_module_destroy
        destroy.argtypes = [ctypes.c_void_p]
        destroy.restype = None

        chunk_frames = 16384
        buffer = (ctypes.c_int16 * (chunk_frames * 2))()
        chunks = []
        try:
            while True:
                frames = read(module, sr, chunk_frames, buffer)
                if not frames:
                    break
                raw = ctypes.string_at(
                    ctypes.addressof(buffer),
                    frames * 2 * ctypes.sizeof(ctypes.c_int16),
                )
                chunks.append(
                    np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                )
        finally:
            destroy(module)

        if not chunks:
            return None
        stereo = np.concatenate(chunks).reshape(-1, 2)
        return stereo[:, 0].copy(), stereo[:, 1].copy()
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def render_it(path, sr=SR):
    """Render an .it file. Returns (left, right) as float32."""
    result = _render_it_ctypes(path, sr)
    if result is not None:
        return result
    result = _render_it_cli(path, sr)
    if result is not None:
        return result
    result = _render_it_python(path, sr)
    if result is not None:
        return result
    raise RuntimeError(
        "no libopenmpt renderer available. Install either 'openmpt123' "
        "or the python bindings for libopenmpt."
    )


# ── .trackscript rendering ───────────────────────────────────────────────

def render_ts(path, sr=SR):
    """Render a .trackscript file. Returns (left, right, info)."""
    import trackscript_play

    tmp = tempfile.mktemp(suffix='.wav')
    try:
        trackscript_play.render(path, tmp)
        sr_read, data = wavfile.read(tmp)
        left, right = _to_stereo_float(data)

        if sr_read and sr_read != sr and len(left):
            n_out = int(len(left) * sr / sr_read)
            x_old = np.arange(len(left))
            x_new = np.linspace(0, len(left) - 1, n_out)
            left = np.interp(x_new, x_old, left).astype(np.float32)
            right = np.interp(x_new, x_old, right).astype(np.float32)

        return left, right, {'sample_rate': sr_read}
    finally:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass


def load_rendered_wav(path, sr=SR):
    """Load a cached stereo render and normalize it to the comparison rate."""
    sr_read, data = wavfile.read(path)
    left, right = _to_stereo_float(data)
    if sr_read and sr_read != sr and len(left):
        n_out = int(len(left) * sr / sr_read)
        x_old = np.arange(len(left))
        x_new = np.linspace(0, len(left) - 1, n_out)
        left = np.interp(x_new, x_old, left).astype(np.float32)
        right = np.interp(x_new, x_old, right).astype(np.float32)
    return left, right


# ── alignment ────────────────────────────────────────────────────────────

def find_lag(a, b, max_lag_s=0.4, window_s=8.0, sr=SR, step_ms=10.0):
    """Return lag and correlations; positive lag means TS audio is late."""
    n_win = min(int(window_s * sr), len(a), len(b))
    if n_win < sr // 4:
        return 0, 0.0, np.zeros(0), np.zeros(0)

    a_win = a[:n_win].astype(np.float64)
    b_win = b[:n_win].astype(np.float64)

    max_lag = min(int(max_lag_s * sr), n_win - 1)
    step = max(1, int(step_ms * sr / 1000.0))
    lags = np.arange(-max_lag, max_lag + 1, step)

    corrs = np.zeros(len(lags), dtype=np.float64)
    if np.linalg.norm(a_win) < 1e-9 or np.linalg.norm(b_win) < 1e-9:
        return 0, 0.0, corrs, lags

    def correlation_at(lag):
        if lag < 0:
            seg_a = a_win[-lag: n_win]
            seg_b = b_win[: n_win + lag]
        elif lag > 0:
            seg_a = a_win[: n_win - lag]
            seg_b = b_win[lag: n_win]
        else:
            seg_a = a_win
            seg_b = b_win
        denom = np.linalg.norm(seg_a) * (np.linalg.norm(seg_b) + 1e-12)
        return float(np.dot(seg_a, seg_b) / denom) if denom > 1e-12 else 0.0

    for i, lag in enumerate(lags):
        corrs[i] = correlation_at(int(lag))

    best = int(np.argmax(np.abs(corrs)))
    best_lag = int(lags[best])
    best_corr = float(corrs[best])
    if abs(best_corr) < 0.1:
        best_lag = 0
        best_corr = correlation_at(0)
    search_radius = max(1, step)
    for lag in range(max(-max_lag, best_lag - search_radius), min(max_lag, best_lag + search_radius) + 1):
        corr = correlation_at(lag)
        if abs(corr) > abs(best_corr):
            best_lag, best_corr = lag, corr

    if best_lag not in lags:
        insert = int(np.searchsorted(lags, best_lag))
        lags = np.insert(lags, insert, best_lag)
        corrs = np.insert(corrs, insert, best_corr)
    else:
        corrs[int(np.searchsorted(lags, best_lag))] = best_corr
    return best_lag, best_corr, corrs, lags


def align_audio(it_audio, ts_audio, lag):
    """Align mono or multichannel audio, preserving channels and full tails."""
    it_audio = np.asarray(it_audio)
    ts_audio = np.asarray(ts_audio)
    if it_audio.ndim != ts_audio.ndim or it_audio.shape[1:] != ts_audio.shape[1:]:
        raise ValueError('audio arrays must have matching channel dimensions')
    it_offset = max(0, lag)
    ts_offset = max(0, -lag)
    size = max(it_offset + len(it_audio), ts_offset + len(ts_audio))
    shape = (size,) + it_audio.shape[1:]
    aligned_it = np.zeros(shape, dtype=np.float32)
    aligned_ts = np.zeros(shape, dtype=np.float32)
    aligned_it[it_offset:it_offset + len(it_audio)] = it_audio
    aligned_ts[ts_offset:ts_offset + len(ts_audio)] = ts_audio

    overlap_start = max(it_offset, ts_offset)
    overlap_end = min(it_offset + len(it_audio), ts_offset + len(ts_audio))
    return aligned_it, aligned_ts, slice(overlap_start, max(overlap_start, overlap_end))


# ── band analysis ────────────────────────────────────────────────────────

def _fft_band_rms(x, sr, lo, hi):
    if len(x) < 256:
        return 0.0
    n = 1 << int(math.ceil(math.log2(len(x))))
    X = np.fft.rfft(x, n)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    mask = (freqs >= lo) & (freqs < hi)
    power = float(np.sum(np.abs(X[mask]) ** 2))
    return math.sqrt(power) / max(1, len(x))


def band_divergence(a, b, sr=SR):
    a_rms, b_rms, div = [], [], []
    for lo, hi in BANDS:
        ra = _fft_band_rms(a, sr, lo, hi)
        rb = _fft_band_rms(b, sr, lo, hi)
        a_rms.append(ra)
        b_rms.append(rb)
        denom = ra + rb
        div.append(0.0 if denom < 1e-12 else 100.0 * abs(ra - rb) / denom)
    return a_rms, b_rms, div


def divergence_grid(a, b, block_seconds, sr=SR):
    n = min(len(a), len(b))
    block = int(block_seconds * sr)
    if block <= 0:
        return []
    out = []
    for i in range(0, n, block):
        end = min(i + block, n)
        _, _, div = band_divergence(a[i:end], b[i:end], sr)
        out.append((i / sr, div))
    return out


# ── CLI ──────────────────────────────────────────────────────────────────

def _cli_render_it_only(it_path):
    print(f"Rendering {it_path} via libopenmpt...")
    l, r = render_it(it_path)
    print(f"  {len(l) / SR:.2f} s ({len(l)} frames)")
    ref = os.path.splitext(it_path)[0] + '_reference.wav'
    _write_wav(ref, l, r)
    print(f"  wrote {ref}")


def _cli_compare(it_path, ts_path, block_seconds, it_wav=None, ts_wav=None):
    print(f"Loading cached IT render {it_wav}..." if it_wav else f"Rendering {it_path} via libopenmpt...")
    it_l, it_r = load_rendered_wav(it_wav) if it_wav else render_it(it_path)
    print(f"  {len(it_l) / SR:.2f} s ({len(it_l)} frames)")

    print(f"Loading cached Trackscript render {ts_wav}..." if ts_wav else "Rendering .trackscript...")
    if ts_wav:
        ts_l, ts_r = load_rendered_wav(ts_wav)
    else:
        ts_l, ts_r, _ = render_ts(ts_path)
    print(f"  {len(ts_l) / SR:.2f} s ({len(ts_l)} frames)")
    print(f"  duration difference: {len(ts_l) / SR - len(it_l) / SR:+.2f} s (TS - IT)")

    it_stereo = np.column_stack((it_l, it_r)).astype(np.float32)
    ts_stereo = np.column_stack((ts_l, ts_r)).astype(np.float32)
    it_mono = np.mean(it_stereo, axis=1)
    ts_mono = np.mean(ts_stereo, axis=1)

    print()
    print("Finding alignment...")
    lag, corr, corrs, lags = find_lag(it_mono, ts_mono)
    print("  correlation over first 8s, 10 ms steps:")
    for l, c in zip(lags, corrs):
        bar = '#' * max(0, int(round(abs(c) * 20)))
        print(f"    {l * 1000.0 / SR:+8.1f} ms  {c:+6.3f}  {bar}")
    print(f"  best lag (10 ms grid): {lag * 1000.0 / SR:+.1f} ms  corr={corr:+.3f}")

    it_stereo, ts_stereo, _overlap = align_audio(it_stereo, ts_stereo, lag)
    it_mono = np.mean(it_stereo, axis=1)
    ts_mono = np.mean(ts_stereo, axis=1)

    out_dir = os.path.dirname(os.path.abspath(ts_path))
    base = os.path.splitext(os.path.basename(ts_path))[0]
    _write_wav(os.path.join(out_dir, base + '_aligned_it.wav'), it_stereo[:, 0], it_stereo[:, 1])
    _write_wav(os.path.join(out_dir, base + '_aligned_ts.wav'), ts_stereo[:, 0], ts_stereo[:, 1])
    _write_wav(os.path.join(out_dir, base + '_diff.wav'),
               it_stereo[:, 0] - ts_stereo[:, 0],
               it_stereo[:, 1] - ts_stereo[:, 1])
    print()
    print("Wrote:")
    print(f"  {out_dir}/{base}_aligned_it.wav")
    print(f"  {out_dir}/{base}_aligned_ts.wav")
    print(f"  {out_dir}/{base}_diff.wav   <- listen to this")

    print()
    print("Divergence by frequency band (whole aligned song, including tails):")
    a_rms, b_rms, div = band_divergence(it_mono, ts_mono)
    print(f"  {'band':>12}  {'it RMS':>10}  {'ts RMS':>10}  {'div':>7}")
    worst_name, worst_div = None, -1.0
    for name, ra, rb, d in zip(BAND_NAMES, a_rms, b_rms, div):
        marker = ''
        if d > 40:
            marker = '   <'
        print(f"  {name:>12}  {ra:10.5f}  {rb:10.5f}  {d:6.1f}%{marker}")
        if d > worst_div:
            worst_div, worst_name = d, name
    print(f"  worst band: {worst_name} ({worst_div:.1f}%)")

    print()
    print(f"Divergence grid (mean per {block_seconds:g}s block, per band):")
    header = f"  {'time':>8}  " + "  ".join(f"{n:>11}" for n in BAND_NAMES)
    print(header)
    for t, divs in divergence_grid(it_mono, ts_mono, block_seconds):
        mm = int(t) // 60
        ss = t - mm * 60
        row = f"  {mm}:{ss:05.2f}  " + "  ".join(f"{d:10.1f}%" for d in divs)
        print(row)

    print()
    print("Done.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('it_file')
    ap.add_argument('ts_file')
    ap.add_argument('--render-it-only', action='store_true')
    ap.add_argument('--block-seconds', type=float, default=15.0)
    ap.add_argument('--it-wav', help='reuse a previously rendered IT WAV')
    ap.add_argument('--ts-wav', help='reuse a previously rendered Trackscript WAV')
    args = ap.parse_args()

    if args.render_it_only:
        _cli_render_it_only(args.it_file)
    else:
        _cli_compare(args.it_file, args.ts_file, args.block_seconds,
                     args.it_wav, args.ts_wav)


if __name__ == '__main__':
    main()
