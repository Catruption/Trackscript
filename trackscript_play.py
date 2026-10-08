#!/usr/bin/env python3
"""trackscript_play - reference player for the Trackscript format.

All parsing is done by ``trackscript_parser.parse_file``.  This module is
only responsible for turning the parsed structure into audio:

    TrackscriptFile -> voices -> samples + automation lanes -> stereo PCM

Format:

    Config { BeatsPerMinute: 110; SongVolume: 100%; Subdivisions: 4; ... }
    Tempo { <§M/B,#bpm> &<§M/B,#bpm,~ramp_beats> ... }
    BlockList() { ƒPattern; ƒPattern; ... }
    BlockArray() {
        Pattern = ƒ'samples/01_KICK.wav': { @4/4[ <§beat/subdivision,#C5,~dur_beats,%vol,¶pan> ... ] }
    }

Each score line is one measure. Positions §beat/subdivision are local to that
measure line; the BlockList line places the pattern at that measure in the
arrangement. Repeating a pattern call repeats its measure. Notes may ring past
the end of their pattern line. Durations ~N are in beats.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import warnings

import numpy as np
from scipy.io import wavfile

try:
    from scipy.io.wavfile import WavFileWarning
except ImportError:
    WavFileWarning = Warning
warnings.filterwarnings("ignore", category=WavFileWarning)

# Canonical parser.  No fallbacks: if this import fails, the installation
# is broken and we want to know about it.
from trackscript_parser import parse_file

# Audio-side automation helpers.  These operate on already-parsed tempo
# events and value lanes; they do not parse text.
from trackscript_timing import AutomationLane, TempoMap

SR = 44100
C5_HZ = 523.2511306


# ── Sample loading ────────────────────────────────────────────────────────

def load_samples(base_dir):
    """Load samples.json and its WAVs.

    Returns (by_id, path_to_id): by_id maps sample id -> sample data dict;
    path_to_id maps the relative path used in the .trackscript file to id.
    """
    by_id = {}
    path_to_id = {}
    meta_path = os.path.join(base_dir, 'samples.json')
    if not os.path.exists(meta_path):
        print(f"  (no samples.json in {base_dir})")
        return by_id, path_to_id

    with open(meta_path) as f:
        meta = json.load(f)

    for entry in meta.get('samples', []):
        rel = entry.get('file')
        if not rel:
            continue
        path = os.path.join(base_dir, rel)
        if not os.path.exists(path):
            continue
        sr, data = wavfile.read(path)
        if data.dtype == np.int16:
            data = data.astype(np.float32) / 32768.0
        elif data.dtype == np.uint8:
            data = (data.astype(np.float32) - 128) / 128.0
        else:
            data = data.astype(np.float32)
        if data.ndim == 1:
            data = data[:, None]

        loop = None
        loops = entry.get('loops', {})
        if 'main' in loops:
            ml = loops['main']
            if ml.get('end', 0) > ml.get('start', 0):
                loop = (int(ml['start']), int(ml['end']))

        sid = entry['id']
        by_id[sid] = {
            'data': data,
            'sr': sr,
            'loop': loop,
            'c5': float(entry.get('c5Speed', 8363)),
        }
        path_to_id[rel] = sid
        if rel.startswith('./'):
            path_to_id[rel[2:]] = sid

    print(f"  loaded {len(by_id)} samples")
    return by_id, path_to_id


# ── Audio synthesis ──────────────────────────────────────────────────────

def sinc_interpolate(data, positions, steps, loop=None, raw_positions=None):
    """Eight-tap windowed-sinc interpolation with downsampling anti-aliasing."""
    from trackscript_native import sinc_interpolate as native_sinc
    native_result = native_sinc(data, positions, steps, loop, raw_positions)
    if native_result is not None:
        return native_result
    if data.ndim == 1:
        data = data[:, None]
    if raw_positions is None:
        raw_positions = positions

    frames = len(positions)
    channels = data.shape[1]
    output = np.empty((frames, channels), dtype=np.float32)
    taps = np.arange(-3, 5, dtype=np.int64)
    chunk_size = 32768

    for start in range(0, frames, chunk_size):
        stop = min(start + chunk_size, frames)
        current_positions = positions[start:stop]
        current_steps = np.maximum(steps[start:stop], 1e-12)
        centers = np.floor(current_positions).astype(np.int64)
        sample_indices = centers[:, None] + taps[None, :]
        distance = current_positions[:, None] - sample_indices
        cutoff = np.minimum(1.0, 1.0 / current_steps)
        scaled_distance = distance * cutoff[:, None]
        window = np.where(
            np.abs(distance) < 4.0,
            0.5 + 0.5 * np.cos(np.pi * distance / 4.0),
            0.0,
        )
        weights = cutoff[:, None] * np.sinc(scaled_distance) * window
        weight_sum = weights.sum(axis=1, keepdims=True)
        weights /= np.where(np.abs(weight_sum) < 1e-12, 1.0, weight_sum)

        if loop is not None:
            loop_start, loop_end = loop
            loop_length = loop_end - loop_start
            if loop_length > 0:
                already_looped = raw_positions[start:stop] >= loop_end
                wrap = sample_indices >= loop_end
                wrap |= already_looped[:, None] & (sample_indices < loop_start)
                sample_indices = np.where(
                    wrap,
                    loop_start + (sample_indices - loop_start) % loop_length,
                    sample_indices,
                )

        sample_indices = np.clip(sample_indices, 0, len(data) - 1)
        selected = data[sample_indices].astype(np.float64)
        output[start:stop] = np.sum(
            selected * weights[:, :, None],
            axis=1,
        ).astype(np.float32)

    return output


def pan_gains(volume, pan, law):
    if law == 'linear':
        return volume * (1.0 - pan) * 0.5, volume * (1.0 + pan) * 0.5
    theta = (pan + 1.0) * (math.pi / 4.0)
    return volume * np.cos(theta), volume * np.sin(theta)


def render_voice(smp, start_sec, end_sec, pitch_hz, vol_pct, pan,
                 left, right, offset=0, pitch_lane=None,
                 volume_lane=None, pan_lane=None, volume_scale=1.0,
                 panning_law='equal_power'):
    data = smp['data']
    sample_sr = smp['sr']
    loop = smp['loop']

    start_samp = int(start_sec * SR)
    end_samp = int(end_sec * SR)
    length = end_samp - start_samp
    if length <= 0:
        return

    pitch_varies = pitch_lane is not None and len(pitch_lane.times) > 1
    volume_varies = volume_lane is not None and len(volume_lane.times) > 1
    pan_varies = pan_lane is not None and len(pan_lane.times) > 1
    sample_offsets = None
    if pitch_varies or volume_varies or pan_varies:
        sample_offsets = (start_samp + np.arange(length, dtype=np.float64)) / SR

    if pitch_varies:
        pitch_values = np.interp(sample_offsets, pitch_lane.times, pitch_lane.values)
        steps = sample_sr * (pitch_values / C5_HZ) / SR
        raw_positions = offset + np.concatenate((
            np.zeros(1, dtype=np.float64),
            np.cumsum(steps[:-1]),
        ))
    else:
        step = sample_sr * (pitch_hz / C5_HZ) / SR
        steps = np.full(length, step, dtype=np.float64)
        raw_positions = offset + np.arange(length, dtype=np.float64) * step

    if volume_varies:
        volume_values = np.interp(sample_offsets, volume_lane.times, volume_lane.values)
    else:
        volume_values = np.full(length, vol_pct, dtype=np.float64)
    volume_values *= volume_scale

    if pan_varies:
        pan_values = np.interp(sample_offsets, pan_lane.times, pan_lane.values)
    else:
        pan_values = np.full(length, pan, dtype=np.float64)
    positions = raw_positions.copy()

    if loop is not None:
        ls, le = loop
        ll = le - ls
        if ll > 0:
            over = positions >= le
            positions[over] = ls + ((positions[over] - le) % ll)

    mask = (positions >= 0) & (positions < len(data))
    if not mask.any():
        return
    first = int(np.argmax(mask))
    interp = sinc_interpolate(
        data,
        positions[mask],
        steps[mask],
        loop,
        raw_positions[mask],
    )
    volume_values = volume_values[mask] / 100.0
    pan_values = np.clip(pan_values[mask], -1.0, 1.0)
    if interp.shape[1] == 1:
        mono = interp[:, 0]
        interp = np.stack([mono, mono], axis=1)

    lg, rg = pan_gains(volume_values, pan_values, panning_law)
    ln = interp[:, 0] * lg
    rn = interp[:, 1] * rg

    ws = start_samp + first
    we = ws + len(ln)
    if ws >= len(left):
        return
    if we > len(left):
        we = len(left)
        ln = ln[:we - ws]
        rn = rn[:we - ws]
    left[ws:we] += ln
    right[ws:we] += rn


# ── Voice collection from a parsed file ─────────────────────────────────

def collect_voices(parsed, path_to_id, samples_by_id, fallback_hz):
    """Walk BlockList -> blocks -> parts -> events, producing voice dicts.

    A voice dict carries the note-on plus per-property automation lists
    collected from subsequent waypoints.  Ordering matches the emitter's
    contract: a note-on and all of its waypoints appear consecutively.
    """
    voices = []
    for line_index, line in enumerate(parsed.block_list):
        for block_name in line:
            part_names = parsed.blocks.get(block_name, [block_name])
            for part_name in part_names:
                part = parsed.parts.get(part_name)
                if part is None:
                    continue
                default_sid = path_to_id.get(part.sample_path)
                subs = part.subs_per_beat
                placement_row = line_index * part.beats_per_measure * subs
                last_voice = None
                for ev in part.events:
                    if not ev.is_waypoint:
                        sid = default_sid
                        if ev.sample_override:
                            sid = path_to_id.get(ev.sample_override, default_sid)
                        if sid is None or sid not in samples_by_id:
                            continue
                        dur_rows = (ev.dur_beats or 1.0) * subs
                        voice = {
                            'start_row': placement_row + ev.row,
                            'end_row': placement_row + ev.row + dur_rows,
                            'pitch_hz': ev.pitch_hz if ev.pitch_hz is not None else fallback_hz,
                            'vol': ev.vol if ev.vol is not None else 100.0,
                            'pan': ev.pan if ev.pan is not None else 0.0,
                            'automation': {'pitch_hz': [], 'vol': [], 'pan': []},
                            'sample': samples_by_id[sid],
                            'offset': ev.offset if ev.offset is not None else part.sample_offset,
                        }
                        voices.append(voice)
                        last_voice = voice
                    else:
                        if last_voice is None:
                            continue
                        duration_rows = (ev.dur_beats or 0.0) * subs
                        for target, key in (
                            (ev.pitch_hz, 'pitch_hz'),
                            (ev.vol, 'vol'),
                            (ev.pan, 'pan'),
                        ):
                            if target is not None:
                                last_voice['automation'][key].append(
                                    (placement_row + ev.row, duration_rows, target)
                                )
    return voices


# ── Main render ──────────────────────────────────────────────────────────

def render(ts_path, out_wav_path):
    ts_path = str(ts_path)
    base_dir = os.path.dirname(os.path.abspath(ts_path))

    parsed = parse_file(ts_path)

    cfg = parsed.config
    bpm = float(cfg.get('BeatsPerMinute', 120.0))
    song_vol = float(cfg.get('SongVolume', 100.0)) / 100.0
    panning_law = str(cfg.get('PanningLaw', 'equal_power')).strip().lower()
    subs_per_beat = int(cfg.get('Subdivisions', 4))

    tempo_map = TempoMap(bpm, parsed.tempo_events, subs_per_beat)

    samples_by_id, path_to_id = load_samples(base_dir)

    print(f"  parsed {len(parsed.parts)} parts, {len(parsed.blocks)} blocks, "
          f"{len(parsed.block_list)} BlockList lines")

    if not parsed.parts:
        wavfile.write(out_wav_path, SR, np.zeros((SR, 2), dtype=np.int16))
        return

    all_voices = collect_voices(parsed, path_to_id, samples_by_id, C5_HZ)

    if not all_voices:
        wavfile.write(out_wav_path, SR, np.zeros((SR, 2), dtype=np.int16))
        return

    print(f"  collected {len(all_voices)} voices")

    if parsed.parts:
        rows_per_measure = max(
            part.beats_per_measure * part.subs_per_beat
            for part in parsed.parts.values()
        )
    else:
        signature = cfg.get('TimeSignature', (4, 4))
        beats_per_measure = signature[0] if isinstance(signature, tuple) else 4
        rows_per_measure = beats_per_measure * subs_per_beat
    # The arrangement ends at the last BlockList measure, while voices with
    # explicit envelope or note tails may continue beyond it.
    total_rows = len(parsed.block_list) * rows_per_measure
    song_end_sec = tempo_map.time_at_row(total_rows)
    # Match OpenMPT's short render tail while allowing final envelope releases
    # to continue after the last arrangement row. Do not extend to arbitrary
    # note lengths: tracker files can contain very long held notes at EOF.
    render_end_sec = song_end_sec + 0.5
    total_seconds = render_end_sec
    total_samples = int(total_seconds * SR)
    left = np.zeros(total_samples, dtype=np.float32)
    right = np.zeros(total_samples, dtype=np.float32)

    render_started = time.monotonic()
    for voice_index, v in enumerate(all_voices, 1):
        start_sec = tempo_map.time_at_row(v['start_row'])
        end_sec = min(
            tempo_map.time_at_row(v['end_row']),
            render_end_sec,
        )
        lanes = {
            key: AutomationLane(
                v[key],
                v['start_row'],
                v['automation'][key],
                tempo_map,
            )
            for key in ('pitch_hz', 'vol', 'pan')
        }
        render_voice(
            v['sample'], start_sec, end_sec,
            v['pitch_hz'], v['vol'], v['pan'],
            left, right, offset=v['offset'],
            pitch_lane=lanes['pitch_hz'],
            volume_lane=lanes['vol'],
            pan_lane=lanes['pan'],
            volume_scale=song_vol,
            panning_law=panning_law,
        )
        if voice_index % 100 == 0 or voice_index == len(all_voices):
            elapsed = time.monotonic() - render_started
            print(
                f"  rendered {voice_index}/{len(all_voices)} voices "
                f"({elapsed:.1f}s elapsed)"
            )

    peak = max(float(np.max(np.abs(left))) if len(left) else 0.0,
               float(np.max(np.abs(right))) if len(right) else 0.0)
    clipped = int(np.count_nonzero(np.abs(left) > 1.0))
    clipped += int(np.count_nonzero(np.abs(right) > 1.0))
    if clipped:
        print(f"  warning: {clipped} output samples clip at peak {peak:.3f}")

    stereo = np.stack([left, right], axis=1)
    wavfile.write(out_wav_path, SR,
                  (np.clip(stereo, -1, 1) * 32767).astype(np.int16))
    print(f"  wrote {out_wav_path} ({total_seconds:.2f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('trackscript')
    ap.add_argument('out_wav')
    args = ap.parse_args()
    render(args.trackscript, args.out_wav)


if __name__ == '__main__':
    main()
