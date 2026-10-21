"""Optional post-processing of finished WAV segments via PyAV."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_TARGETS: dict[str, tuple[str, str | None, int | None]] = {
    "flac": (".flac", "flac", None),
    "opus": (".opus", "libopus", 48000),
}


def transcode(src: Path, target: str) -> Path:
    """Re-encode a finished WAV file into ``target`` ("flac" or "opus")."""
    try:
        import av
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise RuntimeError(
            "PyAV is required for transcoding, install with "
            "`pip install radio-record-server[transcode]`"
        ) from exc

    try:
        suffix, codec, forced_rate = _TARGETS[target]
    except KeyError:
        raise ValueError(f"unsupported transcode target {target!r}") from None

    dst = src.with_suffix(suffix)
    with av.open(str(src)) as source, av.open(str(dst), "w") as sink:
        stream_in = source.streams.audio[0]
        stream_out = sink.add_stream(codec, rate=forced_rate or stream_in.rate)
        stream_out.layout = stream_in.layout.name
        if target == "opus":
            try:
                stream_out.codec_context.bit_rate = 32000
            except AttributeError:  # PyAV < 14 used ``bitrate``
                stream_out.codec_context.bitrate = 32000  # type: ignore[attr-defined]
        for frame in source.decode(stream_in):
            for packet in stream_out.encode(frame):
                sink.mux(packet)
        for packet in stream_out.encode(None):
            sink.mux(packet)
    return dst
