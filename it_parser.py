#!/usr/bin/env python3
"""Canonical Impulse Tracker (.it) decoder used by Trackscript tools.

This module owns the binary IT parsing/decompression logic.  Consumers should
import ``parse_it`` and ``unpack_pattern`` rather than maintaining another IT
parser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
import struct
from typing import NamedTuple


class ITNote(IntEnum):
    """Special raw note values stored in IT packed patterns."""

    MPT_FADE = 0xFD
    FADE = 0xFD
    CUT = 0xFE
    KEY_OFF = 0xFF


class ITOrder(IntEnum):
    """Non-pattern entries in an IT order list."""

    SKIP = 0xFE
    END = 0xFF


class ITSampleFlag(IntFlag):
    """Flags in the sample header's ``flg`` byte."""

    DATA_PRESENT = 0x01
    BIT_16 = 0x02
    STEREO = 0x04
    COMPRESSED = 0x08
    LOOP = 0x10
    SUSTAIN_LOOP = 0x20
    PINGPONG_LOOP = 0x40
    PINGPONG_SUSTAIN = 0x80


class ITSampleConversionFlag(IntFlag):
    """Bit flags in the sample header's ``cvt`` byte."""

    SIGNED = 0x01
    BIG_ENDIAN = 0x02
    DELTA = 0x04
    PTM_8_TO_16 = 0x08


# Compatibility aliases for existing converter and patcher imports.
IT_16BIT = ITSampleFlag.BIT_16
IT_STEREO = ITSampleFlag.STEREO
IT_COMPRESSED = ITSampleFlag.COMPRESSED
IT_SIGNED = ITSampleConversionFlag.SIGNED
IT_BIG_ENDIAN = ITSampleConversionFlag.BIG_ENDIAN
IT_DELTA = ITSampleConversionFlag.DELTA
IT_PTM8TO16 = ITSampleConversionFlag.PTM_8_TO_16
IT_LOOP = ITSampleFlag.LOOP
IT_SUSTAIN = ITSampleFlag.SUSTAIN_LOOP
IT_PINGPONG = ITSampleFlag.PINGPONG_LOOP
IT_PINGPONG_SUSTAIN = ITSampleFlag.PINGPONG_SUSTAIN
IT_ADPCM = 0xFF

NOTE_FADE = ITNote.FADE
NOTE_NOTECUT = ITNote.CUT
NOTE_KEYOFF = ITNote.KEY_OFF


class PatternEvent(NamedTuple):
    """One decoded packed-pattern cell; fields retain the IT byte values."""

    note: int | None
    instrument: int | None
    volume: int | None
    command: int | None
    parameter: int


@dataclass
class ITEnvelope:
    enabled: bool = False
    loop_enabled: bool = False
    sustain_enabled: bool = False
    loop_start: int = 0
    loop_end: int = 0
    sustain_start: int = 0
    sustain_end: int = 0
    points: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Instrument:
    name: str = ""
    nna: int = 0
    dct: int = 0
    dca: int = 0
    fadeout: int = 0
    gbv: int = 0
    dfp: int = 0
    keymap: list[tuple[int, int]] = field(default_factory=list)
    env: dict[str, bool] = field(default_factory=dict)
    envelopes: dict[str, ITEnvelope] = field(default_factory=dict)


@dataclass
class Sample:
    name: str = ""
    gvl: int = 0
    flg: int = 0
    vol: int = 0
    cvt: int = 0
    dfp: int = 0
    length: int = 0
    loop_beg: int = 0
    loop_end: int = 0
    c5: int = 0
    sus_beg: int = 0
    sus_end: int = 0
    ptr: int = 0
    has_data: bool = False
    is16: bool = False
    stereo: bool = False
    compressed: bool = False
    loop: bool = False
    pingpong: bool = False
    sus_loop: bool = False
    pingpong_sus: bool = False
    pcm: list[int] | None = None
    file: str | None = None


