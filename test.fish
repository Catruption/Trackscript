#!/usr/bin/env fish
# test.fish — run the full Trackscript conversion/diagnostic pipeline
#
#   ./test.fish song.it
#   ./test.fish *.it
#   ./test.fish           # opens a KDE file picker

set -l songs $argv
set -l picked_from_dialog false

# ── Pick files if none were supplied ────────────────────────────────────────

if test (count $songs) -eq 0
    if command -q kdialog
        set -l picked (kdialog --getopenfilename (pwd) \
            "*.it|Impulse Tracker modules" \
            --title "Pick .it files" \
            --multiple --separate-output)

        if test -z "$picked"
            exit 0
        end

        set songs $picked
        set picked_from_dialog true
    else
        set songs tests/simple.it
    end
end

set -l any_failed false

# ── Shared parser layer ─────────────────────────────────────────────────────

echo
echo "── Parser layer ────────────────────────────────────────────────"
python3 -m py_compile it_parser.py trackscript_parser.py it2trackscript.py semantic_diff.py pipeline_diagnose.py
or begin
    echo "  ✗ shared parser layer failed to compile/import" >&2
    exit 1
end
echo "  ✓ canonical IT parser:          it_parser.py"
echo "  ✓ canonical Trackscript parser: trackscript_parser.py"

echo
echo "── Focused regression tests ────────────────────────────────────"
python3 -m unittest test_it_parser test_trackscript_timing test_compare_renditions
or begin
    echo "  ✗ focused regression tests failed" >&2
    exit 1
end
echo "  ✓ IT decoding, tempo automation, and audio alignment"

# ── Process each module ─────────────────────────────────────────────────────

for song in $songs
    if not test -f "$song"
        echo "skip: $song (not a file)" >&2
        continue
    end

    set -l base (basename "$song" .it)
    set -l out "out/"$base"_trackscript"

    set -l ts_file "$out/$base.trackscript"
    set -l semantics_file "$out/$base.semantics.json"
    set -l samples_file "$out/samples.json"
    set -l render_cache "$out/.render_cache"
    set -l it_wav "$render_cache/it.wav"
    set -l ts_wav "$render_cache/trackscript.wav"

    set -l semantic_ok true
    set -l pipeline_ok true
    set -l duration_ok true
    set -l render_ok true
    set -l audio_ok true
    set -l timeline_ok true

    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "  $song"
    echo "════════════════════════════════════════════════════════════════"

    # ── 1/8 Convert ─────────────────────────────────────────────────────────

    echo
    echo "── 1/8  Convert ────────────────────────────────────────────────"

    python3 it2trackscript.py "$song" -o "$out"
    or begin
        echo "  ✗ conversion failed" >&2
        set any_failed true
        continue
    end

    if not test -f "$semantics_file"
        echo "  ✗ semantics JSON was not produced: $semantics_file" >&2
        set any_failed true
        continue
    end

    # ── 2/8 Verify samples.json ─────────────────────────────────────────────

    echo
    echo "── 2/8  Verify samples.json ───────────────────────────────────"

    if test -f "$samples_file"
        python3 -c "
