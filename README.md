# Trackscript

Trackscript is a readable, line-oriented music project format. A score line
inside a pattern is one measure. `§beat/subdivision` is local to that measure;
the line number supplies the measure number. A long note can continue beyond
the line where it starts. Its attached `&` waypoints can continue beyond that
measure too; they travel with each instance of the note's pattern.

The BlockList is the arrangement: each line is one measure of
playback, and the pattern calls on that line play together. Repeating a call on
later lines repeats that pattern. Empty measures are written as `;`.

The parser, player, semantic comparison, and converter share
`trackscript_parser.py`. Generated patterns are intended to be measure-sized
and addressed directly from the BlockList, without a song-length wrapper.
When adjacent note groups recur across measures, the converter factors those
groups into shared patterns and places the shared and remaining notes together
on each affected BlockList line.
It also groups notes using the same sample together within each measure across
source tracker channels, so tracker channel layout does not inflate the
arrangement.

IT conversions also retain the original module byte-for-byte in `source/`.
The generated `samples.json` includes decoded IT sample properties, instrument
keymaps, note actions, fadeout settings, and volume, pan, and pitch envelopes.
This preserves source information that the current Trackscript playback
syntax/player does not yet execute in full; the conversion report identifies
playback approximations and unsupported behaviors.

The reference audio renderer uses an optional native C++ sinc kernel when
`g++` or `clang++` is available. It compiles once into the system temporary
directory and caches the library; without a compiler it falls back to the
slower NumPy implementation.
