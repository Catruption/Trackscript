#!/usr/bin/env python3
"""
it2trackscript - convert an Impulse Tracker (.it) module into Trackscript.

    python3 it2trackscript.py song.it
    python3 it2trackscript.py song.it -o out --pitch names

Output directory:
    <name>.trackscript
    samples/NN_name.wav
    conversion_report.txt

The converter intentionally separates the tracker representation from the
Trackscript representation:

    IT playback
        -> simulated voices
        -> one-measure patterns
        -> BlockList placements / BlockArray

Patterns are grouped by sample within each measure, regardless of the source
tracker channel. Identical patterns and repeated adjacent event groups are
reused across the arrangement.

Each BlockList line is one measure and calls its measure patterns directly.
The generated Trackscript describes the music rather than reproducing the
internal layout of the .it file.
"""

import argparse
import array
import json
import math
import os
import re
import shutil
import struct
import sys
from collections import Counter, defaultdict

from it_parser import (
    parse_it, unpack_pattern, decode_sample, it_decompress,
    BitReader, cstr,
    Instrument, Sample, Module,
    note_termination,
    IT_16BIT, IT_STEREO, IT_COMPRESSED, IT_SIGNED,
    IT_BIG_ENDIAN, IT_DELTA, IT_PTM8TO16, IT_LOOP,
    IT_SUSTAIN, IT_PINGPONG, IT_PINGPONG_SUSTAIN, IT_ADPCM,
    NOTE_FADE, NOTE_NOTECUT, NOTE_KEYOFF,
    ITNote, ITOrder,
)
from pathlib import Path
from trackscript_timing import AutomationLane, TempoMap

ROWS_PER_BEAT = 4
C5_HZ = 523.2511306
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F',
              'F#', 'G', 'G#', 'A', 'A#', 'B']

G_TABLE = [0, 1, 4, 8, 16, 32, 64, 96, 128, 255]
VIB_PEAK_PER_DEPTH = 1.0 / 16.0

ROUND = {
    'pitch': 2,
    'vol': 1,
    'pan': 2,
}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def fmt(x, nd=2):
    s = f"{x:.{nd}f}".rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s


def hz(semi):
    return C5_HZ * 2.0 ** ((semi - 60.0) / 12.0)


def slide_pitch_period(pitch, amount, c5_speed, linear):
    """Apply signed IT period movement; positive amount raises pitch."""
    if linear:
        return max(0.0, pitch + amount / 64.0)
    c5_speed = max(1.0, float(c5_speed or 8363))
    period = (8363.0 * 1712.0) / (
        c5_speed * 2.0 ** ((pitch - 60.0) / 12.0)
    )
    next_period = max(1.0, period - amount)
    return max(0.0, pitch + 12.0 * math.log2(period / next_period))


def note_name(n):
    return f"{NOTE_NAMES[n % 12]}{n // 12}"


def safe_name(s):
    s = re.sub(r'[^A-Za-z0-9_.-]+', '_', s).strip('_')
    return s or 'sample'


def identifier(s):
    """
    Make a Trackscript identifier.

    Trackscript names are deliberately kept readable instead of using
    generated numeric IDs wherever possible.
    """
    s = safe_name(s)

    if not s:
        s = 'Part'

    if s[0].isdigit():
        s = '_' + s

    return s


# ----------------------------------------------------------------------------
# IT parsing
# ----------------------------------------------------------------------------

def write_wav(path, s):
    rate = max(
        100,
        min(192000, s.c5 or 8363)
    )

    if s.is16:
        pcm = array.array('h', s.pcm)

        if sys.byteorder == 'big':
            pcm.byteswap()

        bits = 16

    else:
        pcm = array.array(
            'B',
            [x + 128 for x in s.pcm]
        )
        bits = 8

    data = pcm.tobytes()

    block = bits // 8

    fmt_chunk = (
        b'fmt '
        + struct.pack(
            '<IHHIIHH',
            16,
            1,
            1,
            rate,
            rate * block,
            block,
            bits,
        )
    )

    data_chunk = (
        b'data'
        + struct.pack('<I', len(data))
        + data
        + (b'\0' if len(data) & 1 else b'')
    )

    body = b'WAVE' + fmt_chunk + data_chunk

    if s.loop and s.loop_end > s.loop_beg:
        loop = struct.pack(
            '<IIIIII',
            0,
            1 if s.pingpong else 0,
            s.loop_beg,
            s.loop_end - 1,
            0,
            0,
        )

        smpl = (
            struct.pack(
                '<IIIIIIIII',
                0,
                0,
                int(1e9 / rate),
                60,
                0,
                0,
                0,
                1,
                0,
            )
            + loop
        )

        body += (
            b'smpl'
            + struct.pack('<I', len(smpl))
            + smpl
        )

    with open(path, 'wb') as f:
        f.write(
            b'RIFF'
            + struct.pack('<I', len(body))
            + body
        )


# ----------------------------------------------------------------------------
# song simulation
# ----------------------------------------------------------------------------

class Voice:
    def __init__(self, ch, start, sample_idx, note, bpm, instrument, speed):
        self.ch = ch
        self.start = start
        self.end = None

        self.sample = sample_idx
        self.note = note
        self.bpm = bpm
        self.instrument = instrument
        self.speed = speed
        self.envelope = instrument.envelopes.get('volume')
        self.key_off_row = None
        self.offset = 0

        self.init = {}
        self.last = {}

        # (time, property, value, duration)
        self.points = []

        self.natural_rows = None


class ChannelState:
    def __init__(self, pan, vol):
        self.inst = 0

        self.vol = 64
        self.chanvol = min(vol, 64)

        self.pan = (
            float(pan & 127)
            if (pan & 127) <= 64
            else 32.0
        )

        self.pitch = 60.0
        self.target = None

        self.voice = None

        self.gscale = 1.0

        self.mem_d = 0
        self.mem_ef = 0
        self.mem_g = 0
        self.mem_o = 0

        self.vib_spd = 0
        self.vib_dep = 0
        self.vib_pos = 0.0
        self.vib_on = False


def slide_decode(p):
    x = p >> 4
    y = p & 15

    if y == 15 and x:
        return 'fup', x

    if x == 15 and y:
        return 'fdown', y

    if y == 0 and x:
        return 'up', x

    if x == 0 and y:
        return 'down', y

    return None, 0