@dataclass
class Module:
    title: str = ""
    cwtv: int = 0
    cmwt: int = 0
    highlight_minor: int = 0
    highlight_major: int = 0
    gv: int = 0
    mv: int = 0
    speed: int = 0
    tempo: int = 0
    sep: int = 0
    pwd: int = 0
    flags: int = 0
    special: int = 0
    stereo: bool = False
    use_instruments: bool = False
    linear: bool = False
    old_effects: bool = False
    chnpan: list[int] = field(default_factory=list)
    chnvol: list[int] = field(default_factory=list)
    orders: list[int] = field(default_factory=list)
    message: str = ""
    instruments: list[Instrument] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    patterns: list[list[list[PatternEvent | None]]] = field(default_factory=list)


def note_termination(note: int, *, mptm: bool = False) -> str | None:
    """Classify a raw termination note using the source format profile.

    OpenMPT treats 0xFD as fade only in MPTM; in standard IT it is not a note.
    """
    if note == ITNote.MPT_FADE:
        return "fade" if mptm else None
    return {
        ITNote.CUT: "cut",
        ITNote.KEY_OFF: "off",
    }.get(note)


def parse_envelope(data: bytes, offset: int) -> ITEnvelope:
    flags, count, loop_start, loop_end, sustain_start, sustain_end = struct.unpack_from(
        '<6B', data, offset,
    )
    points = []
    for point_index in range(min(count, 25)):
        point_offset = offset + 6 + point_index * 3
        value = data[point_offset]
        if value >= 128:
            value -= 256
        tick = struct.unpack_from('<H', data, point_offset + 1)[0]
        points.append((tick, value))
    return ITEnvelope(
        enabled=bool(flags & 1),
        loop_enabled=bool(flags & 2),
        sustain_enabled=bool(flags & 4),
        loop_start=loop_start,
        loop_end=loop_end,
        sustain_start=sustain_start,
        sustain_end=sustain_end,
        points=points,
    )


def cstr(b: bytes) -> str:
    return b.split(b'\0')[0].decode('latin1').strip()


def parse_it(data):
    if data[:4] != b'IMPM':
        raise ValueError("not an IT module")
    m = Module()
    m.title = cstr(data[4:30])
    m.highlight_minor = data[0x1E] or 4
    m.highlight_major = data[0x1F] or (m.highlight_minor * 4)
    ordnum, insnum, smpnum, patnum, cwt, cmwt, flags, special = struct.unpack_from('<8H', data, 0x20)
    m.gv, m.mv, m.speed, m.tempo, m.sep, m.pwd, msglen, msgoff = struct.unpack_from('<6BHI', data, 0x30)
    m.flags = flags
    m.special = special
    m.cwtv = cwt
    m.cmwt = cmwt
    m.stereo = bool(flags & 1)
    m.use_instruments = bool(flags & 4)
    m.linear = bool(flags & 8)
    m.old_effects = bool(flags & 16)
    m.chnpan = list(data[0x40:0x80])
    m.chnvol = list(data[0x80:0xC0])
    m.orders = list(data[0xC0:0xC0 + ordnum])
    p = 0xC0 + ordnum
    ins_ptr = struct.unpack_from(f'<{insnum}I', data, p); p += 4 * insnum
    smp_ptr = struct.unpack_from(f'<{smpnum}I', data, p); p += 4 * smpnum
    pat_ptr = struct.unpack_from(f'<{patnum}I', data, p)
    m.message = ''
    if (special & 1) and msglen and msgoff + msglen <= len(data):
        m.message = (data[msgoff:msgoff + msglen].split(b'\0')[0].decode('latin1').replace('\r', '\n'))
    m.instruments = []
    for o in ins_ptr:
        i = Instrument()
        i.name = cstr(data[o + 0x20:o + 0x3A])
        i.nna = data[o + 0x11]
        i.dct = data[o + 0x12]
        i.dca = data[o + 0x13]
        i.fadeout = struct.unpack_from('<H', data, o + 0x14)[0]
        i.gbv = data[o + 0x18]
        i.dfp = data[o + 0x19]
        i.keymap = [(data[o + 0x40 + 2*n], data[o + 0x41 + 2*n]) for n in range(120)]
        i.env = {}
        i.envelopes = {}
        for nm, off in (
            ('volume', 0x130),
            ('pan', 0x182),
            ('pitch', 0x1D4),
        ):
            i.envelopes[nm] = parse_envelope(data, o + off)
            i.env[nm] = i.envelopes[nm].enabled
        m.instruments.append(i)
    m.samples = []
    for o in smp_ptr:
        s = Sample()
        s.name = cstr(data[o + 0x14:o + 0x2E])
        s.gvl = data[o + 0x11]
        s.flg = data[o + 0x12]
        s.vol = data[o + 0x13]
        s.cvt = data[o + 0x2E]
        s.dfp = data[o + 0x2F]
        (s.length, s.loop_beg, s.loop_end, s.c5,
         s.sus_beg, s.sus_end, s.ptr) = struct.unpack_from('<7I', data, o + 0x30)
        s.has_data = bool(s.flg & ITSampleFlag.DATA_PRESENT) and s.length > 0
        s.is16 = bool(s.flg & ITSampleFlag.BIT_16)
        s.stereo = bool(s.flg & ITSampleFlag.STEREO)
        s.compressed = bool(s.flg & ITSampleFlag.COMPRESSED)
        s.loop = bool(s.flg & ITSampleFlag.LOOP)
        s.pingpong = bool(s.flg & ITSampleFlag.PINGPONG_LOOP)
        s.sus_loop = bool(s.flg & ITSampleFlag.SUSTAIN_LOOP)
        s.pingpong_sus = bool(s.flg & ITSampleFlag.PINGPONG_SUSTAIN)
        s.pcm = None
        s.file = None
        m.samples.append(s)
    for s in m.samples:
        if s.has_data:
            s.pcm = decode_sample(data, s)
    m.patterns = [unpack_pattern(data, o) for o in pat_ptr]
    return m


