#!/usr/bin/env python3
"""
semantic_diff - compare an Impulse Tracker module with its Trackscript
conversion at a normalized musical-event level.

The important rule is that IT channels and Trackscript simulated voices are
NOT treated as the same thing.  Both sides are converted into semantic
streams first, then compared by musical time and event meaning.

Current semantic layer:
  * note-on
  * note-off / cut / fade / continue markers from IT
  * instrument/sample identity where it can be recovered
  * volume
  * Trackscript pitch/frequency

The comparison deliberately starts conservatively.  It does not claim that
an effect or envelope matches until we have an explicit semantic mapping for
that feature.

Usage:
    python3 semantic_diff.py song.semantics.json song.trackscript
    python3 semantic_diff.py song.semantics.json song.trackscript --around 20
    python3 semantic_diff.py song.semantics.json song.trackscript --json trace.json
    python3 semantic_diff.py song.semantics.json song.trackscript --json -
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from it_parser import note_termination
from trackscript_parser import parse_pitch_midi, parse_trackscript

C5_HZ = 523.2511306
NOTE_NAMES = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
]

@dataclass(frozen=True)
class Event:
    row: int
    kind: str
    pitch: int | None = None
    channel: int | None = None
    voice: str | None = None
    instrument: int | None = None
    sample: str | None = None
    volume: float | None = None
    command: int | None = None
    parameter: int | None = None
    source: str | None = None

    def key(self) -> tuple[Any, ...]:
        """Comparison key for semantic equality.

        Channel/voice identity is intentionally excluded.  A single IT
        channel can become several Trackscript voices because of NNA/overlap.
        """
        return (
            self.row,
            self.kind,
            self.pitch,
            self.instrument,
            self.sample,
            self._norm_volume(),
            self.command,
            self.parameter,
        )

    def note_key(self) -> tuple[int, int]:
        return (self.row, self.pitch if self.pitch is not None else -1)

    def _norm_volume(self) -> float | None:
        if self.volume is None:
            return None
        return round(float(self.volume), 4)


def note_name(midi: int | None) -> str:
    if midi is None:
        return "-"
    return f"{NOTE_NAMES[midi % 12]}{midi // 12}"



def semantics_events(path: str | Path) -> tuple[list[Event], dict[str, Any]]:
    """Load the converter's IT-side semantic interpretation.

    There are deliberately two layers in the semantics JSON:

      * ``it_decode`` contains the literal pattern data (raw IT notes).
      * ``simulation`` contains the converter's playback interpretation.

    For note-ons we compare the *simulation* layer, because IT instruments
    can remap a pattern note through their Note-Sample/Keyboard table.  A raw
    C#5 pattern event can therefore legitimately become D5 at the sample
    playback pitch.  Comparing the raw pattern number directly to Trackscript
    would report a false pitch error.

    Special note events (off/cut/fade) still come from the decoded stream,
    since the current simulation represents their resulting voice termination
    rather than preserving them as standalone events.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "trackscript-semantics":
        raise ValueError(f"not a Trackscript semantics file: {path}")

    events: list[Event] = []
    rows = data.get("it_decode", {}).get("rows_with_events", [])
    by_pattern_row = {(x["pattern"], x["row"]): x["events"] for x in rows}

    # Canonical simulated note-ons.  Voice.note is already the post-keymap
    # playback note, and pitch_hz is the exact pitch the converter emitted.
    voices = data.get("simulation", {}).get("voices", [])
    for v in voices:
        pitch = None
        if v.get("note"):
            pitch = parse_pitch_midi(str(v["note"]))
        if pitch is None and v.get("pitch_hz"):
            pitch = round(60 + 12 * math.log2(float(v["pitch_hz"]) / C5_HZ))
        if pitch is None:
            continue
        events.append(Event(
            row=int(round(v["start_row"])),
            kind="note_on",
            pitch=pitch,
            channel=v.get("channel"),
            voice=f"V{v.get('voice')}",
            instrument=v.get("instrument"),
            sample=(f"{int(v['sample']):02d}" if v.get("sample") is not None else None),
            volume=float(v["volume"]) if v.get("volume") is not None else None,
            source=f"IT simulation row {v['start_row']} voice {v.get('voice')}",
        ))

    # Preserve decoded note termination events as semantic events.  Do not add
    # decoded note-ons here: those have already been replaced by the simulated
    # post-keymap note-ons above.
    executed = data.get("simulation", {}).get("executed_rows", [])
    for item in executed:
        abs_row = int(item["absolute_row"])
        key = (int(item["pattern"]), int(item["pattern_row"]))
        for e in by_pattern_row.get(key, []):
            note_raw = e.get("note_raw")
            termination = note_termination(note_raw)
            if termination is None:
                continue
            events.append(Event(
                row=abs_row,
                kind=f"note_{termination}",
                channel=e.get("channel"),
                instrument=e.get("instrument"),
                command=e.get("effect_raw"),
                parameter=e.get("parameter"),
                source=f"IT decoded row {abs_row} ch {e.get('channel')}",
            ))

    events.sort(key=lambda e: (e.row, e.kind, e.pitch if e.pitch is not None else -1, e.voice or ""))
    return events, data


