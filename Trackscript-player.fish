#!/usr/bin/env fish
# test.fish — run the full pipeline on one or more .it files
#
#   ./test.fish song.it
#   ./test.fish *.it
#   ./test.fish           # opens a KDE file picker

set -l songs $argv
set -l picked_from_dialog false

if test (count $songs) -eq 0
    if command -q kdialog
        set -l picked (kdialog --getopenfilename (pwd) \
            "*.it|Impulse Tracker modules" \
            --title "Pick an .it file" \
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

for song in $songs
    if not test -f "$song"
        echo "skip: $song (not a file)" >&2
        continue
    end

    set -l base (basename "$song" .it)
    set -l out "out/"$base"_trackscript"

    echo
    echo "════════════════════════════════════════════════════════════════"
    echo "  $song"
    echo "════════════════════════════════════════════════════════════════"

    echo
    echo "── 1/4  Convert ────────────────────────────────────────────────"
    python3 it2trackscript.py "$song" -o "$out"
    or begin
        echo "  convert failed" >&2
        continue
    end

    echo
    echo "── 2/4  Verify samples.json ────────────────────────────────────"
    python3 -c "
import json, sys
d = json.load(open('$out/samples.json'))
for s in d['samples']:
    loop = s.get('loops', {}).get('main')
    tag = f\" loop {loop['start']}-{loop['end']}\" if loop else ''
    print(f\"  {s['id']:3d}  {s['name']:20s}  {s.get('length',0):8d}{tag}\")
"
    or echo "  samples.json missing or malformed" >&2

    echo
    echo "── 3/4  Render IT reference ────────────────────────────────────"
    python3 compare_renditions.py "$song" "$out/$base.trackscript" \
        --render-it-only
    or echo "  libopenmpt render failed" >&2

    echo
    echo "── 4/4  Full comparison ────────────────────────────────────────"
    python3 compare_renditions.py "$song" "$out/$base.trackscript" \
        --block-seconds 15
    or echo "  comparison failed" >&2
end

echo
echo "done."

# keep the terminal open when launched from Dolphin
if test "$picked_from_dialog" = true
    echo
    read -P "press enter to close..." -n 1
end
