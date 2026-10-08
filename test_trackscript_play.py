import unittest

import numpy as np

from trackscript_play import pan_gains, sinc_interpolate


class SampleInterpolationTests(unittest.TestCase):
    def test_linear_pan_matches_it_compatible_balance(self):
        left, right = pan_gains(np.array([1.0]), np.array([0.0]), 'linear')

        np.testing.assert_allclose(left, [0.5])
        np.testing.assert_allclose(right, [0.5])

    def test_eight_tap_kernel_preserves_high_frequency_upsampling(self):
        source_positions = np.arange(512, dtype=np.float64)
        source = np.sin(2 * np.pi * 0.4 * source_positions).astype(np.float32)
        positions = np.arange(12.0, 500.0, 0.5)
        steps = np.full(len(positions), 0.5)
        expected = np.sin(2 * np.pi * 0.4 * positions)

        actual = sinc_interpolate(source, positions, steps)[:, 0]
        linear = np.interp(positions, source_positions, source)

        sinc_error = np.sqrt(np.mean((actual[8:-8] - expected[8:-8]) ** 2))
        linear_error = np.sqrt(np.mean((linear[8:-8] - expected[8:-8]) ** 2))
        self.assertLess(sinc_error, linear_error * 0.5)

    def test_kernel_preserves_constant_signal_at_loop_boundary(self):
        source = np.ones((32, 1), dtype=np.float32)
        positions = np.array([30.25, 30.75, 31.25, 31.75])
        raw_positions = np.array([30.25, 30.75, 32.25, 32.75])

        actual = sinc_interpolate(
            source,
            positions,
            np.ones(4),
            loop=(24, 32),
            raw_positions=raw_positions,
        )

        np.testing.assert_allclose(actual, 1.0, atol=1e-6)


if __name__ == "__main__":
    unittest.main()