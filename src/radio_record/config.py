"""Server settings, populated from CLI flags or explicit values."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 9100
    output_dir: Path = Path("recordings")

    sample_rate: int = 16000
    channels: int = 1

    # Rotation is driven by sample count, not by wall clock.
    segment_seconds: float = 900.0
    segment_max_bytes: int = 1 << 30

    # Frames buffered per device before the reader stops draining the socket,
    # which is how TCP backpressure reaches the ESP32.
    queue_frames: int = 512

    pad_gaps: bool = True
    max_gap_seconds: float = 5.0

    # "flac", "opus" or None. Requires the optional PyAV dependency.
    transcode: str | None = None
    keep_wav: bool = False

    idle_timeout: float = 30.0

    # Web dashboard. Bound to loopback by default: it exposes logs and device
    # ids, so opening it to the network is an explicit decision.
    dashboard: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_open: bool = False
    log_buffer: int = 2000
