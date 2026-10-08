import struct
import unittest
from types import SimpleNamespace

from it_parser import (
    ITNote,
    ITOrder,
    ITSampleConversionFlag,
    ITSampleFlag,
    PatternEvent,
    decode_sample,
    it_decompress,
    note_termination,
    parse_envelope,
)


def compressed_block(fields):
    bits = 0
    bit_count = 0
    for value, width in fields:
        bits |= value << bit_count
        bit_count += width
    payload = bits.to_bytes((bit_count + 7) // 8, "little")
    return struct.pack("<H", len(payload)) + payload


class ITDecompressTests(unittest.TestCase):
    def test_it_volume_envelope_point_layout(self):
        raw = bytearray(82)
        raw[:6] = bytes((0x07, 2, 0, 1, 1, 1))
        raw[6:9] = bytes((64, 0, 0))
        raw[9:12] = bytes((0, 12, 0))

        envelope = parse_envelope(raw, 0)

        self.assertTrue(envelope.enabled)
        self.assertTrue(envelope.loop_enabled)
        self.assertTrue(envelope.sustain_enabled)
        self.assertEqual(envelope.points, [(0, 64), (12, 0)])

    def test_shared_it_vocabulary(self):
        self.assertEqual((ITNote.FADE, ITNote.CUT, ITNote.KEY_OFF), (253, 254, 255))
        self.assertEqual((ITOrder.SKIP, ITOrder.END), (254, 255))
        self.assertEqual(ITSampleFlag.DATA_PRESENT, 1)
        self.assertEqual(ITSampleConversionFlag.SIGNED, 1)
        self.assertIsNone(note_termination(253))
        self.assertEqual(note_termination(253, mptm=True), "fade")
        self.assertEqual(note_termination(254), "cut")
        self.assertEqual(note_termination(255), "off")
        self.assertIsNone(note_termination(252))

    def test_pattern_event_has_named_fields_and_tuple_compatibility(self):
        event = PatternEvent(60, 2, 32, 4, 0x12)

        self.assertEqual(event.note, 60)
        self.assertEqual(event.instrument, 2)
        self.assertEqual(tuple(event), (60, 2, 32, 4, 0x12))

    def test_default_width_escape(self):
        data = compressed_block([(0x100 | 5, 9), (1, 6)])

        self.assertEqual(it_decompress(data, 1, False, False), [1])

    def test_middle_width_escape(self):
        data = compressed_block([(0x100 | 6, 9), (62, 7), (1, 3)])

        self.assertEqual(it_decompress(data, 1, False, False), [1])

    def test_small_width_escape(self):
        data = compressed_block([
            (0x100 | 5, 9),
            (1 << 5, 6),
            (2, 3),
            (1, 3),
        ])

        self.assertEqual(it_decompress(data, 1, False, False), [1])

    def test_stereo_channels_are_decoded_separately(self):
        data = compressed_block([(1, 9), (1, 9)])
        data += compressed_block([(2, 9), (2, 9)])
        sample = SimpleNamespace(
            has_data=True,
            ptr=1,
            stereo=True,
            compressed=True,
            is16=False,
            cvt=0,
            length=2,
        )

        self.assertEqual(
            decode_sample(b"\0" + data, sample),
            [1, 2, 2, 4],
        )


if __name__ == "__main__":
    unittest.main()