class Converter:
    def __init__(self, module, pitch_mode='hz'):
        self.m = module
        self.pitch_mode = pitch_mode

        self.warn = Counter()
        self.warn_detail = {}

        self.voices = []

        # (absolute row, order index, pattern, row, bpm)
        self.rowlog = []
        # A tracker tempo slide runs during the row that contains it. Keep
        # those ramps separately from row-start BPM snapshots so the emitted
        # tempo lane does not turn the slide into a late, instantaneous jump.
        self.tempo_ramps = []
        self.executed_rows = []

        self.comments = []
        self.loop_info = None

        self.ch = [
            ChannelState(
                module.chnpan[i],
                module.chnvol[i],
            )
            for i in range(64)
        ]

        self.gvol = module.gv
        self.speed = module.speed
        self.tempo = module.tempo
        self.mem_w = 0

    def note_warn(self, key, detail=None):
        self.warn[key] += 1

        if detail and key not in self.warn_detail:
            self.warn_detail[key] = detail

    # ------------------------------------------------------------------
    # timing
    # ------------------------------------------------------------------

    def bpm(self):
        return (
            60.0
            * self.tempo
            / (2.5 * self.speed * ROWS_PER_BEAT)
        )

    # ------------------------------------------------------------------
    # note mapping
    # ------------------------------------------------------------------

    def map_note(self, inst, note):
        m = self.m

        if m.use_instruments:
            if inst < 1 or inst > len(m.instruments):
                return None

            ins = m.instruments[inst - 1]
            n, s = ins.keymap[note]

            if s == 0 or s > len(m.samples):
                return None

            return ins, s, n

        if inst < 1 or inst > len(m.samples):
            return None

        fake = Instrument()
        fake.gbv = 128
        fake.dfp = 0

        return fake, inst, note

    def eff_vol(self, st, vol, gvol):
        return (
            100.0
            * (vol / 64.0)
            * (st.chanvol / 64.0)
            * (gvol / 128.0)
            * st.gscale
        )

    # ------------------------------------------------------------------
    # waypoint bookkeeping
    # ------------------------------------------------------------------

    def point(self, v, t, prop, value, dur):
        nd = ROUND[prop]

        last = v.last.get(prop)

        if (
            last is not None
            and round(last, nd) == round(value, nd)
        ):
            return

        v.points.append(
            (t, prop, value, dur)
        )

        v.last[prop] = value

    @staticmethod
    def value_at(voice, prop, t):
        """Return a voice lane's interpolated value at absolute row ``t``."""
        value = voice.init.get(prop, 0.0)
        segment_start = voice.start
        segment_end = voice.start
        segment_value = value
        segment_target = value

        for event_t, event_prop, target, duration in voice.points:
            if event_prop != prop:
                continue
            if event_t > t:
                break
            if segment_end > segment_start and event_t < segment_end:
                fraction = max(0.0, (event_t - segment_start) / (segment_end - segment_start))
                value = segment_value + (segment_target - segment_value) * fraction
            else:
                value = segment_target
            segment_start = event_t
            segment_value = value
            segment_end = event_t + max(0.0, duration)
            segment_target = target
            if duration <= 0:
                value = target
                segment_value = target
                segment_end = event_t

        if segment_end > segment_start and t < segment_end:
            fraction = max(0.0, (t - segment_start) / (segment_end - segment_start))
            return segment_value + (segment_target - segment_value) * fraction
        return segment_target

    def end_voice(self, st, t):
        if st.voice is not None:
            st.voice.end = t
            st.voice = None

    # ------------------------------------------------------------------
    # main walk
    # ------------------------------------------------------------------

    def run(self):
        m = self.m

        order_i = 0
        row = 0
        abs_row = 0

        visited = set()

        while True:
            while (
                order_i < len(m.orders)
                and m.orders[order_i] == ITOrder.SKIP
            ):
                order_i += 1

            if (
                order_i >= len(m.orders)
                or m.orders[order_i] == ITOrder.END
            ):
                break

            pat = m.orders[order_i]

            if pat >= len(m.patterns):
                self.note_warn(
                    'order points at missing pattern'
                )
                order_i += 1
                continue

            grid = m.patterns[pat]

            self.executed_rows.append({
                "absolute_row": abs_row,
                "order": order_i,
                "pattern": pat,
                "pattern_row": row,
            })

            if row >= len(grid):
                order_i += 1
                row = 0
                continue

            if (order_i, row) in visited:
                self.loop_info = (
                    order_i,
                    row,
                    abs_row,
                )
                break

            visited.add((order_i, row))

            jump_o, brk = self.process_row(
                abs_row,
                order_i,
                pat,
                row,
                grid[row],
            )

            abs_row += 1

            if jump_o is not None:
                order_i = jump_o
                row = brk or 0

            elif brk is not None:
                order_i += 1
                row = brk

            else:
                row += 1

        self.total_rows = abs_row

        for st in self.ch:
            self.end_voice(st, abs_row)

        self.finish_voices()

    def process_row(
        self,
        t,
        order_i,
        pat,
        row,
        cells,
    ):
        jump_o = None
        brk = None

        # --------------------------------------------------------------
        # global effects
        # --------------------------------------------------------------

        gslide = None
        tslide = 0
        previous_gvol = self.gvol

        for ch, cell in enumerate(cells):
            if not cell or not cell[3]:
                continue

            c = chr(64 + cell[3])
            p = cell[4]

            if c == 'A':
                if p:
                    self.speed = p

            elif c == 'T':
                if p >= 0x20:
                    self.tempo = p

                elif p >> 4 == 0:
                    tslide = -(p & 15)

                elif p >> 4 == 1:
                    tslide = p & 15

            elif c == 'V':
                self.gvol = min(p, 128)

            elif c == 'W':
                if p:
                    self.mem_w = p

                gslide = slide_decode(self.mem_w)

            elif c == 'B':
                jump_o = p

            elif c == 'C':
                brk = (p >> 4) * 10 + (p & 15)

            elif c == 'S' and (p >> 4) == 0xE:
                self.note_warn(
                    'SEx pattern delay ignored'
                )

        speed = self.speed
        bpm = self.bpm()

        self.rowlog.append(
            (
                t,
                order_i,
                pat,
                row,
                round(bpm, 2),
            )
        )

        gstart = self.gvol
        gend = gstart

        if gslide and gslide[0]:
            k, a = gslide

            if k == 'fup':
                gstart = gend = min(
                    128,
                    self.gvol + a * 2,
                )

            elif k == 'fdown':
                gstart = gend = max(
                    0,
                    self.gvol - a * 2,
                )

            elif k == 'up':
                gend = min(
                    128,
                    self.gvol + a * 2 * (speed - 1),
                )

            elif k == 'down':
                gend = max(
                    0,
                    self.gvol - a * 2 * (speed - 1),
                )

        self.gvol_row = gstart

        # Global volume commands and slides change the output level of every
        # voice that is already sounding, including detached NNA voices.
        # Note starts below use gstart directly; existing voices need an
        # explicit automation point to receive the same change.
        if previous_gvol > 0 and gstart != previous_gvol:
            start_ratio = gstart / previous_gvol
            end_ratio = gend / previous_gvol
            for voice in self.voices:
                if voice.end is not None and voice.end <= t:
                    continue
                current = self.value_at(voice, 'vol', t)
                at_start = current * start_ratio
                self.point(voice, t, 'vol', at_start, 0.0)
                if gend != gstart and gstart > 0:
                    self.point(
                        voice,
                        t,
                        'vol',
                        current * end_ratio,
                        1.0,
                    )

        for ch in range(64):
            cell = cells[ch]
            st = self.ch[ch]

            if cell is None and st.voice is None:
                continue

            if (
                self.m.chnpan[ch] & 128
                and cell is not None
            ):
                self.note_warn(
                    'events on disabled channel ignored'
                )
                continue

            self.process_channel(
                t,
                ch,
                st,
                cell,
                speed,
                bpm,
                gstart,
                gend,
            )

        self.gvol = gend

        if tslide:
            self.tempo = max(
                32,
                min(
                    255,
                    self.tempo
                    + tslide * (speed - 1),
                ),
            )
            self.tempo_ramps.append(
                (t, self.bpm(), 1.0)
            )
            self.note_warn(
                'IT tempo slide approximated as linear one-row ramp'
            )

        return jump_o, brk

    # ------------------------------------------------------------------
    # channel processing
    # ------------------------------------------------------------------

    def process_channel(
        self,
        t,
        ch,
        st,
        cell,
        speed,
        bpm,
        gstart,
        gend,
    ):
        note, inst, volc, cmd, par = (
            cell
            if cell
            else (None, None, None, None, 0)
        )
        previous_channel_vol = st.vol

        c = chr(64 + cmd) if cmd else None

        if inst:
            st.inst = inst

        # --------------------------------------------------------------
        # volume column
        # --------------------------------------------------------------

        vcol = None

        if volc is not None:
            if volc <= 64:
                vcol = ('set', volc)

            elif volc <= 74:
                vcol = ('fup', volc - 65)

            elif volc <= 84:
                vcol = ('fdown', volc - 75)

            elif volc <= 94:
                vcol = ('up', volc - 85)

            elif volc <= 104:
                vcol = ('down', volc - 95)

            elif volc <= 114:
                vcol = ('pdown', volc - 105)

            elif volc <= 124:
                vcol = ('pup', volc - 115)

            elif 128 <= volc <= 192:
                vcol = ('pan', volc - 128)

            elif 193 <= volc <= 202:
                vcol = (
                    'G',
                    G_TABLE[volc - 193],
                )

            elif 203 <= volc <= 212:
                vcol = (
                    'H',
                    volc - 203,
                )

        toneporta = (
            (c in ('G', 'L'))
            or (vcol and vcol[0] == 'G')
        )

        is_note = (
            note is not None
            and note < 120
        )

        delay = 0.0

        if (
            c == 'S'
            and (par >> 4) == 0xD
            and (par & 15)
            and is_note
        ):
            if (par & 15) >= speed:
                self.note_warn(
                    'SDx delay >= speed: note never plays, dropped'
                )
                is_note = False

            else:
                delay = (par & 15) / float(speed)

        # --------------------------------------------------------------
        # note off / cut
        # --------------------------------------------------------------

        termination = note_termination(note)
        if termination == 'cut':
            self.end_voice(st, t)
            st.target = None
        elif termination == 'off':
            voice = st.voice
            envelope = voice.envelope if voice is not None else None
            if (
                envelope is not None
                and envelope.enabled
                and envelope.points
                and not envelope.loop_enabled
                and not envelope.sustain_enabled
            ):
                voice.key_off_row = t
            elif voice is not None and voice.instrument.fadeout > 0 and not (
                envelope is not None and envelope.enabled
            ):
                # IT key-off starts instrument fadeout when there is no
                # enabled volume envelope. OpenMPT reduces nFadeOutVol by
                # 2 * fadeout per tick from 65536, so convert that tick count
                # to rows using the voice's speed.
                fade_rows = 32768.0 / (
                    voice.instrument.fadeout * max(1, voice.speed)
                )
                fade_start = voice.last.get(
                    'vol',
                    voice.init.get('vol', 100.0),
                )
                voice.points.append((t, 'vol', fade_start, 0.0))
                voice.points.append((t, 'vol', 0.0, fade_rows))
                voice.end = t + fade_rows
                st.voice = None
            elif (
                voice is not None
                and envelope is not None
                and envelope.enabled
                and (envelope.loop_enabled or envelope.sustain_enabled)
            ):
                self.end_voice(st, t)
                self.note_warn(
                    'looped/sustained volume envelope key-off approximated as cut'
                )
            else:
                # A non-looping envelope keeps running after key-off. With no
                # envelope and no fadeout, IT also keeps the sample playing.
                if voice is not None:
                    voice.key_off_row = t
            st.target = None

        trigger = None

        if is_note:
            if toneporta and st.voice is not None:
                mapped = self.map_note(
                    st.inst,
                    note,
                )

                st.target = (
                    float(mapped[2])
                    if mapped
                    else float(note)
                )

            else:
                if not st.inst:
                    self.note_warn(
                        'note with no instrument ignored'
                    )

                else:
                    mapped = self.map_note(
                        st.inst,
                        note,
                    )

                    if (
                        mapped is None
                        or not self.m.samples[
                            mapped[1] - 1
                        ].has_data
                    ):
                        self.note_warn(
                            'note maps to empty sample (silent)'
                        )
                        self.end_voice(st, t)

                    else:
                        trigger = mapped

        # --------------------------------------------------------------
        # new-note defaults
        # --------------------------------------------------------------

        if trigger:
            ins, sidx, mnote = trigger
            smp = self.m.samples[sidx - 1]

            old_voice = st.voice
            new_start = t + delay
            if old_voice is not None:
                nna = old_voice.instrument.nna
                if nna == 1:  # IT NNA: continue
                    # The old note moves to an independent voice. It remains
                    # in the score while the tracker channel starts the new
                    # note.
                    st.voice = None
                else:
                    if nna in (2, 3):
                        self.note_warn(
                            'NNA note-off/fade approximated as cut'
                        )
                    self.end_voice(st, new_start)

            if ins.dct or ins.dca:
                self.note_warn(
                    'instrument duplicate-check action not converted'
                )

            st.vol = smp.vol

            if (
                self.m.use_instruments
                and (ins.dfp & 128)
            ):
                st.pan = float(
                    min(
                        ins.dfp & 127,
                        64,
                    )
                )

            st.pitch = float(mnote)
            st.target = None

            st.vib_pos = 0.0
            st.vib_on = False

            st.gscale = (
                smp.gvl / 64.0
            ) * (
                ins.gbv / 128.0
                if self.m.use_instruments
                else 1.0
            )

        # --------------------------------------------------------------
        # instant changes
        # --------------------------------------------------------------

        if vcol:
            k, a = vcol

            if k == 'set':
                st.vol = a

            elif k == 'fup':
                st.vol = min(
                    64,
                    st.vol + a,
                )

            elif k == 'fdown':
                st.vol = max(
                    0,
                    st.vol - a,
                )

            elif k == 'pan':
                st.pan = float(a)

        cut_tick = None

        if c == 'M':
            old_chanvol = st.chanvol
            st.chanvol = min(par, 64)
            # IT channel-volume commands affect every playing voice on the
            # channel, including NNA voices that have been detached from
            # st.voice by later note-ons. Scale their current effective level
            # by the channel-volume ratio and emit the change on each voice.
            if old_chanvol and old_chanvol != st.chanvol:
                ratio = st.chanvol / old_chanvol
                for voice in self.voices:
                    if voice.ch != ch or (voice.end is not None and voice.end <= t):
                        continue
                    current = self.value_at(voice, 'vol', t)
                    self.point(voice, t, 'vol', current * ratio, 0.0)

        elif c == 'X':
            st.pan = min(
                64.0,
                par * 64.0 / 255.0,
            )

        elif c == 'S':
            sub, x = par >> 4, par & 15

            if sub == 0x8:
                st.pan = x * 64.0 / 15.0

            elif sub == 0xC:
                cut_tick = x

            elif sub == 0xD:
                pass

            elif sub == 0x9:
                self.note_warn(
                    'S9x surround/sound control ignored'
                )

            elif sub in (0x3, 0x4, 0x5):
                self.note_warn(
                    'S3x/S4x/S5x waveform select ignored'
                )

            elif sub in (
                0x7,
                0xB,
                0xA,
                0xF,
                0x1,
                0x2,
                0x6,
            ):
                self.note_warn(
                    f'S{sub:X}x ignored'
                )

        elif c in ('D', 'K', 'L'):
            if par:
                st.mem_d = par

        elif c == 'O':
            if par:
                st.mem_o = par

        elif c in ('E', 'F'):
            if par:
                st.mem_ef = par

        elif c == 'G':
            if par:
                st.mem_g = par

        elif c == 'Z':
            self.z_comment(t, par)

        elif c in ('H', 'U'):
            pass

        elif c in (
            'B',
            'C',
            'A',
            'T',
            'V',
            'W',
        ):
            pass

        elif c is not None:
            self.note_warn(
                f'effect {c} not converted'
            )

        # --------------------------------------------------------------
        # volume slide
        # --------------------------------------------------------------

        dkind = None

        if c in ('D', 'K', 'L'):
            dkind = slide_decode(
                st.mem_d
            )

            if dkind[0] == 'fup':
                st.vol = min(
                    64,
                    st.vol + dkind[1],
                )

            elif dkind[0] == 'fdown':
                st.vol = max(
                    0,
                    st.vol - dkind[1],
                )

        # --------------------------------------------------------------
        # fine pitch slide
        # --------------------------------------------------------------

        efs = None

        if c in ('E', 'F'):
            p = st.mem_ef

            sgn = (
                -1
                if c == 'E'
                else 1
            )

            if p >= 0xF0:
                efs = (
                    'fine',
                    sgn * (p & 15) / 16.0,
                )

            elif p >= 0xE0:
                efs = (
                    'fine',
                    sgn * (p & 15) / 64.0,
                )

            else:
                efs = (
                    'ramp',
                    sgn * p / 16.0,
                )

        elif vcol and vcol[0] in (
            'pdown',
            'pup',
        ):
            sgn = (
                -1
                if vcol[0] == 'pdown'
                else 1
            )

            efs = (
                'ramp',
                sgn * (vcol[1] * 4) / 16.0,
            )

        if efs and efs[0] == 'fine':
            sample = (
                self.m.samples[st.voice.sample - 1]
                if st.voice is not None
                else None
            )
            st.pitch = slide_pitch_period(
                st.pitch,
                efs[1] * 64.0,
                sample.c5 if sample is not None else 8363,
                self.m.linear,
            )

        # --------------------------------------------------------------
        # create voice
        # --------------------------------------------------------------

        v = st.voice

        if trigger:
            ins, sidx, mnote = trigger
            smp = self.m.samples[sidx - 1]

            v = Voice(
                ch,
                t + delay,
                sidx,
                mnote,
                bpm,
                ins,
                speed,
            )

            if c == 'O':
                v.offset = st.mem_o * 256

                if v.offset >= smp.length:
                    v.offset = 0

                    self.note_warn(
                        'sample offset beyond sample end ignored'
                    )

            st.voice = v
            self.voices.append(v)

            v.init = {
                'pitch': hz(st.pitch),
                'vol': self.eff_vol(
                    st,
                    st.vol,
                    gstart,
                ),
                'pan': (
                    st.pan - 32.0
                ) / 32.0,
            }

            v.last = dict(v.init)

            if not smp.loop:
                rate = max(
                    1.0,
                    (smp.c5 or 8363)
                    * 2.0 ** (
                        (mnote - 60.0) / 12.0
                    ),
                )

                v.natural_rows = (
                    smp.length / rate
                ) * (
                    bpm
                    * ROWS_PER_BEAT
                    / 60.0
                )

        elif v is not None:
            self.point(
                v,
                t,
                'vol',
                self.eff_vol(
                    st,
                    st.vol,
                    gstart,
                ),
                0,
            )

            self.point(
                v,
                t,
                'pan',
                (st.pan - 32.0) / 32.0,
                0,
            )

            if efs and efs[0] == 'fine':
                self.point(
                    v,
                    t,
                    'pitch',
                    hz(st.pitch),
                    0,
                )

        if v is None:
            return

        # --------------------------------------------------------------
        # continuous effects
        # --------------------------------------------------------------

        ramp_dur = (
            speed - 1
        ) / float(speed)

        ticks = speed - 1

        # volume
        vol_end = st.vol
        slide = None

        if dkind and dkind[0] in (
            'up',
            'down',
        ):
            slide = dkind

        elif vcol and vcol[0] in (
            'up',
            'down',
        ):
            slide = vcol

        if slide:
            d = slide[1] * ticks

            if slide[0] == 'up':
                vol_end = min(
                    64,
                    st.vol + d,
                )

            else:
                vol_end = max(
                    0,
                    st.vol - d,
                )

        # IT channel-volume changes apply to every voice still sounding on
        # the channel, including NNA voices detached from ``st.voice``.
        # The attached voice is handled by eff0/eff1 below; update the rest
        # here so volume-column sets and slides do not leave old voices loud.
        if previous_channel_vol > 0 and (
            st.vol != previous_channel_vol or vol_end != st.vol
        ):
            start_ratio = st.vol / previous_channel_vol
            end_ratio = vol_end / previous_channel_vol
            for old_voice in self.voices:
                if (
                    old_voice.ch != ch
                    or old_voice is v
                    or old_voice.start >= t
                    or (old_voice.end is not None and old_voice.end <= t)
                ):
                    continue
                current = self.value_at(old_voice, 'vol', t)
                self.point(old_voice, t, 'vol', current * start_ratio, 0.0)
                if end_ratio != start_ratio:
                    self.point(
                        old_voice,
                        t,
                        'vol',
                        current * end_ratio,
                        ramp_dur,
                    )

        eff0 = self.eff_vol(
            st,
            st.vol,
            gstart,
        )

        eff1 = self.eff_vol(
            st,
            vol_end,
            gend,
        )

        if abs(eff1 - eff0) > 0.05:
            self.point(
                v,
                t,
                'vol',
                eff1,
                ramp_dur,
            )

        # pitch
        base = st.pitch
        base_end = base

        if efs and efs[0] == 'ramp':
            sample = self.m.samples[v.sample - 1]
            if not self.m.linear:
                self.note_warn(
                    'non-linear IT period slide approximated by pitch ramp'
                )
            base_end = slide_pitch_period(
                base,
                efs[1] * 64.0 * ticks,
                sample.c5,
                self.m.linear,
            )

        gp = None

        if c in ('G', 'L'):
            gp = st.mem_g

        elif vcol and vcol[0] == 'G':
            gp = vcol[1] or st.mem_g

            if vcol[1]:
                st.mem_g = vcol[1]

        if gp is not None and st.target is not None:
            sample = self.m.samples[v.sample - 1]
            if not self.m.linear:
                self.note_warn(
                    'non-linear IT tone portamento approximated by pitch ramp'
                )
            direction = 1.0 if st.target > base else -1.0
            base_end = slide_pitch_period(
                base,
                direction * gp * 4.0 * ticks,
                sample.c5,
                self.m.linear,
            )

            if st.target > base:
                base_end = min(st.target, base_end)

            else:
                base_end = max(st.target, base_end)

            if base_end == st.target:
                st.target = None

        if (
            abs(base_end - base) > 1e-6
            and not (
                c in ('H', 'K', 'U')
                or (
                    vcol
                    and vcol[0] == 'H'
                )
            )
        ):
            self.point(
                v,
                t,
                'pitch',
                hz(base_end),
                ramp_dur,
            )

        # vibrato
        vib = False

        if (
            c in ('H', 'K', 'U')
            or (
                vcol
                and vcol[0] == 'H'
            )
        ):
            if c in ('H', 'U'):
                x = par >> 4
                y = par & 15

                if x:
                    st.vib_spd = x

                if y:
                    st.vib_dep = (
                        y
                        if c == 'H'
                        else y / 4.0
                    )

            elif (
                vcol
                and vcol[0] == 'H'
                and vcol[1]
            ):
                st.vib_dep = vcol[1]

            vib = (
                st.vib_spd > 0
                and st.vib_dep > 0
            )

        if vib:
            peak = (
                st.vib_dep
                * VIB_PEAK_PER_DEPTH
            )

            step = st.vib_spd * 4.0
            pos = st.vib_pos

            for tick in range(speed):
                p1 = pos + step

                k = math.ceil(
                    pos / 64.0 - 1e-9
                )

                while k * 64.0 < p1 - 1e-9:
                    frac = (
                        k * 64.0 - pos
                    ) / step

                    tau = (
                        t
                        + (tick + frac)
                        / speed
                    )

                    nxt = (k + 1) % 4

                    off = (
                        0.0,
                        peak,
                        0.0,
                        -peak,
                    )[nxt]

                    self.point(
                        v,
                        tau,
                        'pitch',
                        hz(
                            base_end + off
                        ),
                        (64.0 / step)
                        / speed,
                    )

                    k += 1

                pos = p1

            st.vib_pos = pos
            st.vib_on = True

        elif st.vib_on:
            self.point(
                v,
                t,
                'pitch',
                hz(base_end),
                0,
            )

            st.vib_on = False

        st.pitch = base_end
        st.vol = vol_end

        # sample cut
        if cut_tick is not None:
            self.end_voice(
                st,
                t + cut_tick / float(speed),
            )

    # ------------------------------------------------------------------
    # comments
    # ------------------------------------------------------------------

    def z_comment(self, t, p):
        if p < 0x80:
            self.comments.append(
                (
                    t,
                    (
                        f"Z{p:02X}: filter cutoff "
                        f"{fmt(p / 127.0 * 100, 0)}%"
                    ),
                )
            )

        else:
            self.comments.append(
                (
                    t,
                    f"Z{p:02X}: MIDI macro "
                    f"{p - 0x80:X}",
                )
            )

        self.note_warn(
            'Z filter/MIDI macros written as comments only'
        )

    # ------------------------------------------------------------------
    # post processing
    # ------------------------------------------------------------------

    def finish_voices(self):
        for v in self.voices:
            end = (
                v.end
                if v.end is not None
                else self.total_rows
            )

            if v.natural_rows is not None:
                end = min(
                    end,
                    v.start
                    + max(
                        v.natural_rows,
                        0.05,
                    ),
                )

            v.end = self.apply_volume_envelope(v, end)

            self.merge_steps(v)

    def apply_volume_envelope(self, voice, end):
        envelope = voice.envelope
        if not envelope or not envelope.enabled or not envelope.points:
            return end
        if envelope.loop_enabled or envelope.sustain_enabled:
            self.note_warn('looped/sustained volume envelope not converted')
            return end

        points = sorted(envelope.points)
        initial_volume = voice.init.get('vol', 100.0)
        first_tick, first_value = points[0]
        voice.init['vol'] = initial_volume * first_value / 64.0
        envelope_end = voice.start + max(0, points[-1][0] - first_tick) / max(1, voice.speed)
        if points[-1][1] <= 0:
            # A key-off does not stop an IT voice with a non-looping volume
            # envelope. Likewise, voices still held when the order list ends
            # continue through their envelope tail. Keep natural sample ends
            # and explicit note cuts intact.
            if voice.key_off_row is not None or (
                voice.end is None and voice.natural_rows is None
            ):
                end = max(end, envelope_end)
            else:
                end = min(end, envelope_end)

        row_clock = TempoMap(120.0, [], ROWS_PER_BEAT)
        volume_events = [
            (time, duration, value)
            for time, prop, value, duration in voice.points
            if prop == 'vol'
        ]
        base_lane = AutomationLane(
            initial_volume,
            voice.start,
            volume_events,
            row_clock,
        )

        def envelope_value(row):
            tick = max(first_tick, (row - voice.start) * max(1, voice.speed) + first_tick)
            for (left_tick, left_value), (right_tick, right_value) in zip(points, points[1:]):
                if tick <= right_tick:
                    if right_tick == left_tick:
                        return float(right_value)
                    fraction = (tick - left_tick) / (right_tick - left_tick)
                    return left_value + (right_value - left_value) * fraction
            return float(points[-1][1])

        def base_value(row):
            return base_lane.value_at(row_clock.time_at_row(row))

        breakpoints = {voice.start, end}
        instant_rows = set()
        for time, duration, _value in volume_events:
            if voice.start < time < end:
                breakpoints.add(time)
            finish = time + max(0.0, duration)
            if voice.start < finish < end:
                breakpoints.add(finish)
            if duration <= 0 and voice.start < time <= end:
                instant_rows.add(time)
        for tick, _value in points:
            row = voice.start + max(0, tick - first_tick) / max(1, voice.speed)
            if voice.start < row < end:
                breakpoints.add(row)

        ordered = sorted(breakpoints)
        sampled = set(ordered)
        sampled.update((left + right) / 2.0 for left, right in zip(ordered, ordered[1:]))
        ordered = sorted(sampled)
        volume_points = [point for point in voice.points if point[1] != 'vol']

        for left, right in zip(ordered, ordered[1:]):
            epsilon = min(1e-6, (right - left) / 4.0)
            sample_row = right - epsilon
            target = base_value(sample_row) * envelope_value(sample_row) / 64.0
            volume_points.append((left, 'vol', target, right - left))
            if any(abs(right - row) < 1e-9 for row in instant_rows):
                target_after = base_value(right) * envelope_value(right) / 64.0
                volume_points.append((right, 'vol', target_after, 0.0))

        voice.points = volume_points
        return end

    @staticmethod
    def merge_steps(v):
        out = []

        for prop in (
            'vol',
            'pan',
            'pitch',
        ):
            pts = [
                p
                for p in v.points
                if p[1] == prop
            ]

            keep = []
            i = 0

            while i < len(pts):
                j = i

                if pts[i][3] == 0:
                    while (
                        j + 1 < len(pts)
                        and pts[j + 1][3] == 0
                        and abs(
                            pts[j + 1][0]
                            - pts[j][0]
                            - 1.0
                        ) < 1e-6
                        and (
                            j + 1 == i + 1
                            or abs(
                                (
                                    pts[j + 1][2]
                                    - pts[j][2]
                                )
                                - (
                                    pts[i + 1][2]
                                    - pts[i][2]
                                )
                            ) < 1e-6
                        )
                    ):
                        j += 1

                if j - i >= 2:
                    keep.append(pts[i])

                    keep.append(
                        (
                            pts[i][0],
                            prop,
                            pts[j][2],
                            pts[j][0]
                            - pts[i][0],
                        )
                    )

                    i = j + 1

                else:
                    keep.append(pts[i])
                    i += 1

            out.extend(keep)

        out.sort(
            key=lambda p: (
                p[0],
                p[1],
            )
        )

        v.points = out