def trackscript_events(path: str | Path) -> tuple[list[Event], dict[str, Any]]:
    """Adapt the canonical Trackscript parser to semantic-diff Events."""
    notes, info = parse_trackscript(path)
    events = [
        Event(
            row=int(round(n.row)),
            kind="note_on",
            pitch=n.pitch,
            voice=n.voice,
            sample=n.sample,
            source=n.source,
        )
        for n in notes
    ]
    events.sort(key=lambda e: (e.row, e.pitch if e.pitch is not None else -1, e.voice or ""))
    return events, info


def group(events: Iterable[Event]) -> dict[int, list[Event]]:
    out: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        out[event.row].append(event)
    for row in out:
        out[row].sort(key=lambda e: (e.kind, e.pitch if e.pitch is not None else -1, e.instrument or -1, e.voice or ""))
    return dict(out)


def compare_note_events(it: list[Event], ts: list[Event]) -> dict[str, Any]:
    """Compare note-ons as row-local pitch multisets.

    This remains deliberately separate from richer event comparison because
    instrument/sample/volume semantics are not yet guaranteed to be directly
    equivalent in every generated file.
    """
    ia = defaultdict(list)
    ta = defaultdict(list)
    for e in it:
        if e.kind == "note_on":
            ia[e.row].append(e.pitch)
    for e in ts:
        if e.kind == "note_on":
            ta[e.row].append(e.pitch)

    missing = []
    extra = []
    for row in sorted(set(ia) | set(ta)):
        a = sorted(ia[row])
        b = sorted(ta[row])
        n = min(len(a), len(b))
        missing.extend((row, x) for x in a[n:])
        extra.extend((row, x) for x in b[n:])
    return {"missing": missing, "spurious": extra, "it_count": sum(map(len, ia.values())), "ts_count": sum(map(len, ta.values()))}


def compare_event_presence(it: list[Event], ts: list[Event]) -> list[dict[str, Any]]:
    """Find the first meaningful divergence in the normalized streams."""
    ia = group(e for e in it if e.kind == "note_on")
    ta = group(e for e in ts if e.kind == "note_on")
    for row in sorted(set(ia) | set(ta)):
        a = sorted((e.pitch for e in ia.get(row, [])))
        b = sorted((e.pitch for e in ta.get(row, [])))
        if a != b:
            return [{"row": row, "it": a, "trackscript": b}]
    return []


def microscope(it: list[Event], ts: list[Event], center: int | None, radius: int) -> str:
    if center is None:
        div = compare_event_presence(it, ts)
        center = div[0]["row"] if div else None
    if center is None:
        return "No note-level divergence found."

    ia = group(it)
    ta = group(ts)
    lo = max(0, center - radius)
    hi = center + radius
    lines = [
        "=== EVENT MICROSCOPE ===",
        f"  center row: {center}",
        f"  window:     {lo}..{hi}",
        "",
    ]
    for row in range(lo, hi + 1):
        a = ia.get(row, [])
        b = ta.get(row, [])
        if not a and not b:
            continue
        lines.append(f"ROW {row}")
        lines.append("  IT:")
        if a:
            for e in a:
                lines.append("    " + format_event(e))
        else:
            lines.append("    —")
        lines.append("  TS:")
        if b:
            for e in b:
                lines.append("    " + format_event(e))
        else:
            lines.append("    —")
        if [e.key() for e in a] == [e.key() for e in b]:
            lines.append("  ✓ semantic event set matches")
        else:
            lines.append("  ✗ DIFFERENCE")
        lines.append("")
    return "\n".join(lines)