def unpack_pattern(data, off):
    if off == 0:
        return [[None] * 64 for _ in range(64)]
    length, nrows = struct.unpack_from('<HH', data, off)
    p = off + 8
    end = min(off + 8 + length, len(data))
    grid = [[None] * 64 for _ in range(nrows)]
    lmask = [0] * 64
    lnote = [None] * 64
    linst = [None] * 64
    lvol = [None] * 64
    lcmd = [(None, None)] * 64
    r = 0
    while r < nrows and p < end:
        cv = data[p]; p += 1
        if cv == 0:
            r += 1
            continue
        ch = cv & 0x7F
        if ch:
            ch -= 1
        if ch >= 64:
            break
        if cv & 128:
            if p >= end:
                break
            lmask[ch] = data[p]; p += 1
        mk = lmask[ch]
        note = inst = vol = cmd = par = None
        try:
            if mk & 1: note = data[p]; p += 1; lnote[ch] = note
            elif mk & 16: note = lnote[ch]
            if mk & 2: inst = data[p]; p += 1; linst[ch] = inst
            elif mk & 32: inst = linst[ch]
            if mk & 4: vol = data[p]; p += 1; lvol[ch] = vol
            elif mk & 64: vol = lvol[ch]
            if mk & 8: cmd, par = data[p], data[p+1]; p += 2
            elif mk & 128: cmd, par = lcmd[ch]
        except IndexError:
            break
        if any(v is not None for v in (note, inst, vol, cmd)):
            grid[r][ch] = PatternEvent(
                note,
                inst or None,
                vol,
                cmd or None,
                par or 0,
            )
    return grid


class BitReader:
    def __init__(self, blk):
        self.blk = blk; self.bi = 0; self.acc = 0; self.nb = 0
    def read(self, n):
        while self.nb < n:
            b = self.blk[self.bi] if self.bi < len(self.blk) else 0
            self.acc |= b << self.nb
            self.bi += 1
            self.nb += 8
        v = self.acc & ((1 << n) - 1)
        self.acc >>= n
        self.nb -= n
        return v


def _change_it_width(current_width, encoded_width):
    new_width = encoded_width + 1
    if new_width >= current_width:
        new_width += 1
    return new_width