# ----------------------------------------------------------------------------
# Trackscript representation
# ----------------------------------------------------------------------------

def pos_str(r):
    b = int(
        r // ROWS_PER_BEAT
    ) + 1

    s = (
        r
        - ROWS_PER_BEAT * (b - 1)
        + 1
    )

    s = round(s, 2)

    if abs(s - round(s)) < 1e-9:
        s = int(round(s))

        if s > ROWS_PER_BEAT:
            b += 1
            s = 1

        return f"§{b}/{s}"

    return f"§{b}/{fmt(s, 2)}"


def pitch_text(hz_value, mode):
    """Note name when the pitch is exactly a note (within 2 cents), otherwise Hz."""
    if mode == 'names':
        semis = 60.0 + 12.0 * math.log2(hz_value / C5_HZ)
        n = int(round(semis))
        if abs(semis - n) < 0.02:
            return note_name(n)
    return fmt(hz_value, 2)


def pitch_field(v, mode):
    return f"#{pitch_text(v.init['pitch'], mode)}"


def pan_str(p):
    return fmt(
        max(-1.0, min(1.0, p)),
        2,
    )


def tempo_lane(conv):
    """
    Return tempo events as (row, bpm, ramp_rows).

    IT tempo slides change the tick rate inside the row that contains the
    command. Preserve that as a one-row ramp; direct tempo and speed changes
    remain jumps at their row positions.
    """
    b = [bpm for (_t, _oi, _pat, _row, bpm) in conv.rowlog]
    if not b:
        return []

    wps = []
    slide_rows = set()
    for row, target_bpm, ramp_rows in conv.tempo_ramps:
        wps.append((row, target_bpm, ramp_rows))
        slide_rows.add(row + ramp_rows)

    for row in range(1, len(b)):
        if row not in slide_rows and b[row] != b[row - 1]:
            wps.append((row, b[row], 0.0))

    wps.sort(key=lambda event: event[0])
    return wps