def format_event(e: Event) -> str:
    parts = [e.kind]
    if e.pitch is not None:
        parts.append(note_name(e.pitch))
    if e.instrument is not None:
        parts.append(f"inst={e.instrument}")
    if e.sample is not None:
        parts.append(f"sample={e.sample}")
    if e.volume is not None:
        parts.append(f"vol={e.volume:g}")
    if e.channel is not None:
        parts.append(f"ch={e.channel}")
    if e.voice is not None:
        parts.append(e.voice)
    return " ".join(parts)


def make_trace(it: list[Event], ts: list[Event], ts_info: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": "trackscript-semantic-trace-1",
        "comparison": {
            "it_event_count": len(it),
            "trackscript_event_count": len(ts),
        },
        "it": [asdict(e) for e in it],
        "trackscript": [asdict(e) for e in ts],
        "trackscript_info": ts_info,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("semantics", help=".semantics.json produced by it2trackscript (or the original .it; the semantics file will be inferred from the Trackscript path)")
    parser.add_argument("trackscript")
    parser.add_argument("--around", type=int, default=8, metavar="ROWS", help="rows around first divergence (default: 8)")
    parser.add_argument("--row", type=int, default=None, help="inspect a specific row instead of the first divergence")
    parser.add_argument("--json", metavar="PATH", help="write machine-readable semantic trace; use '-' for stdout")
    args = parser.parse_args()

    try:
        semantics_path = Path(args.semantics)
        if semantics_path.suffix.lower() == ".it":
            # Backward-compatible CLI: semantic_diff old-style invocation was
            #   semantic_diff song.it song.trackscript
            # The converter now emits the semantic trace beside the generated
            # Trackscript, so infer it from the Trackscript filename.
            ts_path = Path(args.trackscript)
            inferred = ts_path.with_suffix(".semantics.json")
            if not inferred.is_file():
                raise FileNotFoundError(
                    f"semantic file not found: {inferred} (run it2trackscript.py first)"
                )
            semantics_path = inferred
        it, semantics = semantics_events(semantics_path)
        ts, ts_info = trackscript_events(args.trackscript)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    notes = compare_note_events(it, ts)
    divergence = compare_event_presence(it, ts)

    print("=== Semantic Comparison ===")
    print(f"  IT decoded semantic events: {len(it)}")
    print(f"  Trackscript events:       {len(ts)}")
    print(f"  IT note-ons:              {notes['it_count']}")
    print(f"  Trackscript note-ons:     {notes['ts_count']}")
    print(f"  missing note-ons:         {len(notes['missing'])}")
    print(f"  spurious note-ons:        {len(notes['spurious'])}")
    print(f"  TS simulated voices:      {ts_info['voices']}")
    print(f"  malformed TS pitch rows:  {ts_info['malformed_pitch_events']}")

    if notes["missing"]:
        print("\n=== Missing Note-ons ===")
        for row, pitch in notes["missing"][:30]:
            print(f"  row {row:5d}  {note_name(pitch)}")
    if notes["spurious"]:
        print("\n=== Spurious Note-ons ===")
        for row, pitch in notes["spurious"][:30]:
            print(f"  row {row:5d}  {note_name(pitch)}")

    if divergence or args.row is not None:
        print()
        print(microscope(it, ts, args.row, args.around))
    elif not notes["missing"] and not notes["spurious"]:
        print("\n✓ Note timeline is semantically identical at row/pitch level.")

    if args.json:
        trace = make_trace(it, ts, ts_info)
        trace["it_semantics_source"] = str(semantics_path)
        if args.json == "-":
            json.dump(trace, sys.stdout, indent=2)
            print()
        else:
            Path(args.json).write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
            print(f"\nWrote semantic trace: {args.json}")

    return 0 if not notes["missing"] and not notes["spurious"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
