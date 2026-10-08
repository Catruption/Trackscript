#!/usr/bin/env python3
"""Make it2trackscript.py use it_parser.py instead of duplicating it.

Removes the local IT-decoding functions and classes and imports them
from the canonical parser.
"""
import re
import sys
from pathlib import Path

TARGET = Path("it2trackscript.py")
src = TARGET.read_text(encoding="utf-8")
orig = src

# Names owned by it_parser.py. Each will be removed from it2trackscript.py
# if it is defined there.
DUPLICATES = [
    "cstr",
    "Instrument", "Sample", "Module",
    "parse_it",
    "unpack_pattern",
    "BitReader",
    "it_decompress",
    "decode_sample",
]


def drop_def(text, name):
    """Remove the top-level def or class named `name` and everything up to
    the next top-level def/class."""
    pattern = re.compile(
        r"^(?:def|class)\s+" + re.escape(name) + r"\b",
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return text, False
    next_def = re.search(
        r"^(?:def|class)\s+\w+",
        text[m.end():],
        re.MULTILINE,
    )
    end = m.end() + next_def.start() if next_def else len(text)
    return text[:m.start()] + text[end:], True


removed = []
for name in DUPLICATES:
    src, ok = drop_def(src, name)
    if ok:
        removed.append(name)

# Add the import if it's not already there.
if "from it_parser import" not in src:
    anchor = "from collections import Counter, defaultdict"
    if anchor not in src:
        sys.exit("cannot find 'from collections import' to anchor the import")
    src = src.replace(
        anchor,
        anchor
        + "\n\nfrom it_parser import (\n"
        "    parse_it, unpack_pattern, decode_sample, it_decompress,\n"
        "    BitReader, cstr,\n"
        "    Instrument, Sample, Module,\n"
        "    IT_16BIT, IT_STEREO, IT_COMPRESSED, IT_SIGNED,\n"
        "    IT_BIG_ENDIAN, IT_DELTA, IT_PTM8TO16, IT_LOOP,\n"
        "    IT_SUSTAIN, IT_PINGPONG, IT_PINGPONG_SUSTAIN, IT_ADPCM,\n"
        "    NOTE_FADE, NOTE_NOTECUT, NOTE_KEYOFF,\n"
        ")",
        1,
    )

if src == orig:
    print("nothing changed")
else:
    TARGET.write_text(src, encoding="utf-8")
    print("removed duplicates:", ", ".join(removed) or "(none)")
    print("added import from it_parser")