def build_sections(conv):
    """
    Global BPM runs.

    Kept separately from the new structural representation because the
    conversion report still uses these as timing diagnostics.
    """

    runs = []

    for (
        t,
        oi,
        pat,
        row,
        bpm,
    ) in conv.rowlog:

        if (
            not runs
            or runs[-1][2] != bpm
        ):
            runs.append(
                [
                    t,
                    t + 1,
                    bpm,
                ]
            )

        else:
            runs[-1][1] = t + 1

    return runs


# ----------------------------------------------------------------------------
# Structural extraction
# ----------------------------------------------------------------------------

def build_order_occurrences(conv):
    """
    Convert the playback log into actual contiguous order occurrences.

    These are only used as candidate boundaries. They are never emitted as
    "pattern 05" / "order 07" in Trackscript.
    """

    if not conv.rowlog:
        return []

    occurrences = []

    start = conv.rowlog[0][0]
    prev = conv.rowlog[0]

    for cur in conv.rowlog[1:]:
        prev_t, prev_oi, prev_pat, prev_row, _ = prev
        cur_t, cur_oi, cur_pat, cur_row, _ = cur

        contiguous = (
            cur_t == prev_t + 1
            and cur_oi == prev_oi
            and cur_pat == prev_pat
            and cur_row == prev_row + 1
        )

        if not contiguous:
            occurrences.append(
                {
                    'start': start,
                    'end': prev_t + 1,
                    'order': prev_oi,
                    'pattern': prev_pat,
                }
            )

            start = cur_t

        prev = cur

    occurrences.append(
        {
            'start': start,
            'end': prev[0] + 1,
            'order': prev[1],
            'pattern': prev[2],
        }
    )

    return occurrences