import json
d = json.load(open('$samples_file'))
for s in d['samples']:
    loop = s.get('loops', {}).get('main')
    tag = f\" loop {loop['start']}-{loop['end']}\" if loop else ''
    print(f\"  {s['id']:3d}  {s['name']:20s}  {s.get('length',0):8d}{tag}\")
"
        or begin
            echo "  ✗ samples.json is malformed" >&2
            set any_failed true
        end
    else
        echo "  ✗ samples.json missing" >&2
        set any_failed true
    end

    # ── 3/8 Semantic verification ──────────────────────────────────────────

    echo
    echo "── 3/8  Semantic verification ──────────────────────────────────"

    python3 semantic_diff.py "$semantics_file" "$ts_file" --around 8
    or begin
        set semantic_ok false
        echo "  ✗ semantic comparison failed" >&2
    end

    # ── 4/8 Converter → Trackscript diagnostic ─────────────────────────────

    echo
    echo "── 4/8  Converter → Trackscript diagnostic ────────────────────"

    python3 pipeline_diagnose.py "$semantics_file" "$ts_file"
    or begin
        set pipeline_ok false
        echo "  ✗ converter/Trackscript diagnostic failed" >&2
    end

    # ── 5/8 Duration gate ───────────────────────────────────────────────────

    echo
    echo "── 5/8  Duration gate ──────────────────────────────────────────"

    python3 -c "
import sys
import os
from pathlib import Path
import hashlib
import json

sys.path.insert(0, '.')

from compare_renditions import render_it, render_ts, _write_wav, SR

cache = Path('$render_cache')
cache.mkdir(parents=True, exist_ok=True)
def digest(paths):
    return {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}

it_key = digest(['$song', 'compare_renditions.py'])
ts_key = digest(['$ts_file', '$samples_file', 'trackscript_play.py', 'trackscript_native.py', 'sinc_kernel.cpp', 'compare_renditions.py'])
it_wav = cache / 'it.wav'
ts_wav = cache / 'trackscript.wav'
it_stamp = cache / 'it.cache.json'
ts_stamp = cache / 'trackscript.cache.json'
it_valid = it_wav.exists() and it_stamp.exists() and json.loads(it_stamp.read_text()) == it_key
ts_valid = ts_wav.exists() and ts_stamp.exists() and json.loads(ts_stamp.read_text()) == ts_key
if it_valid:
    from compare_renditions import load_rendered_wav
    left, right = load_rendered_wav(it_wav)
    it_dur = len(left) / SR
    print('  Reusing cached IT render.')
else:
    print('  Rendering IT reference...')
    left, right = render_it('$song')
    it_dur = len(left) / SR
    _write_wav(str(it_wav), left, right)
    it_stamp.write_text(json.dumps(it_key, indent=2))
if ts_valid:
    from compare_renditions import load_rendered_wav
    left, right = load_rendered_wav(ts_wav)
    ts_dur = len(left) / SR
    print('  Reusing cached Trackscript render.')
else:
    print('  Rendering Trackscript...')
    left, right, _ = render_ts('$ts_file')
    ts_dur = len(left) / SR
    _write_wav(str(ts_wav), left, right)
    ts_stamp.write_text(json.dumps(ts_key, indent=2))

pct = 100.0 * abs(ts_dur - it_dur) / it_dur

print(f'  IT duration: {it_dur:.2f}s')
print(f'  TS duration: {ts_dur:.2f}s')
print(f'  difference:  {pct:+.2f}%')

if pct > 5.0:
    print('  ✗ FAIL: durations differ by more than 5%')
    sys.exit(1)

if pct > 1.5:
    print('  ⚠ WARN: durations differ by more than 1.5%')
else:
    print('  ✓ durations within tolerance')
"
    or begin
        set duration_ok false
        set any_failed true
    end

    # ── 6/8 Verify cached IT reference ──────────────────────────────────────

    echo
    echo "── 6/8  Verify cached renders ──────────────────────────────────"
    if test -s "$it_wav"; and test -s "$ts_wav"
        echo "  ✓ IT and Trackscript renders cached for later analysis"
    else
        set render_ok false
        echo "  ✗ cached render missing" >&2
    end

    # ── 7/8 Full audio comparison ───────────────────────────────────────────

    echo
    echo "── 7/8  Full audio comparison ──────────────────────────────────"

    python3 compare_renditions.py "$song" "$ts_file" --block-seconds 15 --it-wav "$it_wav" --ts-wav "$ts_wav"
    or begin
        set audio_ok false
        echo "  ✗ audio comparison failed" >&2
    end

    # ── 8/8 Timeline report ─────────────────────────────────────────────────

    echo
    echo "── 8/8  Timeline report ────────────────────────────────────────"

    python3 timeline_report.py "$song" "$ts_file" --win 2 --strict --it-wav "$it_wav" --ts-wav "$ts_wav"
    or begin
        set timeline_ok false
        echo "  ✗ timeline report failed" >&2
    end

    # ── Summary ─────────────────────────────────────────────────────────────

    echo
    echo "── Summary ─────────────────────────────────────────────────────"

    if test "$semantic_ok" = true
        echo "  ✓ semantics"
    else
        echo "  ✗ semantics"
    end

    if test "$pipeline_ok" = true
        echo "  ✓ converter → Trackscript"
    else
        echo "  ✗ converter → Trackscript"
    end

    if test "$duration_ok" = true
        echo "  ✓ duration"
    else
        echo "  ✗ duration"
    end

    if test "$render_ok" = true
        echo "  ✓ reference render"
    else
        echo "  ✗ reference render"
    end

    if test "$audio_ok" = true
        echo "  ✓ audio comparison"
    else
        echo "  ✗ audio comparison"
    end

    if test "$timeline_ok" = true
        echo "  ✓ timeline report"
    else
        echo "  ✗ timeline report"
    end

    echo

    if test "$semantic_ok" = true; and \
       test "$pipeline_ok" = true; and \
       test "$duration_ok" = true; and \
       test "$render_ok" = true; and \
       test "$audio_ok" = true; and \
       test "$timeline_ok" = true

        echo "  ✓ PASS: $base"
    else
        echo "  ✗ FAIL: $base"
        set any_failed true
    end
end

# ── Final result ─────────────────────────────────────────────────────────────

set -l exit_code 0

echo

if test "$any_failed" = true
    echo "done (with failures)."
    set exit_code 1
else
    echo "done."
end

if test "$picked_from_dialog" = true
    echo
    read -P "press enter to close..." -n 1
end

exit $exit_code
