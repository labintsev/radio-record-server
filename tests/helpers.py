"""Shared helpers for the ingest and dashboard tests."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterable

from radio_record.protocol import CODEC_PCM_S16LE, FrameHeader

RATE = 16000
FRAME_MS = 20


@asynccontextmanager
async def open_stream(
    port: int,
    device: str,
    sample_rate: int = RATE,
    channels: int = 1,
    host: str = "127.0.0.1",
) -> AsyncIterator[Callable[[int], Awaitable[None]]]:
    """Open one device stream and hand back a ``send(samples)`` callable."""
    _, writer = await asyncio.open_connection(host, port)
    state = {"seq": 0, "sample": 0}

    async def send(samples: int) -> None:
        remaining = samples
        while remaining > 0:
            count = min(sample_rate * FRAME_MS // 1000, remaining)
            payload = bytes(count * 2 * channels)
            header = FrameHeader(
                codec=CODEC_PCM_S16LE,
                device_id=device,
                seq=state["seq"],
                first_sample=state["sample"],
                payload_len=len(payload),
                sample_rate=sample_rate,
                channels=channels,
            )
            writer.write(header.pack() + payload)
            state["seq"] += 1
            state["sample"] += count
            remaining -= count
        await writer.drain()

    try:
        yield send
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - the server may already be gone
            pass


async def push_frames(
    port: int,
    device: str,
    pieces: Iterable[tuple[int, int]],
    host: str = "127.0.0.1",
    sample_rate: int = RATE,
    channels: int = 1,
) -> None:
    """Send ``(first_sample, sample_count)`` pieces over one connection."""
    _, writer = await asyncio.open_connection(host, port)
    seq = 0
    try:
        for first_sample, samples in pieces:
            position, remaining = first_sample, samples
            while remaining > 0:
                count = min(sample_rate * FRAME_MS // 1000, remaining)
                payload = bytes(count * 2 * channels)
                header = FrameHeader(
                    codec=CODEC_PCM_S16LE,
                    device_id=device,
                    seq=seq,
                    first_sample=position,
                    payload_len=len(payload),
                    sample_rate=sample_rate,
                    channels=channels,
                )
                writer.write(header.pack() + payload)
                seq += 1
                position += count
                remaining -= count
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def wait_for(predicate: Callable[[], object], timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


async def wait_until(
    check: Callable[[], Awaitable[object]], timeout: float = 10.0, interval: float = 0.05
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def at_least(directory: Path, pattern: str, count: int) -> list[Path] | None:
    found = sorted(directory.glob(pattern))
    return found if len(found) >= count else None


def _dechunk(data: bytes) -> bytes:
    out = bytearray()
    position = 0
    while True:
        end = data.find(b"\r\n", position)
        if end < 0:
            break
        size = int(data[position:end].split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        start = end + 2
        out += data[start : start + size]
        position = start + size + 2
    return bytes(out)


async def http_get(
    port: int, path: str, host: str = "127.0.0.1", timeout: float = 10.0
) -> tuple[int, dict[str, str], bytes]:
    """Minimal HTTP/1.1 GET, so the tests need no extra client dependency."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Accept: */*\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode()] = value.strip().decode()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    return status, headers, body


async def http_json(port: int, path: str) -> tuple[int, dict]:
    status, _, body = await http_get(port, path)
    return status, json.loads(body or b"{}")


async def sse_collect(
    port: int, path: str, timeout: float = 5.0, settle: float = 0.6
) -> str:
    """Read the event stream until it goes quiet, then return what arrived."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Accept: text/event-stream\r\n\r\n".encode()
        )
        await writer.drain()
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), settle)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buffer += chunk
        return buffer.decode("utf-8", "replace")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
