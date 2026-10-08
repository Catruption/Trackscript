#!/usr/bin/env python3
"""Diagnose converter-model -> Trackscript representation.

This is NOT an IT correctness proof.  The IT-side input is the converter's
semantics JSON.  Trackscript parsing comes exclusively from trackscript_parser.py,
which is the same parser used by semantic_diff.py.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from trackscript_parser import NoteEvent, parse_pitch_midi, parse_trackscript

DURATION_TOLERANCE_ROWS = 0.03
C5_HZ = 523.2511306


def pitch_value(s):
    if isinstance(s, (int, float)):
        return int(round(float(s)))
    return parse_pitch_midi(str(s).lstrip('#'))


def parse_semantics(path):
    d = json.loads(Path(path).read_text())
    voices = d.get('simulation', {}).get('voices', [])
    if not voices:
        raise ValueError('semantics JSON has no simulation.voices')
    out = []
    for i, v in enumerate(voices):
        start = v.get('start_row', v.get('start'))
        end = v.get('end_row', v.get('end'))
        pitch = v.get('pitch')
        if isinstance(pitch, str):
            pitch = pitch_value(pitch)
        elif pitch is None and v.get('pitch_hz'):
            hz = float(v['pitch_hz'])
            pitch = round(60 + 12 * math.log2(hz / C5_HZ)) if hz > 0 else None
        out.append(dict(
            index=i,
            start=float(start),
            end=float(end) if end is not None else None,
            pitch=pitch,
            sample=v.get('sample'),
            note=v.get('note'),
            reason=v.get('end_reason', v.get('reason')),
            channel=v.get('channel'),
        ))
    return d, out


def key(v):
    return (round(float(v['start']), 6), v.get('pitch'))


def ts_key(v: NoteEvent):
    return (round(float(v.row), 6), v.pitch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('semantics')
    ap.add_argument('trackscript')
    ap.add_argument('--show', type=int, default=20)
    a = ap.parse_args()

    _, converter_voices = parse_semantics(a.semantics)
    ts, ts_info = parse_trackscript(a.trackscript)

    print('=== Pipeline Diagnostic ===')
    print('This is NOT an IT correctness proof.')
    print()
    print('A. Converter model -> Trackscript note/lifetime representation')
    print(f'  converter-model voices: {len(converter_voices)}')
    print(f'  Trackscript note events: {len(ts)}')
    print(f'  Trackscript sections/voices: {ts_info["voices"]}')

    if len(converter_voices) != len(ts):
        print('  ✗ voice/event count differs')
    else:
        print('  ✓ counts agree')

    ci = Counter(key(v) for v in converter_voices)
    ct = Counter(ts_key(v) for v in ts)
    miss = list((ci - ct).elements())
    extra = list((ct - ci).elements())

    print(f'  missing converter voices in TS: {len(miss)}')
    print(f'  extra TS voices:                {len(extra)}')

    if miss:
        print('\n  First missing converter events:')
        for start, pitch in sorted(miss)[:a.show]:
            print(f'    row {start:.2f} pitch {pitch}')
    if extra:
        print('\n  First extra Trackscript events:')
        for start, pitch in sorted(extra)[:a.show]:
            print(f'    row {start:.2f} pitch {pitch}')

    # Lifetime matching is deliberately secondary: starts/pitches must first
    # identify the same note.  Multiple overlapping voices can share a key, so
    # match those as multisets and compare available durations in sorted order.
    lifetime_bad = []
    paired = 0
    by_key = {}
    for v in converter_voices:
        by_key.setdefault(key(v), []).append(v)
    ts_by_key = {}
    for v in ts:
        ts_by_key.setdefault(ts_key(v), []).append(v)

    for k in sorted(set(by_key) & set(ts_by_key)):
        avoices = sorted(by_key[k], key=lambda x: (x['end'] if x['end'] is not None else float('inf'), x['index']))
        bvoices = sorted(ts_by_key[k], key=lambda x: (x.duration_rows if x.duration_rows is not None else float('inf'), x.source))
        for av, bv in zip(avoices, bvoices):
            if av['end'] is None or bv.duration_rows is None:
                continue
            expected = float(av['end']) - float(av['start'])
            actual = float(bv.duration_rows)
            paired += 1
            if abs(expected - actual) > DURATION_TOLERANCE_ROWS:
                lifetime_bad.append((k, expected, actual, av.get('reason')))

    print(f'  lifetime pairs checked:         {paired}')
    print(f'  lifetime mismatches:             {len(lifetime_bad)}')
    if lifetime_bad:
        print('\n  First lifetime mismatches:')
        for (start, pitch), expected, actual, reason in lifetime_bad[:a.show]:
            print(f'    row {start:.2f} pitch {pitch}: converter {expected:.3f} rows, TS {actual:.3f} rows ({reason})')

    ok = not miss and not extra and not lifetime_bad
    if ok:
        print('\n✓ Converter-model representation matches Trackscript.')
    else:
        print('\n✗ Converter-model representation differs from Trackscript.')
    print('\nNote: this only verifies the converter model against the encoded Trackscript; it does not verify the model against an independent IT implementation.')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
