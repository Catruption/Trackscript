import tempfile
import unittest
from pathlib import Path

from trackscript_parser import parse_file
from it2trackscript import PartDefinition, factor_repeated_event_pairs


class MeasurePlacementTests(unittest.TestCase):
    def test_repeated_adjacent_events_are_factored_into_reused_patterns(self):
        common = [
            '<§1/1,#C5,~2.39,%68.8>',
            '<§3/3,#C5,~1,%68.8>',
        ]
        first = PartDefinition('first', [], 0, 16, 1)
        first.rendered = '@4/4[\n    ' + ', '.join(common + ['<§4/3,#C6,~0.5,%68.8>']) + '\n]'
        second = PartDefinition('second', [], 16, 32, 1)
        second.rendered = '@4/4[\n    ' + ', '.join(common + ['<§4/3,#C5,~0.5,%68.8>']) + '\n]'

        parts, placements = factor_repeated_event_pairs(
            [first, second],
            [([first], 0, 16), ([second], 16, 32)],
        )

        self.assertEqual(len(parts), 3)
        self.assertEqual([len(line_parts) for line_parts, _, _ in placements], [2, 2])
        self.assertIs(placements[0][0][0], placements[1][0][0])
        self.assertIn('#C6', placements[0][0][1].rendered)
        self.assertIn('#C5', placements[1][0][1].rendered)

    def test_block_list_lines_place_one_measure_patterns(self):
        text = """\
Config { TimeSignature: 4/4; Subdivisions: 4; BeatsPerMinute: 120; }
BlockList(4/4)
{
    ƒKick;
    ;
    ƒKick;
}
BlockArray(4/4)
{
    Kick = ƒ'kick.wav'():
    {
        @4/4[
            <§1/1,#C2,~0.25>
            <§2/1,#D2,~0.25>
        ]
    }
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'measure.trackscript'
            path.write_text(text, encoding='utf-8')
            parsed = parse_file(path)

        self.assertEqual([len(line) for line in parsed.block_list], [1, 0, 1])
        self.assertEqual([event.row for event in parsed.parts['Kick'].events], [0, 4])
        self.assertEqual([note.row for note in parsed.notes], [0, 4, 32, 36])

    def test_tempo_positions_advance_by_measure_lines(self):
        text = """\
Config { TimeSignature: 4/4; Subdivisions: 4; BeatsPerMinute: 120; }
Tempo
{
    <§1/1,#120>
    &<§1/1,#90>
}
BlockList(4/4) { ; }
BlockArray(4/4) { }
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tempo.trackscript'
            path.write_text(text, encoding='utf-8')
            parsed = parse_file(path)

        self.assertEqual(parsed.tempo_events, [(0, 120, 0), (16, 90, 0)])


if __name__ == '__main__':
    unittest.main()
