import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from radio_record.__main__ import build_parser, serve, settings_from_args
from radio_record.config import Settings
from .helpers import http_get, wait_until


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def dashboard_answers(port: int) -> bool:
    try:
        status, _, _ = await http_get(port, "/healthz", timeout=1.0)
    except (OSError, asyncio.TimeoutError):
        return False
    return status == 200


async def tcp_accepts(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


class ParserTests(unittest.TestCase):
    def test_dashboard_enabled_by_default(self) -> None:
        settings = settings_from_args(build_parser().parse_args([]))
        self.assertTrue(settings.dashboard)
        self.assertEqual(settings.dashboard_host, "127.0.0.1")
        self.assertEqual(settings.dashboard_port, 8080)

    def test_headless_flag(self) -> None:
        settings = settings_from_args(build_parser().parse_args(["--no-dashboard"]))
        self.assertFalse(settings.dashboard)

    def test_dashboard_bind_overrides(self) -> None:
        args = build_parser().parse_args(
            ["--dashboard-host", "0.0.0.0", "--dashboard-port", "9000", "--open-dashboard"]
        )
        settings = settings_from_args(args)
        self.assertEqual(settings.dashboard_host, "0.0.0.0")
        self.assertEqual(settings.dashboard_port, 9000)
        self.assertTrue(settings.dashboard_open)


class ServeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.output = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _settings(self, **overrides) -> Settings:
        options = dict(
            host="127.0.0.1",
            port=free_port(),
            output_dir=self.output,
            dashboard_host="127.0.0.1",
            dashboard_port=free_port(),
        )
        options.update(overrides)
        return Settings(**options)

    async def test_serve_runs_both_servers_and_stops_cleanly(self) -> None:
        settings = self._settings()
        task = asyncio.create_task(serve(settings), name="serve")
        try:
            await wait_until(lambda: dashboard_answers(settings.dashboard_port))
            self.assertTrue(await tcp_accepts(settings.port))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(await dashboard_answers(settings.dashboard_port))
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", settings.port)

    async def test_headless_mode_binds_only_ingest_port(self) -> None:
        settings = self._settings(dashboard=False)
        task = asyncio.create_task(serve(settings), name="serve")
        try:
            await wait_until(lambda: tcp_accepts(settings.port))
            self.assertFalse(await dashboard_answers(settings.dashboard_port))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
