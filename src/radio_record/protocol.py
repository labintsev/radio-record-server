"""Wire protocol shared by the ESP32 firmware and the server.

Frame layout (little endian, 43 bytes header + payload)::

    magic        4s   b"RDIO"
    version      B    protocol version, currently 1
    codec        B    1 = PCM signed 16-bit little endian
    device_id    16s  ASCII, NUL padded
    seq          I    per-connection frame counter, wraps at 2**32
    first_sample Q    absolute index of the first sample in this frame
    payload_len  I    payload size in bytes, 1..MAX_PAYLOAD
    sample_rate  I    samples per second per channel
    channels     B    1 = mono, 2 = stereo

The header is little endian on purpose: ESP32 is little endian, so the firmware
can fill a packed struct and write it out without byte swapping. Python's
``struct`` handles the conversion for the server on any platform.

``first_sample`` is what makes the stream self-describing: the server derives
segment boundaries, duration and gaps from sample counts instead of wall clock,
so a drifting ESP32 timer cannot corrupt the recording timeline.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"RDIO"
PROTOCOL_VERSION = 1

CODEC_PCM_S16LE = 1

MAX_DEVICE_ID = 16
MAX_PAYLOAD = 64 * 1024
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
MAX_CHANNELS = 2

_HEADER = struct.Struct("<4sBB16sIQIIB")
HEADER_SIZE = _HEADER.size


class ProtocolError(ValueError):
    """Raised when a frame cannot be parsed or is not supported."""


@dataclass(frozen=True, slots=True)
class FrameHeader:
    codec: int
    device_id: str
    seq: int
    first_sample: int
    payload_len: int
    version: int = PROTOCOL_VERSION
    sample_rate: int = 16000
    channels: int = 1

    def pack(self) -> bytes:
        device = self.device_id.encode("ascii")[:MAX_DEVICE_ID]
        return _HEADER.pack(
            MAGIC,
            self.version,
            self.codec,
            device.ljust(MAX_DEVICE_ID, b"\0"),
            self.seq & 0xFFFFFFFF,
            self.first_sample,
            self.payload_len,
            self.sample_rate,
            self.channels,
        )

    @classmethod
    def unpack(cls, raw: bytes) -> "FrameHeader":
        if len(raw) != HEADER_SIZE:
            raise ProtocolError(f"header must be {HEADER_SIZE} bytes, got {len(raw)}")
        (
            magic,
            version,
            codec,
            raw_device,
            seq,
            first_sample,
            payload_len,
            sample_rate,
            channels,
        ) = _HEADER.unpack(raw)
        if magic != MAGIC:
            raise ProtocolError("bad magic")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version {version}")
        if codec != CODEC_PCM_S16LE:
            raise ProtocolError(f"unsupported codec {codec}")
        if not 0 < payload_len <= MAX_PAYLOAD:
            raise ProtocolError(f"payload_len {payload_len} out of range")
        if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise ProtocolError(f"sample_rate {sample_rate} out of range")
        if not 0 < channels <= MAX_CHANNELS:
            raise ProtocolError(f"channels {channels} out of range")
        device_id = raw_device.rstrip(b"\0").decode("ascii", "replace")
        if not device_id:
            raise ProtocolError("empty device_id")
        return cls(
            codec=codec,
            device_id=device_id,
            seq=seq,
            first_sample=first_sample,
            payload_len=payload_len,
            version=version,
            sample_rate=sample_rate,
            channels=channels,
        )


def samples_in(payload_len: int, channels: int) -> int:
    """Number of whole samples (per channel) contained in a PCM payload."""
    return payload_len // (channels * 2)
