"""Command line entry point: ``python -m radio_record``."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from radio_record.config import Settings
from radio_record.receiver import RadioServer
from radio_record.transcode import transcode

log = logging.getLogger("radio_record")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radio-record",
        description="Receive PCM streams from ESP32 radio devices and store them as WAV segments.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--output", type=Path, default=Path("recordings"))
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--segment-minutes", type=float, default=15.0)
    parser.add_argument("--max-segment-mb", type=int, default=1024)
    parser.add_argument("--transcode", choices=("flac", "opus"), default=None)
    parser.add_argument("--keep-wav", action="store_true")
    parser.add_argument("--no-pad-gaps", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        host=args.host,
        port=args.port,
        output_dir=args.output,
        sample_rate=args.sample_rate,
        channels=args.channels,
        segment_seconds=args.segment_minutes * 60.0,
        segment_max_bytes=args.max_segment_mb * 1024 * 1024,
        pad_gaps=not args.no_pad_gaps,
        transcode=args.transcode,
        keep_wav=args.keep_wav,
    )


def make_finalizer(settings: Settings):
    async def finalize(path: Path) -> None:
        if settings.transcode is None:
            return
        result = await asyncio.to_thread(transcode, path, settings.transcode)
        log.info("transcoded %s -> %s", path.name, result.name)
        if not settings.keep_wav:
            path.unlink(missing_ok=True)

    return finalize


async def serve(settings: Settings) -> None:
    server = RadioServer(settings, on_finalize=make_finalizer(settings))
    await server.start()
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = settings_from_args(args)
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