def voice_crosses(v, boundary):
    """
    True when cutting the timeline at boundary would split a sounding voice.
    """

    if v.start >= boundary:
        return False

    if v.end is None:
        return True

    return v.end > boundary + 1e-9


def build_safe_spans(conv):
    """Return one pattern span per measure.

    A sounding voice may continue past a pattern boundary. Its note length
    carries the sound into later measures; it must not force the converter to
    merge measures into a song-sized pattern.
    """
    if conv.total_rows <= 0:
        return []
    measure_rows = ROWS_PER_BEAT * 4
    return [
        (start, min(start + measure_rows, conv.total_rows))
        for start in range(0, conv.total_rows, measure_rows)
    ]


def voices_for_span(conv, start, end):
    return [
        v
        for v in conv.voices
        if start <= v.start < end
    ]


def group_span_voices(conv, start, end):
    """
    Group voices into measure patterns by source sample.

    Tracker channels are playback machinery, not part of Trackscript's
    musical model. Voices using the same sample in a measure belong to one
    candidate pattern even when the source module played them on different
    channels.
    """

    groups = defaultdict(list)

    for v in voices_for_span(
        conv,
        start,
        end,
    ):
        groups[v.sample].append(v)

    for voices in groups.values():
        voices.sort(
            key=lambda v: (
                v.start,
                v.end if v.end is not None else 10**18,
            )
        )

    return groups


# ----------------------------------------------------------------------------
# event rendering
# ----------------------------------------------------------------------------

def render_voice_event(
    conv,
    v,
    local_start,
    sample_files,
    pitch_mode,
):
    f = sample_files.get(v.sample)

    if not f:
        return None

    dur_rows = max(
        v.end - v.start,
        0.05,
    )

    # ~ is length in BEATS. Convert from rows.
    dur_beats = dur_rows / ROWS_PER_BEAT

    # § is local to the pattern's measure; BlockList supplies the measure.
    fields = [
        pos_str(v.start - local_start),
        pitch_field(v, pitch_mode),
        f"~{fmt(dur_beats, 2)}",
    ]

    if abs(v.init['vol'] - 100.0) > 1e-9:
        fields.append(f"%{fmt(v.init['vol'], 1)}")

    if v.offset:
        fields.append(f"ƒ'{f}'({v.offset})")

    if abs(v.init['pan']) > 1e-9:
        fields.append("¶" + pan_str(v.init['pan']))

    ev = "<" + ",".join(fields) + ">"

    transitions = []

    groups = {}

    for (
        pt,
        prop,
        val,
        d,
    ) in v.points:
        key = (
            round(pt, 3),
            round(d, 3),
        )

        groups.setdefault(
            key,
            []
        ).append(
            (prop, val)
        )

    for (
        pt,
        d,
    ), props_list in sorted(
        groups.items(),
        key=lambda x: x[0][0],
    ):
        props = dict(props_list)

        # Like note duration, an attached waypoint may extend beyond this
        # measure's pattern. Its position remains relative to the pattern's
        # placement, so each instance carries its own later automation.
        if pt < local_start:
            continue

        fields = [pos_str(pt - local_start)]

        if 'pitch' in props:
            fields.append(
                f"#{pitch_text(props['pitch'], pitch_mode)}"
            )

        if 'vol' in props:
            fields.append(
                f"%{fmt(props['vol'], 1)}"
            )

        if 'pan' in props:
            fields.append(
                f"¶{pan_str(props['pan'])}"
            )

        if d > 1e-9:
            # Waypoint duration is in BEATS.
            fields.append(
                f"~{fmt(d / ROWS_PER_BEAT, 2)}"
            )

        transitions.append(
            "&<"
            + ",".join(fields)
            + ">"
        )

    return ev + ''.join(transitions)


