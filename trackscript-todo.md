# Trackscript TODO

## Blocks converter work (in contract priority order)
1. Timing (T)
   - [ ] Compare the one-row linear ramp approximation against a libopenmpt render (see "Tempo slides" below).
2. Channel volume and pan (M, X)
   - [ ] Interpolation syntax for a blend (which curve `&` uses). Undecided. Until it is, the converter can emit the plain blend with no curve written. Say what the default shape is (linear?), since IT volume and pan slides are linear per tick.
3. Sample offsets (O)
   - [x] Static offsets use the source call argument: `ƒ'sample.wav'(5)` means start 5 sample frames into the source. Empty `()` means offset 0.
   - [ ] Define source-argument operators for time-varying offsets, including units, loop behavior, and bounds.
4. Voice lifecycle and NNA
   - [ ] Add an instrument-sidecar representation for NNA off/fade and duplicate-check actions; NNA continue is now emitted as overlapping voices.

## Contract checks (verify against libopenmpt)
- [x] OpenMPT source: IT applies T0x/T1x using the low nibble on ticks after the first tick of the row; net change is low-nibble × (speed - 1).
- [ ] Compare the converter's linear one-row approximation against libopenmpt audio and timing.
- [ ] Contract: record tempo/BPM and measure as the shim exception under Representation philosophy.
- [ ] How a measure shim relates to a pattern signature and `BlockList(4/4)`.
- [ ] Connector spacing rule (proposed): a connector glued to the note on its left (`>@ <`) connects the previous note to the next one; a connector with a space on its left (`> @<`) is standalone. A space on the right lets it find the next note across rests and measures. Need the full table for all four spacing cases, including `> @ <` and the glued-both-sides `>@<`, and whether a newline counts as a space.
- [ ] Define "the next note": next in written order or next in time? Does the search stop at the end of a pattern, or cross pattern boundaries? (No note found: do nothing, as already stated for `=`, `▲`, `♪`.)
- [ ] Say that the same spacing rule applies to `&`, `=`, `▲`, `♪` and shims, and add it to the connector section of the example.
- [ ] Say how channel state (M/X channel volume and pan) is expressed in general Trackscript automation.
- [ ] Reword the contract's "& modifies a voice that is already playing" to match the format's meaning (blend between two notes).
- [ ] The contract embeds its own TODO under Timing; move it here or keep one copy.

## Format spec gaps
- [x] `&` waypoints stay attached to their note and may extend beyond its measure; each BlockList instance carries them forward with the note.
- [ ] Count semantics: `<%15+0.1→0/1:4>` makes 4 changes, but `<§1/1+0/0.25:2>` makes 2 copies total (1 change). Pick one meaning of `:count`.
- [ ] Syntax for a pattern selecting an instrument sidecar.
- [ ] Native Trackscript/player execution for instrument envelopes, especially loop/sustain and pan/pitch modes. IT conversion now retains these in `samples.json` and the original IT module in `source/`.
- [ ] Envelope section: sustain index base (0 or 1), NNA values and how IT cut/continue/off/fade map to them.
- [ ] Volume scales in samples.json (default 256, global 64): name them and say how they map to `%`.
- [ ] Documentation of soundfont support as planned (ƒ sound sources).
- [ ] The converter must emit the new operator form (`→`, not `×`).

## Done
- [x] A pattern score line is one measure; `§beat/subdivision` is local to that line. Notes may ring past the line without merging patterns.
- [x] The BlockList is the arrangement, one measure per line. Pattern calls on a line play together; repeated calls repeat the pattern.
- [x] `§N/M` — N is the beat within the current measure (1-based, must be ≤ beats per measure for the block's signature), M is the subdivision within that beat (1-based). Each line is a new measure, so N counts from 1 on every line.
- [x] A shim can take a `§` like a note (`@<§2/3,'Tempo'(120)>`); with no `§` it takes effect at the start of its measure. The `@` and quoted name tell a shim from a note.
- [x] IT speed folds into the tempo shim: BPM = 24 × tempo ÷ (rowsPerBeat × speed).
- [x] Mid-note volume or pan changes use `&` slide targets that name only the changed value and inherit the rest. A zero-length note makes a single blend. Example: `<%100,¶0>&<%60>`.
- [x] Tempo shim `~` is the ramp length in beats (same meaning as everywhere else).
- [x] Tempo/BPM and measure changes are shims set from the BlockList (a documented exception to "prefer symbols over shims"; the contract should say so).
- [x] Operator grammar and "no :count means once".
- [x] Separator rule: each call ends with ";".
- [x] Undefined block call is a hard error.
