"""Tempo-lane parsing and musical-row to audio-time conversion."""
from __future__ import annotations

import math
import re
from bisect import bisect_right


def parse_tempo(text: str, subdivisions: int = 4) -> list[tuple[float, float, float]]:
    """Return tempo points as ``(row, bpm, ramp_beats)`` tuples."""
    subdivisions = max(1, subdivisions)
    events = []
    match = re.search(r'\bTempo\s*\{(.*?)\}', text, re.DOTALL)
    if not match:
        return events

    for event in re.finditer(r'[&]?\s*<([^<>]+)>', match.group(1)):
        row = bpm = None
        ramp_beats = 0.0
        for field in (part.strip() for part in event.group(1).split(',')):
            if field.startswith('§'):
                try:
                    beat, subdivision = field[1:].split('/', 1)
                    row = (
                        (float(beat) - 1) * subdivisions
                        + float(subdivision) - 1
                    )
                except (ValueError, TypeError):
                    row = None
            elif field.startswith('#'):
                try:
                    bpm = float(field[1:])
                except ValueError:
                    bpm = None
            elif field.startswith('~'):
                try:
                    ramp_beats = float(field[1:].lstrip('+'))
                except ValueError:
                    ramp_beats = 0.0

        if row is not None and bpm is not None and math.isfinite(row + bpm + ramp_beats):
            events.append((row, bpm, ramp_beats))
    return events


class TempoMap:
    """Integrate a piecewise-linear BPM lane over absolute musical rows."""

    def __init__(
        self,
        initial_bpm: float,
        events: list[tuple[float, float, float]],
        subdivisions: int,
    ):
        self.subdivisions = max(1, subdivisions)
        self.segments: list[tuple[float, float, float, float]] = []
        cursor = 0.0
        bpm = max(1e-6, float(initial_bpm))
        ramp = None

        for event_row, target_bpm, ramp_beats in sorted(events, key=lambda event: event[0]):
            event_row = max(cursor, 0.0, event_row)
            bpm, ramp = self._advance(cursor, event_row, bpm, ramp)
            cursor = event_row
            target_bpm = max(1e-6, target_bpm)
            ramp_rows = max(0.0, ramp_beats) * self.subdivisions

            if ramp_rows > 0:
                ramp = (cursor, cursor + ramp_rows, bpm, target_bpm)
            else:
                bpm = target_bpm
                ramp = None

        if ramp is not None:
            ramp_end = ramp[1]
            bpm, ramp = self._advance(cursor, ramp_end, bpm, ramp)
            cursor = ramp_end

        self.segments.append((cursor, math.inf, bpm, bpm))

    def _advance(self, start, end, bpm, ramp):
        if end <= start:
            return bpm, ramp

        if ramp is None:
            self.segments.append((start, end, bpm, bpm))
            return bpm, None

        ramp_start, ramp_end, ramp_from, ramp_to = ramp
        ramp_stop = min(end, ramp_end)

        if start < ramp_stop:
            duration = ramp_end - ramp_start
            start_bpm = ramp_from + (ramp_to - ramp_from) * (start - ramp_start) / duration
            stop_bpm = ramp_from + (ramp_to - ramp_from) * (ramp_stop - ramp_start) / duration
            self.segments.append((start, ramp_stop, start_bpm, stop_bpm))
            bpm = stop_bpm
            start = ramp_stop

        if end > start:
            bpm = ramp_to
            self.segments.append((start, end, bpm, bpm))

        if end >= ramp_end:
            return ramp_to, None
        return bpm, ramp

    def time_at_row(self, row: float) -> float:
        target = max(0.0, float(row))
        seconds = 0.0

        for start, end, bpm_start, bpm_end in self.segments:
            if target <= start:
                break
            stop = min(target, end)
            delta_rows = stop - start
            if delta_rows <= 0:
                continue

            if bpm_start == bpm_end or math.isinf(end):
                seconds += 60.0 * delta_rows / (self.subdivisions * bpm_start)
            else:
                full_rows = end - start
                stop_bpm = bpm_start + (bpm_end - bpm_start) * delta_rows / full_rows
                slope = (stop_bpm - bpm_start) / delta_rows
                if abs(slope) < 1e-12:
                    seconds += 60.0 * delta_rows / (self.subdivisions * bpm_start)
                else:
                    seconds += (
                        60.0
                        / (self.subdivisions * slope)
                        * math.log(stop_bpm / bpm_start)
                    )

            if target <= end:
                break

        return seconds


class AutomationLane:
    """Linear value automation with transitions positioned in musical rows."""

    def __init__(self, initial_value, start_row, events, tempo_map):
        self.start_time = tempo_map.time_at_row(start_row)
        self.points = [(self.start_time, float(initial_value))]

        for row, duration_rows, target in sorted(events, key=lambda event: event[0]):
            start_time = max(self.start_time, tempo_map.time_at_row(row))
            end_time = tempo_map.time_at_row(row + max(0.0, duration_rows))
            start_value = self.value_at(start_time)

            kept = [point for point in self.points if point[0] < start_time]
            self.points = kept + [(start_time, start_value)]
            if end_time <= start_time:
                self.points.append((start_time, float(target)))
            else:
                self.points.append((end_time, float(target)))

        self.times = [point[0] for point in self.points]
        self.values = [point[1] for point in self.points]

    def value_at(self, time):
        index = bisect_right([point[0] for point in self.points], time) - 1
        if index < 0:
            return self.points[0][1]
        if index >= len(self.points) - 1:
            return self.points[index][1]

        start_time, start_value = self.points[index]
        end_time, end_value = self.points[index + 1]
        if end_time <= start_time:
            return end_value
        fraction = (time - start_time) / (end_time - start_time)
        return start_value + (end_value - start_value) * fraction