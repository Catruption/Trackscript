import math
import unittest

from trackscript_timing import AutomationLane, TempoMap, parse_tempo


class TempoMapTests(unittest.TestCase):
    def test_parse_tempo_positions_use_configured_subdivision(self):
        events = parse_tempo("Tempo { <§3/2,#90> }", subdivisions=8)

        self.assertEqual(events, [(17.0, 90.0, 0.0)])

    def test_instant_tempo_change(self):
        events = [(0.0, 120.0, 0.0), (4.0, 60.0, 0.0)]
        tempo = TempoMap(120.0, events, subdivisions=4)

        self.assertAlmostEqual(tempo.time_at_row(4), 0.5)
        self.assertAlmostEqual(tempo.time_at_row(8), 1.5)

    def test_linear_bpm_ramp_integrates_elapsed_time(self):
        events = [(0.0, 120.0, 0.0), (8.0, 60.0, 2.0)]
        tempo = TempoMap(120.0, events, subdivisions=4)
        ramp_seconds = 60.0 / (4 * -7.5) * math.log(60.0 / 120.0)

        self.assertAlmostEqual(tempo.time_at_row(8), 1.0)
        self.assertAlmostEqual(tempo.time_at_row(16), 1.0 + ramp_seconds)
        self.assertAlmostEqual(tempo.time_at_row(20), 2.0 + ramp_seconds)

    def test_automation_interpolates_over_its_row_duration(self):
        tempo = TempoMap(120.0, [], subdivisions=4)
        lane = AutomationLane(0.0, 0.0, [(4.0, 4.0, 100.0)], tempo)

        self.assertAlmostEqual(lane.value_at(0.25), 0.0)
        self.assertAlmostEqual(lane.value_at(0.75), 50.0)
        self.assertAlmostEqual(lane.value_at(1.0), 100.0)

    def test_new_automation_interrupts_an_active_ramp(self):
        tempo = TempoMap(120.0, [], subdivisions=4)
        lane = AutomationLane(
            0.0,
            0.0,
            [(0.0, 8.0, 80.0), (4.0, 0.0, 20.0)],
            tempo,
        )

        self.assertAlmostEqual(lane.value_at(0.25), 20.0)
        self.assertAlmostEqual(lane.value_at(0.5), 20.0)


if __name__ == "__main__":
    unittest.main()