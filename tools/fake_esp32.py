"""Simulated ESP32 client used to exercise the ingest server without hardware.

    python tools/fake_esp32.py --seconds 3 --speed 8
    python tools/fake_esp32.py --seconds 5 --gap-at 2.0 --gap-seconds 0.5
    python tools/fake_esp32.py --host http://radio.mldl.ru --seconds 5 --speed 5
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from array import array
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from radio_record.protocol import CODEC_PCM_S16LE, FrameHeader  # noqa: E402


def pcm_chunk(
    start: int, count: int, rate: int, channels: int, freq: float, amplitude: float
) -> bytes:
    step = 2.0 * math.pi * freq / rate
    mono = array("h", (int(amplitude * 32767 * math.sin(step * (start + i))) for i in range(count)))
    if channels == 1:
        return mono.tobytes()
    interleaved = array("h", bytes(len(mono) * channels * 2))
    for i, sample in enumerate(mono):
        for channel in range(channels):
            interleaved[i * channels + channel] = sample
    return interleaved.tobytes()


def build_frames(args: argparse.Namespace):
    """Yields (first_sample, payload) pairs, honouring the simulated gap."""
    frame_samples = args.rate * args.frame_ms // 1000
    total = int(args.rate * args.seconds)
    gap_at = None if args.gap_at is None else int(args.gap_at * args.rate)
    offset = 0
    position = 0
    while position < total:
        if gap_at is not None and position >= gap_at and offset == 0:
            offset = int(args.gap_seconds * args.rate)
        count = min(frame_samples, total - position)
        yield position + offset, pcm_chunk(
            position, count, args.rate, args.channels, args.freq, args.amplitude
        )
        position += count


async def run(args: argparse.Namespace) -> None:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(args.host, args.port), args.connect_timeout
    )
    try:
        seq = 0
        for first_sample, payload in build_frames(args):
            header = FrameHeader(
                codec=CODEC_PCM_S16LE,
                device_id=args.device,
                seq=seq,
                first_sample=first_sample,
                payload_len=len(payload),
                sample_rate=args.rate,
                channels=args.channels,
            )
            writer.write(header.pack() + payload)
            await writer.drain()
            seq += 1
            await asyncio.sleep(args.frame_ms / 1000.0 / args.speed)
    finally:
        writer.close()
        await writer.wait_closed()


def parse_target(raw: str, default_port: int) -> tuple[str, int]:
    """Accepts ``host``, ``host:port`` and URLs like ``http://host:port/``."""
    text = raw.strip()
    if text.startswith("[") or "://" in text or "/" in text:
        parts = urlsplit(text if "://" in text else f"//{text}")
        if not parts.hostname:
            raise ValueError(f"cannot parse host from {raw!r}")
        return parts.hostname, parts.port or default_port
    if text.count(":") == 1 and not text.startswith("["):
        host, _, port = text.partition(":")
        if port.isdigit():
            return host, int(port)
    return text, default_port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push a fake ESP32 stream to the server")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="host or URL, e.g. 10.0.0.5 or http://radio.example.com",
    )
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--device", default="esp32-sim")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--freq", type=float, default=440.0)
    parser.add_argument("--amplitude", type=float, default=0.25)
    parser.add_argument("--speed", type=float, default=1.0, help="playback multiplier")
    parser.add_argument("--gap-at", type=float, default=None)
    parser.add_argument("--gap-seconds", type=float, default=0.5)
    parser.add_argument(
        "--connect-timeout", type=float, default=10.0, help="seconds to wait for the TCP connect"
    )
    args = parser.parse_args(argv)
    try:
        args.host, args.port = parse_target(args.host, args.port)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        asyncio.run(run(args))
    except TimeoutError:
        print(
            f"connect to {args.host}:{args.port} timed out after {args.connect_timeout:g}s",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"stream to {args.host}:{args.port} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