def _it_decompress_channel(src, pos, length, is16, it215):
    bitsize = 16 if is16 else 8
    initial_width = bitsize + 1
    maxblock = 0x4000 if is16 else 0x8000
    half = 1 << (bitsize - 1)
    mask = (1 << bitsize) - 1
    esc_bits = 4 if is16 else 3
    lower_b = -8 if is16 else -4
    upper_b = 7 if is16 else 3

    out = []
    while len(out) < length and pos + 2 <= len(src):
        blen = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if blen == 0 or pos + blen > len(src):
            break
        br = BitReader(src[pos:pos + blen])
        pos += blen

        n = min(maxblock, length - len(out))
        width = initial_width
        d1 = 0
        d2 = 0

        while n:
            if width < 1 or width > initial_width:
                break

            value = br.read(width)
            top_bit = 1 << (width - 1)

            if width <= 6:
                if value == top_bit:
                    width = _change_it_width(width, br.read(esc_bits))
                    continue
            elif width < initial_width:
                lower_border = top_bit + lower_b
                upper_border = top_bit + upper_b
                if lower_border <= value <= upper_border:
                    width = _change_it_width(
                        width,
                        value - lower_border,
                    )
                    continue
            elif value & top_bit:
                width = (value ^ top_bit) + 1
                continue

            if width < bitsize:
                shift = bitsize - width
                dw = (value << shift) & mask
                if dw >= half:
                    dw -= 1 << bitsize
                dw >>= shift
            else:
                dw = value & mask
                if dw >= half:
                    dw -= 1 << bitsize

            d1 += dw
            d2 += d1
            v = d2 if it215 else d1
            out.append(((v + half) & mask) - half)

            n -= 1

    return out, pos


def it_decompress(src, length, is16, it215, channels=1):
    if channels < 1:
        raise ValueError("channel count must be positive")

    planes = []
    pos = 0
    for _ in range(channels):
        samples, pos = _it_decompress_channel(
            src,
            pos,
            length,
            is16,
            it215,
        )
        planes.append(samples)

    if channels == 1:
        return planes[0]

    return [sample for frame in zip(*planes) for sample in frame]


def decode_sample(data, s):
    if not s.has_data or s.ptr <= 0 or s.ptr >= len(data):
        return []
    cvt = s.cvt
    channels = 2 if s.stereo else 1
    if s.compressed:
        it215 = bool(cvt & IT_DELTA)
        return it_decompress(
            data[s.ptr:],
            s.length,
            s.is16,
            it215,
            channels,
        )
    if not s.is16 and cvt == IT_ADPCM:
        if s.ptr + 16 > len(data): return []
        lut = struct.unpack_from('<16b', data, s.ptr)
        start = s.ptr + 16
        n = min((s.length + 1) // 2, len(data) - start)
        out = []; delta = 0
        for i in range(n):
            b = data[start + i]
            delta = (delta + lut[b & 0x0F]) & 0xFF
            if delta >= 128: delta -= 256
            out.append(delta)
            delta = (delta + lut[(b >> 4) & 0x0F]) & 0xFF
            if delta >= 128: delta -= 256
            out.append(delta)
        return out[:s.length]
    signed = bool(cvt & IT_SIGNED)
    big_endian = bool(cvt & IT_BIG_ENDIAN)
    delta = bool(cvt & IT_DELTA)
    if s.is16:
        endian = '>' if big_endian else '<'
        needed = s.length * channels * 2
        if s.ptr + needed > len(data):
            needed = len(data) - s.ptr
            if needed < 2: return []
        n = needed // 2
        fc = 'h' if signed else 'H'
        vals = list(struct.unpack_from(f'{endian}{n}{fc}', data, s.ptr))
        if not signed: vals = [x - 32768 for x in vals]
        if delta:
            acc = 0; out = []
            for v in vals:
                acc = (acc + v) & 0xFFFF
                if acc >= 32768: acc -= 65536
                out.append(acc)
            vals = out
        return vals
    needed = s.length * channels
    if s.ptr + needed > len(data): needed = len(data) - s.ptr
    raw = data[s.ptr:s.ptr + needed]
    if signed: vals = [b - 256 if b >= 128 else b for b in raw]
    else: vals = [b - 128 for b in raw]
    if delta:
        acc = 0; out = []
        for v in vals:
            acc = (acc + v) & 0xFF
            if acc >= 128: acc -= 256
            out.append(acc)
        vals = out
    return vals
