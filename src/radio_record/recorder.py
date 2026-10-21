"""Per-device segmenting recorder.

Frames arrive through a bounded queue owned by the connection handler. Writing
happens synchronously in the consumer task: payloads go straight into the OS
page cache, and if disk throughput cannot keep up the queue fills, the reader
stops draining the socket and TCP backpressure throttles the ESP32 instead of
dropping audio.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from radio_record.config import Settings
from radio_record.protocol import FrameHeader

log = logging.getLogger(__name__)

WAV_HEADER_SIZE = 44
_MAX_WAV_DATA_BYTES = 0xFFFFFFFF - 36  # RIFF chunk size field is 32-bit
_SILENCE_CHUNK = 64 * 1024


def wav_header(data_bytes: int, sample_rate: int, channels: int, bits: int = 16) -> bytes:
    block_align = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits,
        b"data",
        data_bytes,
    )


class WavWriter:
    """Appends PCM to a WAV file and patches the RIFF header on close."""

    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.data_bytes = 0
        self._fh = path.open("wb")
        self._fh.write(b"\0" * WAV_HEADER_SIZE)

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self.data_bytes += len(data)

    @property
    def duration(self) -> float:
        return self.data_bytes / (self.sample_rate * self.channels * 2)

    def close(self) -> None:
        if self._fh.closed:
            return
        self._fh.seek(0)
        self._fh.write(wav_header(self.data_bytes, self.sample_rate, self.channels))
        self._fh.close()


@dataclass(slots=True)
class SegmentStats:
    frames: int = 0
    gap_samples: int = 0


class DeviceRecorder:
    """Turns the frame stream of one device into rotated WAV segments."""

    def __init__(
        self,
        device_id: str,
        settings: Settings,
        on_finalize: Callable[[Path], Awaitable[None]] | None = None,
    ) -> None:
        self.device_id = device_id
        self.settings = settings
        self._on_finalize = on_finalize
        self._writer: WavWriter | None = None
        self._expected_sample: int | None = None
        self._segment_start = datetime.now(timezone.utc)
        self._stats = SegmentStats()
        self._finalized: list[Path] = []
        self._background: set[asyncio.Task[None]] = set()

    async def run(self, queue: "asyncio.Queue[tuple[FrameHeader, bytes] | None]") -> None:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                header, payload = item
                self._write(header, payload)
                await self._drain_finalized()
        finally:
            try:
                self._finalize_segment()
            finally:
                await self._drain_finalized()
                await self._wait_background()

    def _write(self, header: FrameHeader, payload: bytes) -> None:
        if self._writer is None:
            self._open_segment()
        elif self._needs_rotation():
            self._finalize_segment()
            self._open_segment()

        assert self._writer is not None

        if self._expected_sample is not None:
            gap = header.first_sample - self._expected_sample
            if gap > 0:
                self._pad(gap)
            elif gap < 0:
                # Device rebooted and restarted its sample counter.
                log.warning("%s: sample counter went back by %d", self.device_id, -gap)
                self._expected_sample = header.first_sample

        block = self.settings.channels * 2
        usable = len(payload) - len(payload) % block
        if usable <= 0:
            return
        self._writer.write(payload[:usable])
        self._stats.frames += 1
        self._expected_sample = header.first_sample + usable // block

    def _pad(self, gap_samples: int) -> None:
        if not self.settings.pad_gaps:
            self._expected_sample = (self._expected_sample or 0) + gap_samples
            self._stats.gap_samples += gap_samples
            return
        limit = int(self.settings.max_gap_seconds * self.settings.sample_rate)
        pad = min(gap_samples, limit)
        self._stats.gap_samples += gap_samples
        self._expected_sample = (self._expected_sample or 0) + gap_samples
        if pad <= 0:
            return
        assert self._writer is not None
        remaining = pad * self.settings.channels * 2
        chunk = bytes(min(remaining, _SILENCE_CHUNK))
        while remaining > 0:
            self._writer.write(chunk[:remaining])
            remaining -= len(chunk)

    def _needs_rotation(self) -> bool:
        assert self._writer is not None
        limit = min(self.settings.segment_max_bytes, _MAX_WAV_DATA_BYTES)
        return (
            self._writer.duration >= self.settings.segment_seconds
            or self._writer.data_bytes >= limit
        )

    def _open_segment(self) -> None:
        self._segment_start = datetime.now(timezone.utc)
        self._stats = SegmentStats()
        path = self._next_path()
        self._writer = WavWriter(path, self.settings.sample_rate, self.settings.channels)

    def _next_path(self) -> Path:
        directory = self.settings.output_dir / self.device_id
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{self.device_id}-{self._segment_start:%Y%m%dT%H%M%SZ}"
        # Rotation can produce several segments within the same second, and a
        # reconnect can start a new one, so the finalised names must be taken
        # into account too - otherwise the next segment silently overwrites
        # the previous one on rename.
        taken = (".part", ".wav", ".flac", ".opus")
        index = 1
        while True:
            base = stem if index == 1 else f"{stem}-{index}"
            if not any((directory / f"{base}{ext}").exists() for ext in taken):
                return directory / f"{base}.part"
            index += 1

    def _finalize_segment(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        writer.close()
        target = writer.path.with_suffix(".wav")
        writer.path.replace(target)
        self._finalized.append(target)
        log.info(
            "%s: stored %s (%.2fs, %d frames, %d gap samples)",
            self.device_id,
            target.name,
            writer.duration,
            self._stats.frames,
            self._stats.gap_samples,
        )

    async def _drain_finalized(self) -> None:
        while self._finalized:
            path = self._finalized.pop(0)
            if self._on_finalize is None:
                continue
            # Post-processing (e.g. encoding a 15 minute segment to FLAC) can
            # take seconds. Running it inline would stop draining the frame
            # queue and throttle the ESP32 through TCP backpressure, so it runs
            # as a separate task instead.
            task = asyncio.create_task(self._on_finalize(path), name=f"finalize:{path.name}")
            self._background.add(task)
            task.add_done_callback(self._post_processing_done)

    def _post_processing_done(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("%s: post-processing failed: %s", self.device_id, error)

    async def _wait_background(self) -> None:
        while self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
