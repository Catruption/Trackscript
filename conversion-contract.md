# Trackscript ↔ IT Conversion Contract

This document describes the rules that any IT → Trackscript conversion
must follow. It is intended to be short, stable, and checkable.

Implementation-specific detail belongs in `conversion-notes.md`.

---

## Purpose

The converter's job is to represent the musical behavior of an
Impulse Tracker module using Trackscript's vocabulary.

When Trackscript cannot express something exactly, the converter must
report the approximation or use a shim. It must not silently drop
information.

---

## Authority

When sources disagree, use this order:

1. The source `.it` file is authoritative for the module.
2. libopenmpt is the reference renderer for audible and timing behavior.
3. The Trackscript specification is authoritative for Trackscript
   syntax and semantics.
4. `it2trackscript.py` is an implementation, not an authority.
5. `trackscript_play.py` is a Trackscript player, not an authority on
   IT semantics.

Never adjust the interpretation of the source to make the converter's
current output appear correct.

---

## Accounting

Every recognized source effect must end in exactly one of four
outcomes:

| Outcome        | Meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| represented    | Source behavior has a faithful Trackscript representation.    |
| approximated   | Represented, but the representation is not exact.             |
| dropped        | No representation is emitted.                                 |
| parser warning | The parser could not determine what the source command means. |

Nothing may be silently lost.

Parsing is not the same as representing. A value that is read into
temporary converter state but never reaches the emitted Trackscript
does not count as represented.

The converter's final report must show the accounting.

Example format:

```text
conversion report:

  M: 1000 source / 1000 represented / 0 approximated / 0 dropped
  X:  355 source /  355 represented / 0 approximated / 0 dropped
  H:   90 source /    0 represented / 90 approximated / 0 dropped

  approximated:
    H: 90
    K: 54

  dropped:
    Z: 60

  parser warnings:
    S: 7
```

Exact format may change. The accounting must exist.

---

## Representation philosophy

When converting an IT effect, prefer representations in this order:

1. An existing Trackscript property or symbol.
2. A `&`, `=`, `▲`, `♪` connector, or a combination.
3. An inline operator for values that change over time.
4. An instrument sidecar for instrument behavior, envelopes, and
   voice lifetime.
5. A shim, only when Trackscript genuinely cannot express the effect.
6. An explicit report when the effect cannot be represented faithfully.

Do not invent a Trackscript extension when an existing mechanism
already expresses the behavior.

Prefer **semantic fidelity** over mimicking the source syntax.

Exception: tempo (BPM) and measure (time-signature) changes are shims.
They affect everything after the point where they happen, so they are
not written inside a note. A shim can take a `§` like a note does,
for example `@<§2/3,'Tempo'(120)>`. With no `§` it takes effect at the
start of the measure it is written in.

---

## Invariants

These should remain true unless the Trackscript specification itself
changes.

### Measure patterns and arrangement

Trackscript positions, including tempo-shim positions, are local to the
measure line that contains them. A note's attached `&` waypoints are also
anchored to its pattern placement, but may extend beyond that measure; this
lets automation follow a note that rings across a measure boundary. For
example, in 4/4, `§5/1` is the first beat after the pattern's measure. Each
BlockList instance carries its attached waypoints forward by the same offset.
`§1/1` means the start of that line's measure on every line; it is never an
absolute song position. Each score line is one measure. A note may continue
past the measure where it begins, but that does not merge adjacent patterns.

The BlockList is the arrangement, also read one line per measure. Pattern
calls on a line play together; repeating a call on later lines repeats the
pattern. Generated conversions should emit measure-sized patterns and direct
BlockList calls. They must not wrap the whole song in a composite block or
encode song time as ever-increasing `§` beat values.

### Sample tuning

The WAV is written at the IT `c5Speed` value. Therefore:

```text
WAV sample rate = IT c5Speed
playback_rate = c5Speed × (pitch_hz / 523.25)
```

At C5, play at `c5Speed`. At C6, `2 × c5Speed`. At C4, `0.5 × c5Speed`.

The loader variable currently named `sample_sr` is the same value as
`c5Speed` and should be renamed for clarity.

### Connector semantics

A slide connector:

```text
<A>&<B>
```

blends note A into note B across the space between them. B may name
only the properties that change; everything it leaves out is inherited
from A. If both notes have no length, only the blend remains.

This is how a change to a voice that is already sounding is written. The
waypoint remains attached to the note even when its position is beyond the
measure where that note began.
Which curve the blend uses is not yet defined by the Trackscript
specification.

### Channel automation

`M` and `X` affect voices already active on the affected channel.

They must not overwrite the voice's intrinsic properties. The emitted
value must represent the resulting effective channel/voice state.

They are written as `&` blends that name only the changed property
(see Connector semantics).

### Global automation

`V` and `W` affect the whole arrangement, not one channel.

### Timing

IT `T` commands affect tempo. The parameter range determines behavior:

| Range     | Meaning                              |
| --------- | ------------------------------------ |
| `T00–T0F` | Slide tempo down                     |
| `T10–T1F` | Slide tempo up                       |
| `T20–TFF` | Set tempo directly to `xx` BPM       |

The slide ranges change tempo during the row containing the command.
OpenMPT applies the low nibble on ticks after the row's first tick. The
converter represents that discrete per-tick change as a linear BPM ramp
over one row; this preserves its timing span and ending tempo, while the
within-row curve remains an approximation. See `conversion-notes.md`.

IT speed, `Axx`, sets ticks per row. It is not the same as `T`, but it
changes row length in the same way. Trackscript has no separate speed,
so the converter folds speed into the tempo shim as an effective BPM:

```text
BPM = 24 × tempo ÷ (rowsPerBeat × speed)
```

At 4 rows per beat this is `6 × tempo ÷ speed`. A change that does not
fall on the start of a measure uses the shim's `§`.

### Accounting

Every recognized source effect must result in one of the four
outcomes listed in the Accounting section.

Never silently discard a recognized source command.

---

## Priorities

When deciding what to implement next, order by expected fidelity
impact per unit of work.

1. Timing changes (`T`).
2. Channel-volume automation (`M`).
3. Channel-pan automation (`X`).
4. Sample offsets (`O`).
5. Voice lifecycle and NNA.
6. Vibrato (`H`, `K`).
7. Pitch slides (`G`, `F`, `E`).
8. Global-volume automation (`V`, `W`).
9. Filter / MIDI macros (`Z`).
10. Parser investigation (`S`).

Re-rank when new information changes the expected impact.

---

## Validation

Each major change should be compared against the libopenmpt render.

Compare, in this order of importance:

1. Musical events (which notes, when).
2. Timing (tempo changes, row alignment).
3. Pitch and tuning.
4. Dynamics (channel volume, global volume).
5. Stereo movement.
6. Voice behavior (release, tails, overlap).
7. Loop behavior.
8. Total duration.

A change that improves one dimension while regressing another is not
automatically an improvement.

The goal is not merely:

```text
same duration
```

but:

```text
same musical events
+ same timing
+ same pitch
+ same dynamics
+ same stereo movement
+ same voice behavior
```

within the representational limits of Trackscript.
