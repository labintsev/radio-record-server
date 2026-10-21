import unittest

from radio_record.protocol import (
    HEADER_SIZE,
    MAGIC,
    FrameHeader,
    ProtocolError,
    samples_in,
)


class FrameHeaderTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        header = FrameHeader(
            codec=1, device_id="esp32-a", seq=42, first_sample=123456, payload_len=640
        )
        parsed = FrameHeader.unpack(header.pack())
        self.assertEqual(parsed, header)

    def test_header_is_fixed_size(self) -> None:
        header = FrameHeader(codec=1, device_id="x", seq=0, first_sample=0, payload_len=2)
        self.assertEqual(len(header.pack()), HEADER_SIZE)
        self.assertEqual(HEADER_SIZE, 43)

    def test_format_fields_roundtrip(self) -> None:
        header = FrameHeader(
            codec=1,
            device_id="d",
            seq=1,
            first_sample=2,
            payload_len=4,
            sample_rate=48000,
            channels=2,
        )
        self.assertEqual(FrameHeader.unpack(header.pack()), header)

    def test_out_of_range_sample_rate_rejected(self) -> None:
        raw = bytearray(FrameHeader(1, "d", 0, 0, 2).pack())
        raw[38:42] = (1).to_bytes(4, "little")
        with self.assertRaises(ProtocolError):
            FrameHeader.unpack(bytes(raw))

    def test_out_of_range_channels_rejected(self) -> None:
        raw = bytearray(FrameHeader(1, "d", 0, 0, 2).pack())
        raw[42] = 3
        with self.assertRaises(ProtocolError):
            FrameHeader.unpack(bytes(raw))

    def test_bad_magic_rejected(self) -> None:
        raw = bytearray(FrameHeader(1, "d", 0, 0, 2).pack())
        raw[0:4] = b"XXXX"
        with self.assertRaises(ProtocolError):
            FrameHeader.unpack(bytes(raw))

    def test_unsupported_codec_rejected(self) -> None:
        raw = bytearray(FrameHeader(1, "d", 0, 0, 2).pack())
        raw[5] = 9
        with self.assertRaises(ProtocolError):
            FrameHeader.unpack(bytes(raw))

    def test_oversized_payload_rejected(self) -> None:
        raw = bytearray(FrameHeader(1, "d", 0, 0, 2).pack())
        raw[34:38] = (70000).to_bytes(4, "little")
        with self.assertRaises(ProtocolError):
            FrameHeader.unpack(bytes(raw))

    def test_device_id_truncated_to_field_width(self) -> None:
        header = FrameHeader(1, "device-with-a-very-long-name", 0, 0, 2)
        raw = header.pack()
        self.assertEqual(raw[6:22], b"device-with-a-ve")
        self.assertTrue(raw.startswith(MAGIC))

    def test_samples_in(self) -> None:
        self.assertEqual(samples_in(640, 1), 320)
        self.assertEqual(samples_in(640, 2), 160)
        self.assertEqual(samples_in(641, 1), 320)


if __name__ == "__main__":
    unittest.main()
