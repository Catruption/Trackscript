#!/usr/bin/env python3
"""Canonical parser for the Trackscript format.

This is the ONLY parser for .trackscript files.  Every consumer — the
player, semantic_diff, pipeline_diagnose, timeline_report — reads from
``parse_file()``.

Pitch is deliberately exposed twice:

  * ``pitch_midi``  an integer MIDI note (semantic comparisons want this)
  * ``pitch_hz``    a frequency in Hz     (the audio renderer wants this)

Both derive from the same source text.  Do not import ``parse_pitch`` from
anywhere else; that ambiguity is what let the player render C5 as 60 Hz.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

C5_HZ = 523.2511306
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


# ── data model ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NoteEvent:
    """A note-on, flattened across all parts.  Used by diagnostics."""
    row: float
    pitch: int
    pitch_hz: float
    voice: str
    sample: str | None
    duration_rows: float | None
    duration_beats: float | None
    section: int
    source: str


@dataclass
class TrackscriptEvent:
    """One event inside a part.  Note-on or waypoint."""
    op: str                                    # '', '&', '=', '▲', '♪'
    row: float
    pitch_midi: int | None = None
    pitch_hz: float | None = None
    vol: float | None = None
    pan: float | None = None
    dur_beats: float | None = None
    sample_override: str | None = None
    offset: int | None = None

    @property
    def is_waypoint(self) -> bool:
        return self.op == '&'


@dataclass
class TrackscriptPart:
    name: str
    sample_path: str
    beats_per_measure: int
    subs_per_beat: int
    len_beats: float | None
    sample_offset: int = 0
    events: list[TrackscriptEvent] = field(default_factory=list)


@dataclass
class TrackscriptFile:
    config: dict
    tempo_events: list[tuple[float, float, float]]     # (row, bpm, ramp_beats)
    block_list: list[list[str]]
    blocks: dict[str, list[str]]                       # name -> part names
    parts: dict[str, TrackscriptPart]
    notes: list[NoteEvent]                             # flat, sorted
    info: dict


# ── pitch ────────────────────────────────────────────────────────────────

def note_to_hz(note: int) -> float:
    return C5_HZ * 2.0 ** ((note - 60) / 12.0)


def hz_to_note(hz: float) -> int:
    return int(round(60 + 12.0 * math.log2(hz / C5_HZ)))


def _parse_note_name(text: str) -> int | None:
    m = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", text.strip())
    if not m:
        return None
    name, accidental, octave = m.groups()
    base = NOTE_NAMES.index(name.upper())
    if accidental == '#':
        base += 1
    elif accidental == 'b':
        base -= 1
    return int(octave) * 12 + base


def parse_pitch_midi(text: str) -> int | None:
    """Note name or numeric Hz -> MIDI note.  For semantic comparisons."""
    note = _parse_note_name(text)
    if note is not None:
        return note
    try:
        hz = float(text.strip())
    except ValueError:
        return None
    return hz_to_note(hz) if hz > 0 else None


def parse_pitch_hz(text: str) -> float | None:
    """Note name or numeric Hz -> frequency in Hz.  For the audio renderer."""
    note = _parse_note_name(text)
    if note is not None:
        return note_to_hz(note)
    try:
        hz = float(text.strip())
    except ValueError:
        return None
    return hz if hz > 0 else None


# ── low-level text utilities ─────────────────────────────────────────────

def _strip_comments(text: str) -> str:
    """Remove // ... to end of line, respecting single-quoted strings."""
    lines = []
    for line in text.split('\n'):
        in_quote = False
        cut = len(line)
        i = 0
        while i < len(line):
            c = line[i]
            if c == "'":
                in_quote = not in_quote
            elif not in_quote and c == '/' and i + 1 < len(line) and line[i+1] == '/':
                cut = i
                break
            i += 1
        lines.append(line[:cut])
    return '\n'.join(lines)


