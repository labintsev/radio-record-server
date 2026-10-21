"""asyncio TCP ingest server."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from radio_record.config import Settings
from radio_record.protocol import HEADER_SIZE, FrameHeader, ProtocolError
from radio_record.recorder import DeviceRecorder

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(device_id: str) -> str:
    """Device ids come from the wire and end up in paths, so sanitise them."""
    cleaned = _UNSAFE.sub("_", device_id).strip("._")
    return (cleaned or "device")[:64]


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
        self._sessions: dict[str, asyncio.StreamWriter] = {}
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("server is not started")
        sockets = self._server.sockets or []
        return sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.settings.host, self.settings.port
        )
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
        for writer in list(self._sessions.values()):
            writer.close()
        if self._handlers:
            await asyncio.gather(*list(self._handlers), return_exceptions=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)

        peer = writer.get_extra_info("peername")
        queue: asyncio.Queue[tuple[FrameHeader, bytes] | None] = asyncio.Queue(
            self.settings.queue_frames
        )
        device: str | None = None
        recorder_task: asyncio.Task[None] | None = None

        try:
            header, payload = await self._read_frame(reader)
            self._check_format(header)
            device = safe_name(header.device_id)
            await self._preempt(device, writer)

            recorder = DeviceRecorder(device, self.settings, self._on_finalize)
            recorder_task = asyncio.create_task(recorder.run(queue), name=f"recorder:{device}")
            log.info("%s: connected from %s", device, peer)
            await queue.put((header, payload))

            while True:
                header, payload = await self._read_frame(reader)
                if safe_name(header.device_id) != device:
                    raise ProtocolError("device_id changed mid-stream")
                self._check_format(header)
                await queue.put((header, payload))
        except asyncio.IncompleteReadError:
            log.info("%s: disconnected (%s)", device or peer, "stream ended")
        except asyncio.TimeoutError:
            log.warning("%s: idle timeout, closing", device or peer)
        except ProtocolError as exc:
            log.warning("%s: protocol error: %s", device or peer, exc)
        except ConnectionError:
            log.info("%s: connection lost", device or peer)
        finally:
            try:
                if device is not None and self._sessions.get(device) is writer:
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
        if previous is not None and previous is not writer:
            log.warning("%s: reconnected, dropping previous session", device)
            previous.close()
        self._sessions[device] = writer