def render_part(
    conv,
    voices,
    start,
    end,
    sample_files,
):
    """
    Render a part independently of its original channel.

    Events sharing a start position are placed on one Trackscript line.
    """

    events = defaultdict(list)

    for v in voices:
        ev = render_voice_event(
            conv,
            v,
            start,
            sample_files,
            conv.pitch_mode,
        )

        if ev is None:
            conv.note_warn(
                'voice references missing sample output'
            )
            continue

        rel = round(
            v.start - start,
            3,
        )

        events[rel].append(ev)

    lines = ["@4/4["]
    all_events = [
        event
        for rel in sorted(events)
        for event in events[rel]
    ]
    if all_events:
        lines.append("    " + ", ".join(all_events))
    lines.append("]")

    return "\n".join(lines)


def split_rendered_events(rendered):
    """Split a rendered measure into top-level event/connector groups."""
    body = rendered[rendered.find('[') + 1:rendered.rfind(']')].strip()
    if not body:
        return []
    groups = []
    start = 0
    depth = 0
    for i, char in enumerate(body):
        if char == '<':
            depth += 1
        elif char == '>':
            depth -= 1
        elif char == ',' and depth == 0:
            groups.append(body[start:i].strip())
            start = i + 1
    groups.append(body[start:].strip())
    return [group for group in groups if group]


def render_event_groups(groups):
    lines = ["@4/4["]
    if groups:
        lines.append("    " + ", ".join(groups))
    lines.append("]")
    return "\n".join(lines)


def factor_repeated_event_pairs(parts, measure_placements):
    """Reuse repeated adjacent event pairs as direct pattern components."""
    event_groups = {part: split_rendered_events(part.rendered) for part in parts}
    pair_owners = defaultdict(set)
    for part, groups in event_groups.items():
        for i in range(len(groups) - 1):
            pair_owners[(part.sample_index, groups[i], groups[i + 1])].add(part)

    reusable_pairs = {
        pair for pair, owners in pair_owners.items()
        if len(owners) >= 2
    }
    if not reusable_pairs:
        return parts, measure_placements

    new_parts = []
    replacement = {}
    pair_parts = {}
    for part in parts:
        groups = event_groups[part]
        candidates = [
            (part.sample_index, groups[i], groups[i + 1], i)
            for i in range(len(groups) - 1)
            if (part.sample_index, groups[i], groups[i + 1]) in reusable_pairs
        ]
        if not candidates:
            new_parts.append(part)
            replacement[part] = [part]
            continue

        # Prefer the earliest repeated pair. Each original part is factored
        # once, keeping the transformation deterministic and easy to inspect.
        sample_index, first, second, pair_index = candidates[0]
        pair_key = (sample_index, first, second)
        if pair_key not in pair_parts:
            shared = PartDefinition(
                ("shared-event-pair", pair_key), [], part.start, part.end, sample_index
            )
            shared.rendered = render_event_groups([first, second])
            pair_parts[pair_key] = shared
            new_parts.append(shared)

        remainder = groups[:pair_index] + groups[pair_index + 2:]
        pieces = [pair_parts[pair_key]]
        if remainder:
            rest = PartDefinition(
                ("event-remainder", part.signature), part.voices,
                part.start, part.end, sample_index
            )
            rest.rendered = render_event_groups(remainder)
            new_parts.append(rest)
            pieces.append(rest)
        replacement[part] = pieces

    expanded = []
    for placement_parts, start, end in measure_placements:
        expanded.append((
            [piece for part in placement_parts for piece in replacement[part]],
            start,
            end,
        ))
    return new_parts, expanded


def part_signature(
    conv,
    voices,
    start,
    end,
    sample_files,
):
    """
    Normalize a part for deduplication.

    Channel number is deliberately excluded.

    The signature is based on the generated musical representation, plus the
    relative extent of the part.
    """

    rendered = render_part(
        conv,
        voices,
        start,
        end,
        sample_files,
    )

    last_end = 0.0

    for v in voices:
        last_end = max(
            last_end,
            v.end - start,
        )

    return (
        tuple(sorted({v.sample for v in voices})),
        rendered,
        round(last_end, 3),
    )


def sample_base_name(module, sample_index):
    s = module.samples[sample_index - 1]

    if s.name:
        return identifier(
            s.name
        )

    return f"Sample{sample_index:02d}"


class PartDefinition:
    def __init__(
        self,
        signature,
        voices,
        start,
        end,
        sample_index,
    ):
        self.signature = signature
        self.voices = voices
        self.start = start
        self.end = end
        self.sample_index = sample_index
        self.name = None
        self.rendered = None


# ----------------------------------------------------------------------------
# structure extraction
# ----------------------------------------------------------------------------

def extract_structure(
    conv,
    sample_files,
):
    """
    Build reusable one-measure Parts and their arrangement placements.

    Every measure is decomposed into sample-based musical layers. Tracker
    channels are ignored; identical layers are deduplicated globally, while
    each measure placement retains its calls.
    """

    spans = build_safe_spans(conv)

    part_by_signature = {}
    parts = []

    measure_placements = []

    for start, end in spans:
        groups = group_span_voices(
            conv,
            start,
            end,
        )

        span_parts = []

        for (
            sample_index,
            voices,
        ) in sorted(
            groups.items(),
            key=lambda x: (
                min(v.start for v in x[1]),
                x[0],
            ),
        ):
            sig = part_signature(
                conv,
                voices,
                start,
                end,
                sample_files,
            )

            if sig not in part_by_signature:
                part = PartDefinition(
                    sig,
                    voices,
                    start,
                    end,
                    sample_index,
                )

                part.rendered = render_part(
                    conv,
                    voices,
                    start,
                    end,
                    sample_files,
                )

                part_by_signature[sig] = part
                parts.append(part)

            else:
                part = part_by_signature[sig]

            span_parts.append(part)

        # Keep repeated references when identical musical material appears
        # on multiple layers: each call is a separate sounding instance.
        unique_parts = span_parts

        measure_placements.append((unique_parts, start, end))

    return factor_repeated_event_pairs(parts, measure_placements)


# ----------------------------------------------------------------------------
# naming
# ----------------------------------------------------------------------------

def assign_part_names(parts, module):
    """
    Name parts from their source instrument/sample when possible.

    The names describe the source material without claiming that the sample's
    name necessarily tells us its exact musical role.
    """

    used = Counter()

    for part in parts:
        base = sample_base_name(
            module,
            part.sample_index,
        )

        used[base] += 1
        n = used[base]

        if n == 1:
            part.name = base

        else:
            part.name = f"{base}{n:02d}"

    # A pathological case can still collide after sanitization.
    seen = set()

    for part in parts:
        original = part.name
        candidate = original
        n = 2

        while candidate in seen:
            candidate = f"{original}{n:02d}"
            n += 1

        part.name = candidate
        seen.add(candidate)


# ----------------------------------------------------------------------------
# Trackscript emitter
# ----------------------------------------------------------------------------