def _match_pair(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return index of the matching close_ch for text[start] == open_ch."""
    if text[start] != open_ch:
        raise ValueError(f"expected {open_ch!r} at {start}")
    depth = 1
    i = start + 1
    in_quote = False
    while i < len(text):
        c = text[i]
        if c == "'":
            in_quote = not in_quote
        elif not in_quote:
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(text)


def _extract_block(text: str, header_rx: str) -> str | None:
    """Find `header_rx { ... }` and return the body between the braces."""
    m = re.search(header_rx, text)
    if not m:
        return None
    brace = text.find('{', m.end() - 1)
    if brace < 0:
        return None
    close = _match_pair(text, brace, '{', '}')
    return text[brace+1:close]


def _split_fields(body: str) -> list[str]:
    """Split a comma-separated note body, respecting quoted strings."""
    out, cur, quote = [], [], None
    for c in body:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            continue
        if c == ',':
            out.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if cur:
        out.append(''.join(cur).strip())
    return out


# ── sub-parsers ──────────────────────────────────────────────────────────

def _parse_config(body: str | None) -> dict:
    cfg: dict = {}
    if body is None:
        return cfg
    for entry in body.split(';'):
        if ':' not in entry:
            continue
        key, _, value = entry.partition(':')
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if key == 'BeatsPerMinute':
            try: cfg[key] = float(value)
            except ValueError: cfg[key] = value
        elif key == 'Subdivisions':
            try: cfg[key] = int(value)
            except ValueError: cfg[key] = value
        elif key == 'SongVolume':
            try: cfg[key] = float(value.rstrip('%'))
            except ValueError: cfg[key] = value
        elif key == 'TimeSignature':
            try:
                a, b = value.split('/')
                cfg[key] = (int(a), int(b))
            except (ValueError, TypeError):
                cfg[key] = value
        else:
            cfg[key] = value
    return cfg


def _parse_tempo(body: str | None, subs: int) -> list[tuple[float, float, float]]:
    """Return [(absolute_row, bpm, ramp_beats), ...]. Each line is a measure."""
    events: list[tuple[float, float, float]] = []
    if body is None:
        return events
    body = body.strip()
    measure_rows = 4 * subs
    for em in re.finditer(r'[&]?\s*<([^<>]+)>', body):
        row = bpm = None
        ramp = 0.0
        for f in (x.strip() for x in em.group(1).split(',')):
            if f.startswith('§'):
                try:
                    a, b = f[1:].split('/', 1)
                    local_row = (float(a) - 1) * subs + float(b) - 1
                    measure = body.count('\n', 0, em.start())
                    row = measure * measure_rows + local_row
                except (ValueError, TypeError):
                    row = None
            elif f.startswith('#'):
                try: bpm = float(f[1:])
                except ValueError: pass
            elif f.startswith('~'):
                try: ramp = float(f[1:].lstrip('+'))
                except ValueError: pass
        if row is not None and bpm is not None and math.isfinite(row + bpm + ramp):
            events.append((row, bpm, ramp))
    return events


def _parse_block_list(body: str | None) -> list[list[str]]:
    """Each line is a list of block names that play together."""
    if body is None:
        return []
    lines: list[list[str]] = []
    for raw in body.strip().split('\n'):
        names = re.findall(r'ƒ([A-Za-z_][A-Za-z0-9_]*)', raw)
        if names or raw.strip() == ';':
            lines.append(names)
    return lines


EVENT_RX = re.compile(r"(?P<op>[♪&=▲]?)\s*<(?P<body>[^<>]*)>")


def _parse_part_events(body: str, subs: int, beats_per_measure: int) -> list[TrackscriptEvent]:
    events: list[TrackscriptEvent] = []
    body = body.strip()
    for em in EVENT_RX.finditer(body):
        op = em.group('op')
        fields = _split_fields(em.group('body'))
        beat = sub = None
        ev = TrackscriptEvent(op=op, row=0.0)
        for f in fields:
            if f.startswith('§'):
                try:
                    a, b = f[1:].split('/', 1)
                    beat = float(a)
                    sub = float(b) - 1.0
                except (ValueError, TypeError):
                    beat = sub = None
            elif f.startswith('#'):
                ev.pitch_midi = parse_pitch_midi(f[1:])
                ev.pitch_hz = parse_pitch_hz(f[1:])
            elif f.startswith('~'):
                try: ev.dur_beats = float(f[1:].lstrip('+'))
                except ValueError: pass
            elif f.startswith('%'):
                try: ev.vol = float(f[1:].lstrip('+'))
                except ValueError: pass
            elif f.startswith('¶'):
                try: ev.pan = float(f[1:])
                except ValueError: pass
            elif f.startswith('ƒ'):
                # A sound source is callable: ƒ'path'() or ƒ'path'(offset).
                # Keep the old +offset spelling readable for existing files.
                m2 = re.match(r"ƒ'([^']+)'(?:\((\d*)\)|\+(\d+))?", f)
                if m2:
                    ev.sample_override = m2.group(1)
                    raw_offset = m2.group(2) or m2.group(3)
                    ev.offset = int(raw_offset) if raw_offset else 0
        if beat is None or sub is None:
            continue
        measure = body.count('\n', 0, em.start())
        ev.row = measure * beats_per_measure * subs + (beat - 1.0) * subs + sub
        events.append(ev)
    return events


def _parse_block_array(body: str | None) -> tuple[dict[str, list[str]], dict[str, TrackscriptPart]]:
    blocks: dict[str, list[str]] = {}
    parts: dict[str, TrackscriptPart] = {}
    if body is None:
        return blocks, parts

    name_rx = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*')
    pos = 0
    while pos < len(body):
        m = name_rx.search(body, pos)
        if not m:
            break
        name = m.group(1)
        j = m.end()
        while j < len(body) and body[j] in ' \t\n':
            j += 1
        if j >= len(body):
            break
        c = body[j]

        if c == '{':
            # Composite block: name = { ƒP1, ƒP2; }
            close = _match_pair(body, j, '{', '}')
            inner = body[j+1:close]
            blocks[name] = re.findall(r'ƒ([A-Za-z_][A-Za-z0-9_]*)', inner)
            pos = close + 1

        elif c == 'ƒ':
            # Part: name = ƒ'path': { @N/D[ ... ] }
            sm = re.match(r"ƒ'([^']+)'(?:\((\d*)\))?\s*:\s*", body[j:])
            if not sm:
                pos = j + 1
                continue
            sample_path = sm.group(1)
            sample_offset = int(sm.group(2) or 0)
            k = j + sm.end()
            while k < len(body) and body[k] in ' \t\n':
                k += 1
            if k >= len(body) or body[k] != '{':
                pos = j + 1
                continue
            close = _match_pair(body, k, '{', '}')
            inner = body[k+1:close]

            hm = re.match(
                r'\s*@(\d+)\s*/\s*(\d+)\s*(?:~([\d.]+)\s*)?\[',
                inner,
            )
            if not hm:
                pos = close + 1
                continue
            beats = int(hm.group(1))
            subs = int(hm.group(2))
            len_beats = float(hm.group(3)) if hm.group(3) else None
            open_bracket = hm.end() - 1
            close_bracket = _match_pair(inner, open_bracket, '[', ']')
            event_body = inner[open_bracket+1:close_bracket]

            parts[name] = TrackscriptPart(
                name=name,
                sample_path=sample_path,
                sample_offset=sample_offset,
                beats_per_measure=beats,
                subs_per_beat=subs,
                len_beats=len_beats,
                events=_parse_part_events(event_body, subs, beats),
            )
            pos = close + 1

        else:
            pos = j + 1

    return blocks, parts


# ── top level ────────────────────────────────────────────────────────────

def parse_file(path: str | Path) -> TrackscriptFile:
    text = _strip_comments(Path(path).read_text(encoding='utf-8'))

    config = _parse_config(_extract_block(text, r'\bConfig\s*\{'))
    subs = int(config.get('Subdivisions', 4))

    tempo_events = _parse_tempo(_extract_block(text, r'\bTempo\s*\{'), subs)
    block_list = _parse_block_list(
        _extract_block(text, r'\bBlockList\s*\([^)]*\)\s*\{')
    )
    blocks, parts = _parse_block_array(
        _extract_block(text, r'\bBlockArray\s*\([^)]*\)\s*\{')
    )

    notes: list[NoteEvent] = []
    info = {
        'voices': len(parts),
        'sections_with_events': 0,
        'malformed_pitch_events': 0,
        'sections': [],
    }
    part_counts = {name: 0 for name in parts}
    for placement_index, calls in enumerate(block_list):
        for call in calls:
            part_names = blocks.get(call, [call])
            for part_name in part_names:
                part = parts.get(part_name)
                if part is None:
                    continue
                placement_row = placement_index * part.beats_per_measure * part.subs_per_beat
                for ev in part.events:
                    if ev.is_waypoint:
                        continue
                    if ev.pitch_midi is None or ev.pitch_hz is None:
                        info['malformed_pitch_events'] += 1
                        continue
                    dur_rows = ev.dur_beats * part.subs_per_beat if ev.dur_beats is not None else None
                    notes.append(NoteEvent(
                        row=placement_row + ev.row,
                        pitch=ev.pitch_midi,
                        pitch_hz=ev.pitch_hz,
                        voice=f'{part_name}@{placement_index}',
                        sample=ev.sample_override or part.sample_path,
                        duration_rows=dur_rows,
                        duration_beats=ev.dur_beats,
                        section=placement_index,
                        source=f'TS row {placement_row + ev.row:g} {part_name}',
                    ))
                    part_counts[part_name] = part_counts.get(part_name, 0) + 1
    for name, part in parts.items():
        count = part_counts.get(name, 0)
        if count:
            info['sections_with_events'] += 1
        info['sections'].append({
            'voice': name,
            'sample': part.sample_path,
            'events': count,
        })
    notes.sort(key=lambda n: (n.row, n.pitch, n.voice))

    return TrackscriptFile(
        config=config,
        tempo_events=tempo_events,
        block_list=block_list,
        blocks=blocks,
        parts=parts,
        notes=notes,
        info=info,
    )


def parse_trackscript(path: str | Path):
    """Compatibility wrapper: returns (notes, info) for diagnostics."""
    f = parse_file(path)
    return f.notes, f.info
