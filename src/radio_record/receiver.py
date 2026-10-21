"""asyncio TCP ingest server."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from radio_record.config import Settings
from radio_record.protocol import HEADER_SIZE, FrameHeader, ProtocolError
from radio_record.recorder import DeviceRecorder

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# A stream that has stopped delivering frames is shown as stalled rather than
# live, so a wedged device is visible in the dashboard.
STALL_SECONDS = 3.0


def safe_name(device_id: str) -> str:
    """Device ids come from the wire and end up in paths, so sanitise them."""
    cleaned = _UNSAFE.sub("_", device_id).strip("._")
    return (cleaned or "device")[:64]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class DeviceSession:
    """Live bookkeeping for one connected device."""

    device_id: str
    peer: str
    writer: asyncio.StreamWriter
    queue: "asyncio.Queue[tuple[FrameHeader, bytes] | None]"
    recorder: DeviceRecorder
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _monotonic_start: float = field(default_factory=time.monotonic)
    frames: int = 0
    total_bytes: int = 0
    samples: int = 0
    last_frame_at: float | None = None
    rate_bps: float = 0.0
    _rate_mark: float = 0.0
    _rate_bytes: int = 0

    def touch(self, header: FrameHeader, payload_len: int) -> None:
        now = time.monotonic()
        self.frames += 1
        self.total_bytes += payload_len
        block = self.recorder.settings.channels * 2
        self.samples += payload_len // block
        self.last_frame_at = now
        if self._rate_mark == 0.0:
            self._rate_mark, self._rate_bytes = now, self.total_bytes
            return
        span = now - self._rate_mark
        if span >= 1.0:
            self.rate_bps = (self.total_bytes - self._rate_bytes) / span
            self._rate_mark, self._rate_bytes = now, self.total_bytes

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        idle = None if self.last_frame_at is None else max(0.0, now - self.last_frame_at)
        stalled = idle is not None and idle > STALL_SECONDS
        return {
            "device_id": self.device_id,
            "peer": self.peer,
            "state": "stalled" if stalled else "streaming",
            "connected_at": _iso(self.connected_at),
            "uptime_seconds": round(now - self._monotonic_start, 1),
            "idle_seconds": None if idle is None else round(idle, 1),
            "frames": self.frames,
            "samples": self.samples,
            "total_bytes": self.total_bytes,
            "rate_bps": 0.0 if stalled else round(self.rate_bps, 1),
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            **self.recorder.live_state(),
        }


class RadioServer:
    """Accepts one frame stream per device and writes rotated WAV segments."""

    def __init__(
        self,
        settings: Settings,
        on_finalize: Callable[[Path], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._on_finalize = on_finalize
        self._server: asyncio.Server | None = None
        self._sessions: dict[str, DeviceSession] = {}
        self._recent: deque[dict[str, object]] = deque(maxlen=20)
        self._handlers: set[asyncio.Task[None]] = set()
        self.started_at: datetime | None = None
        self.processed_frames = 0
        self.processed_bytes = 0

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not started")
        sockets = self._server.sockets or []
        return sockets[0].getsockname()[1]

    @property
    def sessions(self) -> dict[str, DeviceSession]:
        return self._sessions

    def snapshot(self) -> dict[str, object]:
        """Everything the dashboard renders, read from the event loop."""
        return {
            "started_at": _iso(self.started_at) if self.started_at else None,
            "listen": f"{self.settings.host}:{self._server_port()}",
            "output_dir": str(self.settings.output_dir),
            "sample_rate": self.settings.sample_rate,
            "channels": self.settings.channels,
            "segment_seconds": self.settings.segment_seconds,
            "transcode": self.settings.transcode,
            "processed_frames": self.processed_frames,
            "processed_bytes": self.processed_bytes,
            "devices": [session.snapshot() for session in self._sessions.values()],
            "recent": list(self._recent),
        }

    def _server_port(self) -> int | None:
        if self._server is None:
            return None
        sockets = self._server.sockets or []
        return sockets[0].getsockname()[1] if sockets else None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.settings.host, self.settings.port
        )
        self.started_at = datetime.now(timezone.utc)
        log.info("listening on %s:%d", self.settings.host, self.port)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("server is not started")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        for session in list(self._sessions.values()):
            session.writer.close()
        if self._handlers:
            await asyncio.gather(*list(self._handlers), return_exceptions=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)

        peer_info = writer.get_extra_info("peername")
        peer = f"{peer_info[0]}:{peer_info[1]}" if isinstance(peer_info, tuple) else "unknown"
        queue: asyncio.Queue[tuple[FrameHeader, bytes] | None] = asyncio.Queue(
            self.settings.queue_frames
        )
        device: str | None = None
        session: DeviceSession | None = None
        recorder_task: asyncio.Task[None] | None = None
        reason = "closed"

        try:
            header, payload = await self._read_frame(reader)
            self._check_format(header)
            device = safe_name(header.device_id)
            await self._preempt(device, writer)

            recorder = DeviceRecorder(device, self.settings, self._on_finalize)
            session = DeviceSession(
                device_id=device,
                peer=peer,
                writer=writer,
                queue=queue,
                recorder=recorder,
            )
            self._sessions[device] = session
            recorder_task = asyncio.create_task(recorder.run(queue), name=f"recorder:{device}")
            log.info("%s: connected from %s", device, peer)
            self._account(session, header, len(payload))
            await queue.put((header, payload))

            while True:
                header, payload = await self._read_frame(reader)
                if safe_name(header.device_id) != device:
                    raise ProtocolError("device_id changed mid-stream")
                self._check_format(header)
                self._account(session, header, len(payload))
                await queue.put((header, payload))
        except asyncio.IncompleteReadError:
            reason = "stream ended"
            log.info("%s: disconnected (%s)", device or peer, reason)
        except asyncio.TimeoutError:
            reason = "idle timeout"
            log.warning("%s: idle timeout, closing", device or peer)
        except ProtocolError as exc:
            reason = f"protocol error: {exc}"
            log.warning("%s: protocol error: %s", device or peer, exc)
        except ConnectionError:
            reason = "connection lost"
            log.info("%s: connection lost", device or peer)
        finally:
            try:
                if device is not None and self._sessions.get(device) is session:
                    del self._sessions[device]
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - transport may already be gone
                    pass

                if recorder_task is not None:
                    if not recorder_task.done():
                        try:
                            queue.put_nowait(None)
                        except asyncio.QueueFull:
                            await queue.put(None)
                    results = await asyncio.gather(recorder_task, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            log.error("%s: recorder failed: %s", device, result)

                # Recorded after the recorder finished, so the summary carries
                # the final segment counters rather than a partial snapshot.
                if session is not None:
                    self._recent.append(
                        {
                            **session.snapshot(),
                            "state": "closed",
                            "reason": reason,
                            "closed_at": _iso(datetime.now(timezone.utc)),
                        }
                    )
            finally:
                if task is not None:
                    self._handlers.discard(task)

    async def _read_frame(self, reader: asyncio.StreamReader) -> tuple[FrameHeader, bytes]:
        raw = await asyncio.wait_for(
            reader.readexactly(HEADER_SIZE), self.settings.idle_timeout
        )
        header = FrameHeader.unpack(raw)
        payload = await asyncio.wait_for(
            reader.readexactly(header.payload_len), self.settings.idle_timeout
        )
        return header, payload

    def _account(self, session: DeviceSession, header: FrameHeader, payload_len: int) -> None:
        session.touch(header, payload_len)
        self.processed_frames += 1
        self.processed_bytes += payload_len

    def _check_format(self, header: FrameHeader) -> None:
        if header.sample_rate != self.settings.sample_rate:
            raise ProtocolError(
                f"sample_rate {header.sample_rate} does not match server "
                f"{self.settings.sample_rate}"
            )
        if header.channels != self.settings.channels:
            raise ProtocolError(
                f"channels {header.channels} does not match server {self.settings.channels}"
            )

    async def _preempt(self, device: str, writer: asyncio.StreamWriter) -> None:
        previous = self._sessions.get(device)
        if previous is not None and previous.writer is not writer:
            log.warning("%s: reconnected, dropping previous session", device)
            previous.writer.close()
        self._sessions.pop(device, None)