def emit(
    conv,
    name,
    sample_files,
    source_name,
    with_message=True,
):
    m = conv.m

    (
        parts,
        measure_placements,
    ) = extract_structure(
        conv,
        sample_files,
    )

    assign_part_names(
        parts,
        m,
    )

    out = []
    A = out.append

    A(f"// {m.title or name}")
    A(
        f"// converted from {source_name} "
        f"by it2trackscript"
    )
    A("//")
    A(
        "// The generated structure is based on "
        "reusable musical parts rather than IT channels."
    )
    A("//")

    if with_message and m.message.strip():
        A("// ---- original song message ----")

        for line in m.message.split('\n'):
            A("// " + line.rstrip())

        A("// ---- end song message ----")
        A("//")

    A(
        "// Samples (WAV files are written at each "
        "sample's C5Speed):"
    )

    for idx, s in enumerate(m.samples, 1):
        if s.file:
            loop = (
                f", loop {s.loop_beg}-{s.loop_end}"
                if s.loop
                else ""
            )

            A(
                f"//   {idx:02d}  {s.file}"
                f"  (C5Speed {s.c5}{loop})"
            )

    A("")

    runs = build_sections(conv)

    first_bpm = (
        runs[0][2]
        if runs
        else 120
    )

    A("Config")
    A("{")
    A("    TrackscriptVersion: 1.10;")
    A(
        f"    BeatsPerMinute: "
        f"{fmt(first_bpm, 2)};"
    )
    A("    TimeSignature: 4/4;")
    A(
        f"    Subdivisions: "
        f"{ROWS_PER_BEAT};"
    )
    A(
        f"    SongVolume: "
        f"{fmt(m.mv * 100.0 / 512.0, 2)}%;"
    )
    A("    PanningLaw: linear;")
    A("    Overlap: stack;")
    A("    PitchDefault: hz;")
    A("}")
    A("")

    wps = tempo_lane(conv)

    if wps:
        A("// One tempo line per measure; § positions are local to that line.")
        A("Tempo")
        A("{")
        measure_rows = ROWS_PER_BEAT * 4
        measure_count = max(1, math.ceil(conv.total_rows / measure_rows))
        tempo_by_measure = defaultdict(list)
        tempo_by_measure[0].append(
            f"<§1/1,#{fmt(first_bpm, 2)}>"
        )
        for row, bpm, d in wps:
            measure = int(row // measure_rows)
            local = row - measure * measure_rows
            tempo_by_measure[measure].append(
                f"&<{pos_str(local)},#{fmt(bpm, 2)}"
                + (f",~{fmt(d / ROWS_PER_BEAT, 2)}" if d else "")
                + ">"
            )
        for measure in range(measure_count):
            A("    " + " ".join(tempo_by_measure[measure]))

        A("}")
        A("")

    # ------------------------------------------------------------------
    # BlockList
    # ------------------------------------------------------------------

    A("BlockList()")
    A("{")

    for measure_parts, _start, _end in measure_placements:
        calls = [f"ƒ{part.name};" for part in measure_parts]
        A("    " + " ".join(calls) if calls else "    ;")

    A("}")
    A("")

    # ------------------------------------------------------------------
    # BlockArray
    # ------------------------------------------------------------------

    A("BlockArray()")
    A("{")

    # Pattern definitions are direct BlockList targets. Each generated
    # pattern contains one measure; repeated material is repeated in the list.
    for part in parts:
        lines = part.rendered.splitlines()

        A(
            f"    {part.name} = "
            f"ƒ'{sample_files[part.sample_index]}'():"
        )
        A("    {")

        for line in lines:
            A(
                "        "
                + line
            )

        A("    }")
        A("")

    A("}")

    if conv.loop_info:
        oi, row, _ = conv.loop_info

        A("")
        A(
            "// original song loops back to "
            f"order {oi:02d} row {row}"
        )

    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------

def make_report(
    conv,
    source_name,
    runs,
):
    m = conv.m

    (
        parts,
        measure_placements,
    ) = extract_structure(
        conv,
        {
            i: s.file
            for i, s in enumerate(m.samples, 1)
            if s.file
        },
    )

    # These are only used for statistics in the report.
    unique_part_count = len(parts)
    pattern_placement_count = sum(len(item[0]) for item in measure_placements)

    r = []
    A = r.append

    A(
        f"it2trackscript report for {source_name}"
    )

    A(f"title: {m.title}")
    A(f"source archive: source/{source_name} (byte-exact)")

    A(
        f"orders: {len(m.orders)}   "
        f"patterns: {len(m.patterns)}   "
        f"rows played: {conv.total_rows}"
    )

    A(
        f"instruments: {len(m.instruments)}   "
        f"samples: {len(m.samples)}"
    )

    A(
        "timing sections: "
        f"{len(runs)} "
        f"("
        f"{', '.join(
            fmt(x[2], 2) + ' BPM'
            for x in runs[:8]
        )}"
        f"{' ...' if len(runs) > 8 else ''}"
        f")"
    )

    A(
        f"notes converted: {len(conv.voices)}   "
        f"waypoints: "
        f"{sum(len(v.points) for v in conv.voices)}"
    )

    A("")

    A("Trackscript structure:")
    A(f"  reusable pattern definitions: {unique_part_count}")
    A(f"  pattern placements in BlockList: {pattern_placement_count}")

    if conv.loop_info:
        A(
            f"song loops back to order "
            f"{conv.loop_info[0]} "
            f"row {conv.loop_info[1]}"
        )

    A("")
    A(
        "approximations / things not converted:"
    )

    if not conv.warn:
        A("  (none)")

    for k, n in conv.warn.most_common():
        extra = ""

        if k in conv.warn_detail:
            extra = (
                "  -- "
                + conv.warn_detail[k]
            )

        A(
            f"  {n:5d} x {k}{extra}"
        )

    envs = []

    for i, ins in enumerate(
        m.instruments,
        1,
    ):
        used = [
            k
            for k, on in ins.env.items()
            if on
        ]

        if (
            used
            and any(
                s
                for _, s in ins.keymap
            )
        ):
            statuses = []
            for name in used:
                envelope = ins.envelopes[name]
                if (
                    name == "volume"
                    and envelope.points
                    and not envelope.loop_enabled
                    and not envelope.sustain_enabled
                ):
                    statuses.append(
                        "volume converted to approximate automation"
                    )
                else:
                    statuses.append(f"{name} playback not converted")
            envs.append(
                f"  instrument {i:02d} {ins.name!r}: "
                f"{'; '.join(statuses)}; source data retained in samples.json"
            )

    if envs:
        A("")
        A(
            "instrument envelope playback status "
            "(all source envelope data retained in samples.json):"
        )

        r.extend(envs)

    return "\n".join(r) + "\n"



# -----------------------------------------------------------------------------
# Shared semantic snapshot
# -----------------------------------------------------------------------------

def _note_name_from_it(note):
    if note is None:
        return None
    if note == 253:
        return "FADE"
    if note == 254:
        return "CUT"
    if note == 255:
        return "OFF"
    if 0 <= note < 120:
        return note_name(note)
    return None


def _effect_name(cmd):
    if not cmd:
        return None
    if 1 <= cmd <= 26:
        return chr(64 + cmd)
    return None


def _decoded_row_events(module):
    out = []
    for pat_i, grid in enumerate(module.patterns):
        for row_i, cells in enumerate(grid):
            events = []
            for ch, cell in enumerate(cells):
                if not cell:
                    continue
                note, inst, volc, cmd, par = cell
                if note is None and inst is None and volc is None and not cmd:
                    continue
                events.append({
                    "channel": ch,
                    "note_raw": note,
                    "note": _note_name_from_it(note),
                    "instrument": inst,
                    "volume_column": volc,
                    "effect": _effect_name(cmd),
                    "effect_raw": cmd,
                    "parameter": par,
                })
            if events:
                out.append({"pattern": pat_i, "row": row_i, "events": events})
    return out


def _voice_semantics(conv):
    out = []
    for i, v in enumerate(conv.voices):
        pitch_hz = v.init.get("pitch") if getattr(v, "init", None) else None
        pitch = None
        if pitch_hz is not None and pitch_hz > 0:
            pitch = round(60 + 12 * math.log2(pitch_hz / C5_HZ))
        out.append({
            "voice": i,
            "channel": v.ch,
            "start_row": v.start,
            "end_row": v.end,
            "end_reason": "natural_end" if getattr(v, "natural_rows", None) is not None and v.end is not None else "channel_end",
            "sample": v.sample,
            "note": _note_name_from_it(v.note),
            "pitch": pitch,
            "pitch_hz": pitch_hz,
            "volume": v.init.get("vol") if getattr(v, "init", None) else None,
            "pan": v.init.get("pan") if getattr(v, "init", None) else None,
            "offset": v.offset,
            "waypoints": [
                {"row": pt, "property": prop, "value": val, "duration": dur}
                for pt, prop, val, dur in getattr(v, "points", [])
            ],
        })
    return out


