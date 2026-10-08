# Conversion notes

Implementation-specific detail for the IT → Trackscript converter.
The rules are in `conversion-contract.md`. Open questions about the
format itself are in `trackscript-todo.md`.

---

## Files

| File                     | Role                                                                 |
| ------------------------ | -------------------------------------------------------------------- |
| `it2trackscript.py`      | Converter (IT → Trackscript).                                        |
| `trackscript_play.py`    | Trackscript player.                                                  |
| `compare_renditions.py`  | Renders the `.it` with libopenmpt and the `.trackscript` with the player, aligns them, and reports per-band divergence. Writes aligned and diff wavs. |

Test module: `wind_of_fjords_2.5_release.it`.

---

## Mapping decisions

| IT                         | Trackscript                                                              |
| -------------------------- | ------------------------------------------------------------------------ |
| `T` set (T20–TFF)          | Tempo shim, e.g. `@<'Tempo'(120)>`.                                      |
| `T` slide (T00–T1F)        | Tempo shim ramp spanning the source row; per-tick OpenMPT steps are approximated by a linear BPM ramp. |
| `Axx` speed                | Folded into the tempo shim as an effective BPM (formula in the contract). |
| Change not on a measure start | Shim with a measure-local `§` on the matching measure line.                   |
| `M`, `X`                   | `&` blend that names only the changed value, e.g. `<%80,¶0>&<§2/1,%60>`. |
| Instrument behavior        | Instrument sidecar, written by the converter.                            |
| IT master preamp (`mv`)    | `SongVolume: mv / 512`, matching OpenMPT's IT-compatible mix attenuation. |
| IT panning                 | `PanningLaw: linear`, matching OpenMPT's IT-compatible balance pan.       |

`rowsPerBeat` in the speed formula is whatever value the converter
uses to turn rows into beats. Confirm what `it2trackscript.py` uses
before applying the formula.

---

## Next steps, in contract priority order

1. **Compare the one-row `T` slide ramp against a libopenmpt render.**
   OpenMPT's source applies the low nibble on ticks after the first tick
   of the row. The converter preserves the total row span and final BPM,
   but its linear ramp approximates OpenMPT's discrete tick steps.
2. Emit tempo shims with measure-local `§` positions and one tempo line per
   measure.
3. Emit `M` and `X` as `&` blends.
4. Sample offsets (`O`). Trackscript has no property for this yet.
5. Voice lifecycle and NNA. Needs the block-to-instrument declaration
   syntax.

---

## Known issues (as last observed on wind_of_fjords)

- Chorus clicks and is too loud.
- The drone has a repeating problem.
- Suspected cause of some of the above, not confirmed: the powerchord
  sample's offbeat double-hit happens only in the bridge in the `.it`,
  but the converter may be applying it to every powerchord hit.

---

## Cleanup

- Rename the loader variable `sample_sr` to `c5Speed` (same value).
- The final report must show the accounting (see the contract).
