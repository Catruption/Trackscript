import unittest

import numpy as np

from compare_renditions import align_audio, find_lag


class AudioAlignmentTests(unittest.TestCase):
    def test_positive_lag_means_trackscript_is_late_and_keeps_tails(self):
        it_audio = np.zeros(20, dtype=np.float32)
        ts_audio = np.zeros(25, dtype=np.float32)
        it_audio[5] = 1.0
        it_audio[19] = 0.25
        ts_audio[7] = 1.0
        ts_audio[24] = 0.5

        lag, corr, _, _ = find_lag(
            it_audio,
            ts_audio,
            max_lag_s=0.3,
            window_s=2.0,
            sr=10,
            step_ms=100,
        )
        aligned_it, aligned_ts, overlap = align_audio(it_audio, ts_audio, lag)

        self.assertEqual(lag, 2)
        self.assertAlmostEqual(corr, 1.0)
        self.assertEqual(int(np.argmax(aligned_it)), int(np.argmax(aligned_ts)))
        self.assertEqual(len(aligned_it), 25)
        self.assertEqual(aligned_it[21], 0.25)
        self.assertEqual(aligned_ts[24], 0.5)
        self.assertEqual((overlap.start, overlap.stop), (2, 22))

    def test_negative_lag_preserves_early_trackscript_audio(self):
        it_audio = np.zeros(20, dtype=np.float32)
        ts_audio = np.zeros(20, dtype=np.float32)
        it_audio[7] = 1.0
        ts_audio[5] = 1.0

        lag, _, _, _ = find_lag(
            it_audio,
            ts_audio,
            max_lag_s=0.3,
            window_s=2.0,
            sr=10,
            step_ms=100,
        )
        aligned_it, aligned_ts, _ = align_audio(it_audio, ts_audio, lag)

        self.assertEqual(lag, -2)
        self.assertEqual(int(np.argmax(aligned_it)), int(np.argmax(aligned_ts)))
        self.assertEqual(aligned_ts[7], 1.0)

    def test_lag_refines_below_the_coarse_grid(self):
        it_audio = np.zeros(1000, dtype=np.float32)
        ts_audio = np.zeros(1000, dtype=np.float32)
        it_audio[300] = 1.0
        ts_audio[303] = 1.0

        lag, corr, _, _ = find_lag(
            it_audio,
            ts_audio,
            max_lag_s=0.02,
            window_s=1.0,
            sr=1000,
            step_ms=10,
        )

        self.assertEqual(lag, 3)
        self.assertAlmostEqual(corr, 1.0)


if __name__ == "__main__":
    unittest.main()