def _channel_semantics(conv):
    out = []
    for i, st in enumerate(conv.ch):
        out.append({
            "channel": i,
            "instrument": st.inst,
            "volume": st.vol,
            "channel_volume": st.chanvol,
            "pan": st.pan,
            "pitch": st.pitch,
            "global_scale": st.gscale,
            "sample_offset": st.mem_o,
            "vibrato_speed": st.vib_spd,
            "vibrato_depth": st.vib_dep,
            "active_voice": (conv.voices.index(st.voice) if st.voice in conv.voices else None),
        })
    return out


def write_semantics_json(path, module, conv, source_name):
    data = {
        "format": "trackscript-semantics",
        "version": 1,
        "source": source_name,
        "converter": "it2trackscript",
        "source_archive": f"source/{source_name}",
        "module": {
            "title": module.title,
            "initial_speed": module.speed,
            "initial_tempo": module.tempo,
            "global_volume": module.gv,
            "mix_volume": module.mv,
            "stereo_separation": module.sep,
            "pitch_wheel_depth": module.pwd,
            "flags": module.flags,
            "special_flags": module.special,
            "linear_pitch": module.linear,
            "old_effects": module.old_effects,
            "orders_raw": module.orders,
            "channel_pan_raw": module.chnpan,
            "channel_volume_raw": module.chnvol,
            "message": module.message,
            "orders": len(module.orders),
            "patterns": len(module.patterns),
            "instruments": len(module.instruments),
            "samples": len(module.samples),
        },
        "it_decode": {
            "pattern_count": len(module.patterns),
            "instrument_count": len(module.instruments),
            "sample_count": len(module.samples),
            "rows_with_events": _decoded_row_events(module),
        },
        "simulation": {
            "total_rows": conv.total_rows,
            "rows_per_beat": ROWS_PER_BEAT,
            "rows_per_measure": ROWS_PER_BEAT * 4,
            "initial_bpm": conv.bpm(),
            "final_speed": conv.speed,
            "final_tempo": conv.tempo,
            "final_global_volume": conv.gvol,
            "tempo_events": [
                {"row": t, "bpm": bpm}
                for t, _oi, _pat, _row, bpm in conv.rowlog
                if not _oi and not _pat and not _row
            ],
            "channels": _channel_semantics(conv),
            "executed_rows": conv.executed_rows,
            "voices": _voice_semantics(conv),
        },
        "effects": {
            "seen": {},
            "represented": {},
            "approximated": {},
            "dropped": {},
        },
        "warnings": dict(conv.warn),
    }
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def write_samples_json(path, module, sample_files):
    """Write samples.json for the Trackscript player.

    The player reads c5Speed, loop points, and default volume from this file.
    Source IT instrument/sample fields are retained for fidelity and future
    player support, including currently unsupported envelope modes.
    """
    out = {
        "format": "trackscript-sample-library",
        "version": 2,
        "samples": [],
        # Keep IT instrument behavior beside the extracted samples even when
        # the current Trackscript player cannot execute every feature yet.
        "instruments": [],
    }
    for idx, s in enumerate(module.samples, 1):
        entry = {
            "id": idx,
            "name": s.name or "",
            "length": int(s.length),
        }
        if idx in sample_files:
            entry["file"] = sample_files[idx]
        entry["c5Speed"] = int(s.c5 or 8363)
        if s.loop and s.loop_end > s.loop_beg:
            entry.setdefault("loops", {})["main"] = {
                "start": int(s.loop_beg),
                "end": int(s.loop_end),
                "pingpong": bool(s.pingpong),
            }
        if s.sus_loop and s.sus_end > s.sus_beg:
            entry.setdefault("loops", {})["sustain"] = {
                "start": int(s.sus_beg),
                "end": int(s.sus_end),
                "pingpong": False,
            }
        entry["volume"] = {
            "default": int(s.vol * 4),
            "global": int(s.gvl),
            "panning": int(s.dfp & 0x7F) * 4,
        }
        entry["source_it"] = {
            "global_volume": int(s.gvl),
            "default_volume": int(s.vol),
            "flags": int(s.flg),
            "conversion_flags": int(s.cvt),
            "default_pan": int(s.dfp),
            "length_frames": int(s.length),
            "loop_start": int(s.loop_beg),
            "loop_end": int(s.loop_end),
            "c5_speed": int(s.c5),
            "sustain_loop_start": int(s.sus_beg),
            "sustain_loop_end": int(s.sus_end),
            "has_data": bool(s.has_data),
            "is_16_bit": bool(s.is16),
            "stereo": bool(s.stereo),
            "compressed": bool(s.compressed),
            "loop": bool(s.loop),
            "pingpong_loop": bool(s.pingpong),
            "sustain_loop": bool(s.sus_loop),
            "pingpong_sustain": bool(s.pingpong_sus),
        }
        out["samples"].append(entry)
    for idx, ins in enumerate(module.instruments, 1):
        out["instruments"].append({
            "id": idx,
            "name": ins.name,
            "new_note_action": int(ins.nna),
            "duplicate_check_type": int(ins.dct),
            "duplicate_check_action": int(ins.dca),
            "fadeout": int(ins.fadeout),
            "global_volume": int(ins.gbv),
            "default_pan": int(ins.dfp),
            "keymap": [
                {"note": note, "sample": sample}
                for note, sample in ins.keymap
            ],
            "envelopes": {
                name: {
                    "enabled": bool(env.enabled),
                    "loop_enabled": bool(env.loop_enabled),
                    "sustain_enabled": bool(env.sustain_enabled),
                    "loop_start_index": int(env.loop_start),
                    "loop_end_index": int(env.loop_end),
                    "sustain_start_index": int(env.sustain_start),
                    "sustain_end_index": int(env.sustain_end),
                    "points": [
                        {"tick": int(tick), "value": int(value)}
                        for tick, value in env.points
                    ],
                }
                for name, env in ins.envelopes.items()
            },
        })
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2)


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Convert Impulse Tracker "
            "(.it) to Trackscript"
        )
    )

    ap.add_argument(
        'input'
    )

    ap.add_argument(
        '-o',
        '--out',
        help=(
            "output directory "
            "(default: <name>_trackscript)"
        ),
    )

    ap.add_argument(
        '--pitch',
        choices=('hz', 'names'),
        default='names',
        help=(
            "note-on pitch style "
            "(waypoints are always Hz)"
        ),
    )

    ap.add_argument(
        '--no-message',
        action='store_true',
        help=(
            "leave the song message out"
        ),
    )

    args = ap.parse_args()

    with open(args.input, 'rb') as f:
        data = f.read()

    m = parse_it(data)

    base = os.path.splitext(
        os.path.basename(args.input)
    )[0]

    outdir = (
        args.out
        or f"{base}_trackscript"
    )

    os.makedirs(
        os.path.join(
            outdir,
            'samples',
        ),
        exist_ok=True,
    )

    # Keep a byte-exact source copy so unsupported/reserved IT data and any
    # future parser discoveries are never silently discarded by conversion.
    source_dir = os.path.join(outdir, 'source')
    os.makedirs(source_dir, exist_ok=True)
    source_copy = os.path.join(source_dir, os.path.basename(args.input))
    if os.path.abspath(args.input) != os.path.abspath(source_copy):
        shutil.copyfile(args.input, source_copy)

    sample_files = {}

    for idx, s in enumerate(
        m.samples,
        1,
    ):
        if s.pcm:
            fname = (
                f"{idx:02d}_"
                f"{safe_name(s.name or 'sample')}"
                ".wav"
            )

            s.file = (
                f"samples/{fname}"
            )

            sample_files[idx] = s.file

            write_wav(
                os.path.join(
                    outdir,
                    'samples',
                    fname,
                ),
                s,
            )

    write_samples_json(
        os.path.join(outdir, 'samples.json'),
        m,
        sample_files,
    )

    conv = Converter(
        m,
        args.pitch,
    )

    conv.run()

    text = emit(
        conv,
        base,
        sample_files,
        os.path.basename(args.input),
        not args.no_message,
    )

    with open(
        os.path.join(
            outdir,
            f"{base}.trackscript",
        ),
        'w',
        encoding='utf-8',
    ) as f:
        f.write(text)

    report = make_report(
        conv,
        os.path.basename(args.input),
        build_sections(conv),
    )

    with open(
        os.path.join(
            outdir,
            'conversion_report.txt',
        ),
        'w',
        encoding='utf-8',
    ) as f:
        f.write(report)

    semantics_path = os.path.join(
        outdir,
        f"{base}.semantics.json",
    )
    write_semantics_json(
        semantics_path,
        m,
        conv,
        os.path.basename(args.input),
    )

    print(report)
    print(f"wrote {semantics_path}")
    print(
        f"wrote {outdir}/"
    )


if __name__ == '__main__':
    main()
