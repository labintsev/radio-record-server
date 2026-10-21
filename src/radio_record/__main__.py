"""Command line entry point: ``python -m radio_record``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import webbrowser
from pathlib import Path

from radio_record.config import Settings
from radio_record.dashboard import Dashboard, DashboardError, start_dashboard
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

    gui = parser.add_argument_group("dashboard")
    gui.add_argument("--no-dashboard", action="store_true", help="run headless")
    gui.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="bind address for the dashboard (default: loopback only)",
    )
    gui.add_argument("--dashboard-port", type=int, default=8080)
    gui.add_argument(
        "--open-dashboard", action="store_true", help="open the dashboard in a browser on start"
    )
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
        dashboard=not args.no_dashboard,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
        dashboard_open=args.open_dashboard,
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


def _install_loop_exception_filter() -> None:
    """Downgrade peer disconnects that asyncio reports from its transports.

    On Windows the proactor transport calls ``shutdown()`` while closing a
    socket whose peer already reset it, and the loop logs the resulting
    ``ConnectionResetError`` as an unhandled error. Browser tabs closing an
    event stream do this routinely, so it would drown real errors in the
    dashboard.
    """

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        error = context.get("exception")
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log.debug("peer closed the socket: %s", error)
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(handler)


async def serve(settings: Settings) -> None:
    _install_loop_exception_filter()
    server = RadioServer(settings, on_finalize=make_finalizer(settings))
    await server.start()
    dashboard: Dashboard | None = None
    ingest: asyncio.Task[None] | None = None
    try:
        if settings.dashboard:
            dashboard = await start_dashboard(server, settings)
            if settings.dashboard_open:
                threading.Thread(target=webbrowser.open, args=(dashboard.url,), daemon=True).start()

        ingest = asyncio.create_task(server.serve_forever(), name="ingest")
        if dashboard is None:
            await ingest
        else:
            done, _ = await asyncio.wait(
                {ingest, dashboard.task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                # uvicorn re-raises a captured SIGINT when it shuts down.
                if error is not None and not isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
    finally:
        if ingest is not None and not ingest.done():
            ingest.cancel()
            await asyncio.gather(ingest, return_exceptions=True)
        if dashboard is not None:
            await dashboard.stop()
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
    except (OSError, DashboardError) as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
