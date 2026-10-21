import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from typing import Callable, Iterable

from radio_record.config import Settings
from radio_record.protocol import CODEC_PCM_S16LE, FrameHeader
from radio_record.receiver import RadioServer

RATE = 16000
FRAME_MS = 20


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
        await writer.wait_closed()


async def wait_for(predicate: Callable[[], object], timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def at_least(directory: Path, pattern: str, count: int) -> list[Path] | None:
    found = sorted(directory.glob(pattern))
    return found if len(found) >= count else None


class IngestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.output = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _serve(self, on_finalize=None, **overrides) -> RadioServer:
        options = dict(
            host="127.0.0.1",
            port=0,
            output_dir=self.output,
            segment_seconds=60.0,
        )
        options.update(overrides)
        settings = Settings(**options)
        server = RadioServer(settings, on_finalize=on_finalize)
        await server.start()
        self.addAsyncCleanup(server.close)
        return server

    async def test_single_segment_is_finalised(self) -> None:
        server = await self._serve()
        await push_frames(server.port, "esp32-a", [(0, RATE * 2)])

        directory = self.output / "esp32-a"
        path = await wait_for(lambda: next(directory.glob("*.wav"), None))

        with wave.open(str(path), "rb") as handle:
            self.assertEqual(handle.getframerate(), RATE)
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getnframes(), RATE * 2)
        self.assertEqual(list(directory.glob("*.part")), [])

    async def test_gap_is_padded_with_silence(self) -> None:
        server = await self._serve()
        await push_frames(server.port, "esp32-b", [(0, RATE), (RATE + 8000, RATE)])

        directory = self.output / "esp32-b"
        path = await wait_for(lambda: next(directory.glob("*.wav"), None))

        with wave.open(str(path), "rb") as handle:
            self.assertEqual(handle.getnframes(), RATE * 2 + 8000)

    async def test_gap_padding_can_be_disabled(self) -> None:
        server = await self._serve(pad_gaps=False)
        await push_frames(server.port, "esp32-c", [(0, RATE), (RATE + 8000, RATE)])

        directory = self.output / "esp32-c"
        path = await wait_for(lambda: next(directory.glob("*.wav"), None))

        with wave.open(str(path), "rb") as handle:
            self.assertEqual(handle.getnframes(), RATE * 2)

    async def test_segments_rotate_and_keep_all_samples(self) -> None:
        server = await self._serve(segment_seconds=0.5)
        await push_frames(server.port, "esp32-d", [(0, RATE * 3)])

        directory = self.output / "esp32-d"

        def collected() -> tuple[int, list[Path]] | None:
            files = sorted(directory.glob("*.wav"))
            total = 0
            for path in files:
                with wave.open(str(path), "rb") as handle:
                    total += handle.getnframes()
            return (total, files) if total == RATE * 3 else None

        total, files = await wait_for(collected)
        self.assertGreaterEqual(len(files), 2)
        self.assertEqual(total, RATE * 3)

    async def test_reconnect_starts_a_new_segment(self) -> None:
        server = await self._serve()
        await push_frames(server.port, "esp32-e", [(0, RATE)])
        await push_frames(server.port, "esp32-e", [(RATE, RATE)])

        directory = self.output / "esp32-e"
        files = await wait_for(lambda: at_least(directory, "*.wav", 2))
        self.assertEqual(len(files), 2)

    async def test_garbage_closes_connection_without_output(self) -> None:
        server = await self._serve()
        _, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"not-a-frame" * 10)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)
        self.assertEqual(list(self.output.rglob("*.wav")), [])

    async def test_format_mismatch_is_rejected(self) -> None:
        server = await self._serve()
        await push_frames(server.port, "esp32-g", [(0, 8000)], sample_rate=8000)
        await asyncio.sleep(0.2)
        self.assertEqual(list(self.output.rglob("*.wav")), [])

    async def test_transcode_to_flac(self) -> None:
        try:
            import av  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PyAV is not installed")

        from radio_record.transcode import transcode

        async def finalize(path: Path) -> None:
            await asyncio.to_thread(transcode, path, "flac")

        server = await self._serve(on_finalize=finalize)
        await push_frames(server.port, "esp32-f", [(0, RATE)])

        directory = self.output / "esp32-f"
        flac = await wait_for(lambda: next(directory.glob("*.flac"), None))
        self.assertTrue(flac.stat().st_size > 0)

        import av

        with av.open(str(flac)) as container:
            self.assertAlmostEqual(float(container.duration) / 1e6, 1.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
