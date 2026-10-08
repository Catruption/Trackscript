#!/usr/bin/env python3
"""timeline_report - where does the Trackscript render stop matching the .it?

Renders both sides (libopenmpt and trackscript_play), assumes both start at
time 0 (no global lag search), and prints one row per window:

  it dB / ts dB   RMS level (overall gain offset removed first)
  diff            ts - it in dB (positive = Trackscript louder)
  corr0           waveform correlation at lag 0 (1.0 = same audio)
  lag ms          best local offset within +/- max-lag (positive = TS late)
  corr@lag        correlation at that offset
  flag            what kind of problem this window has

Flags:
  TS SILENT   IT has sound, Trackscript doesn't
  IT SILENT   Trackscript has sound where the IT is silent
  TIMING      right audio, wrong place (lag column says how far)
  LEVEL       right-ish audio, wrong loudness
  CONTENT     wrong waveform at about the right level

Usage:
    python3 timeline_report.py song.it song.trackscript
    python3 timeline_report.py song.it song.trackscript --win 1 --all
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from compare_renditions import render_it, render_ts, load_rendered_wav, SR

SILENT_DB = -60.0


def rms_db(x):
    if not len(x):
        return -180.0
    r = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20.0 * np.log10(max(r, 1e-9))


def last_loud(x, thresh=1e-3):
    idx = np.nonzero(np.abs(x) > thresh)[0]
    return idx[-1] / SR if len(idx) else 0.0


def fmt_time(s):
    return f"{int(s) // 60}:{s % 60:05.2f}"


def best_lag(a, b_ext, ml):
    """Return (lag_samples, corr_at_best, corr_at_zero). Positive lag means
    TS plays the same audio later than the IT."""
    n = len(a)
    size = 1 << int(np.ceil(np.log2(n + len(b_ext))))
    cc = np.fft.irfft(
        np.conj(np.fft.rfft(a, size)) * np.fft.rfft(b_ext, size), size,
    )[:2 * ml + 1]
    cs = np.concatenate([[0.0], np.cumsum(b_ext.astype(np.float64) ** 2)])
    seg = cs[n:n + 2 * ml + 1] - cs[:2 * ml + 1]
    denom = np.linalg.norm(a) * np.sqrt(np.maximum(seg, 1e-18))
    c = cc / np.maximum(denom, 1e-12)
    k = int(np.argmax(c))
    return k - ml, float(c[k]), float(c[ml])


def _fit_once(a, b, win, ml, min_corr=0.25, max_lag_ms=100.0, tol_ms=5.0):
    bp = np.pad(b, (ml, ml + win))
    ts, lags = [], []
    for w in range(len(a) // win):
        i = w * win
        aw = a[i:i + win]
        if rms_db(aw) < SILENT_DB:
            continue
        lag, c, _ = best_lag(aw, bp[i:i + win + 2 * ml], ml)
        if c >= min_corr and abs(lag) <= max_lag_ms * SR / 1000:
            ts.append(i + win / 2.0)
            lags.append(lag)
    if len(ts) < 8:
        return None
    ts_arr = np.array(ts, float)
    lags_arr = np.array(lags, float)
    keep = np.ones(len(ts_arr), bool)
    slope, off = 0.0, 0.0
    for _ in range(4):
        slope, off = np.polyfit(ts_arr[keep], lags_arr[keep], 1)
        new = np.abs(lags_arr - (off + slope * ts_arr)) <= tol_ms * SR / 1000
        if new.sum() < 8 or (new == keep).all():
            break
        keep = new
    return off, slope


def fit_drift(a, b, win, ml):
    first = _fit_once(a, b, win, ml)
    if first is None:
        return None
    off, slope = first
    idx = np.arange(len(b), dtype=np.float64)
    for _ in range(3):
        x = idx * (1.0 + slope) + off
        bw = np.interp(x, idx, b).astype(np.float32)
        r = _fit_once(a, bw, win, int(0.010 * SR),
                      min_corr=0.2, max_lag_ms=8.0, tol_ms=1.0)
        if r is None:
            break
        off += r[0]
        slope += r[1]
    return off, slope


def longest_note_beats(ts_path):
    try:
        text = Path(ts_path).read_text(encoding='utf-8')
    except OSError:
        return None
    best = 0.0
    for m in re.finditer(r'~(?:length)?\s*([\d.]+)', text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if v > best:
            best = v
    return best if best > 0 else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("it_file")
    ap.add_argument("trackscript_file")
    ap.add_argument("--win", type=float, default=2.0, help="window seconds")
    ap.add_argument("--max-lag-ms", type=float, default=250.0)
    ap.add_argument("--no-drift", action="store_true",
                    help="do not estimate/remove constant clock drift")
    ap.add_argument("--all", action="store_true",
                    help="also print windows that look fine")
    ap.add_argument("--strict", action="store_true",
                    help="exit nonzero when any mismatch window is found")
    ap.add_argument("--it-wav", help="reuse a previously rendered IT WAV")
    ap.add_argument("--ts-wav", help="reuse a previously rendered Trackscript WAV")
    args = ap.parse_args()

    ln = longest_note_beats(args.trackscript_file)
    if ln is not None:
        print(f"  longest note in file: {ln:.2f} beats")

    if args.it_wav:
        print(f"Loading cached IT render {args.it_wav}...")
        la, ra = load_rendered_wav(args.it_wav)
    else:
        print("Rendering .it via libopenmpt...")
        la, ra = render_it(args.it_file)
    if args.ts_wav:
        print(f"Loading cached Trackscript render {args.ts_wav}...")
        lb, rb = load_rendered_wav(args.ts_wav)
    else:
        print("Rendering .trackscript...")
        lb, rb, _ = render_ts(args.trackscript_file)

    a = ((la + ra) * 0.5).astype(np.float32)
    b = ((lb + rb) * 0.5).astype(np.float32)

    print()
    print(f"  IT length:          {fmt_time(len(a) / SR)}   "
          f"(last audible {fmt_time(last_loud(a))})")
    print(f"  Trackscript length: {fmt_time(len(b) / SR)}   "
          f"(last audible {fmt_time(last_loud(b))})")

    n = len(a)
    if len(b) < n:
        b = np.pad(b, (0, n - len(b)))

    if not args.no_drift:
        fit = fit_drift(a, b, int(args.win * SR),
                        int(args.max_lag_ms * SR / 1000))
        if fit is None:
            print("  clock drift: not enough clean windows to estimate")
        else:
            off, slope = fit
            x = np.arange(len(b), dtype=np.float64) * (1.0 + slope) + off
            b = np.interp(x, np.arange(len(b)), b).astype(np.float32)
            print(f"  clock drift: Trackscript runs {slope * 100:+.4f}% slow "
                  f"(offset {off * 1000 / SR:+.1f} ms); corrected before comparing")

    b_cmp = b[:n]

    gain_db = rms_db(a) - rms_db(b_cmp)
    b = b * (10 ** (gain_db / 20.0))
    print(f"  overall gain offset: Trackscript is {-gain_db:+.1f} dB vs IT "
          f"(removed below)")
    print()

    win = int(args.win * SR)
    ml = int(args.max_lag_ms * SR / 1000)
    bp = np.pad(b, (ml, ml + win))

    print(f"{'time':>8}  {'it dB':>7}  {'ts dB':>7}  {'diff':>6}  "
          f"{'corr0':>6}  {'lag ms':>7}  {'corr@lag':>8}  flag")

    first = {}
    counts = {}
    nwin = n // win

    for w in range(nwin):
        i = w * win
        aw = a[i:i + win]
        bw = b[i:i + win]
        adb, bdb = rms_db(aw), rms_db(bw)
        t = i / SR
        flags = []
        lag_ms = 0.0
        c_best = c0 = 0.0

        if adb < SILENT_DB and bdb < SILENT_DB:
            if not args.all:
                continue
        elif bdb < SILENT_DB:
            flags.append("TS SILENT")
        elif adb < SILENT_DB:
            flags.append("IT SILENT")
        else:
            b_ext = bp[i:i + win + 2 * ml]
            lag, c_best, c0 = best_lag(aw, b_ext, ml)
            lag_ms = lag * 1000.0 / SR
            diff = bdb - adb
            if c0 >= 0.8:
                if abs(diff) > 3:
                    flags.append("LEVEL")
            elif abs(lag_ms) > 5 and c_best >= 0.8 and c_best - c0 > 0.3:
                flags.append("TIMING")
                if abs(diff) > 3:
                    flags.append("LEVEL")
            else:
                if abs(diff) > 3:
                    flags.append("LEVEL")
                flags.append("CONTENT")

        for f in flags:
            counts[f] = counts.get(f, 0) + 1
            first.setdefault(f, t)

        if flags or args.all:
            print(f"{fmt_time(t):>8}  {adb:7.1f}  {bdb:7.1f}  "
                  f"{bdb - adb:+6.1f}  {c0:6.2f}  {lag_ms:+7.1f}  "
                  f"{c_best:8.2f}  {', '.join(flags)}")

    print()
    print(f"summary ({nwin} windows of {args.win:g}s):")
    if not counts:
        print("  no problem windows")
    for f, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {f:10s} {c:4d} windows, first at {fmt_time(first[f])}")
    if args.strict and counts:
        print("  strict check failed: audio mismatches were found")
        sys.exit(1)


if __name__ == '__main__':
    main()
