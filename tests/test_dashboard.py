import asyncio
import json
import logging
import socket
import tempfile
import unittest
from pathlib import Path

from radio_record.config import Settings
from radio_record.dashboard import DashboardError, LogBuffer, start_dashboard
from radio_record.receiver import RadioServer
from .helpers import RATE, http_get, http_json, open_stream, push_frames, sse_collect, wait_for


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.output = Path(self._tmp.name)
        self.settings = Settings(
            host="127.0.0.1",
            port=0,
            output_dir=self.output,
            dashboard_host="127.0.0.1",
            dashboard_port=0,
        )
        self.server = RadioServer(self.settings)
        await self.server.start()
        self.dashboard = await start_dashboard(self.server, self.settings, LogBuffer(500))
        self.port = self.dashboard.port
        self.addAsyncCleanup(self._shutdown)

    async def _shutdown(self) -> None:
        await self.dashboard.stop()
        await self.server.close()
        self._tmp.cleanup()

    async def test_index_is_served(self) -> None:
        status, headers, body = await http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<title>radio-record</title>", body)
        self.assertIn("Журнал".encode(), body)

    async def test_healthz(self) -> None:
        status, payload = await http_json(self.port, "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    async def test_status_without_devices(self) -> None:
        status, payload = await http_json(self.port, "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["devices"], [])
        self.assertEqual(payload["recent"], [])
        self.assertEqual(payload["sample_rate"], RATE)
        self.assertIsNotNone(payload["started_at"])
        self.assertIsNotNone(payload["listen"])

    async def test_status_reports_live_session(self) -> None:
        async def poll() -> dict:
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                _, payload = await http_json(self.port, "/api/status")
                if payload["devices"]:
                    return payload
                await asyncio.sleep(0.05)
            raise AssertionError("device never appeared in /api/status")

        async with open_stream(self.server.port, "esp32-gui") as send:
            await send(RATE // 2)
            payload = await poll()

        device = payload["devices"][0]
        self.assertEqual(device["device_id"], "esp32-gui")
        self.assertEqual(device["state"], "streaming")
        self.assertGreater(device["frames"], 0)
        self.assertEqual(device["samples"], RATE // 2)
        self.assertEqual(device["total_bytes"], RATE // 2 * 2)
        self.assertEqual(device["queue_capacity"], self.settings.queue_frames)
        self.assertTrue(str(device["segment_name"]).endswith(".part"))
        self.assertGreater(device["segment_seconds"], 0)

    async def test_disconnected_session_moves_to_recent(self) -> None:
        await push_frames(self.server.port, "esp32-gone", [(0, RATE // 2)])

        async def poll() -> dict:
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline:
                _, payload = await http_json(self.port, "/api/status")
                if payload["recent"]:
                    return payload
                await asyncio.sleep(0.05)
            raise AssertionError("session never showed up in the recent list")

        payload = await poll()
        self.assertEqual(payload["devices"], [])
        recent = payload["recent"][0]
        self.assertEqual(recent["device_id"], "esp32-gone")
        self.assertEqual(recent["reason"], "stream ended")
        self.assertGreater(recent["frames"], 0)
        self.assertEqual(recent["segments_written"], 1)
        self.assertIn("closed_at", recent)

    async def test_logs_endpoint_captures_server_events(self) -> None:
        await push_frames(self.server.port, "esp32-log", [(0, RATE // 4)])
        await wait_for(
            lambda: any(
                "esp32-log" in str(entry["message"])
                for entry in self.dashboard.logs.since(0, 1000)
            )
        )

        status, _, body = await http_get(self.port, "/api/logs")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertGreater(payload["latest"], 0)
        messages = " ".join(str(entry["message"]) for entry in payload["entries"])
        self.assertIn("esp32-log: connected from", messages)
        self.assertTrue(all("level" in entry and "seq" in entry for entry in payload["entries"]))

    async def test_logs_endpoint_cursor(self) -> None:
        await push_frames(self.server.port, "esp32-cur", [(0, RATE // 4)])
        await wait_for(lambda: not self.server.sessions)
        await asyncio.sleep(0.2)  # let the recorder log the finalised segment

        _, first = await http_json(self.port, "/api/logs")
        latest = first["latest"]
        self.assertGreater(latest, 0)

        _, second = await http_json(self.port, f"/api/logs?after={latest}")
        self.assertEqual(second["entries"], [])

    async def test_event_stream_delivers_log_and_status(self) -> None:
        async with open_stream(self.server.port, "esp32-sse") as send:
            await send(RATE // 4)
            raw = await sse_collect(self.port, "/api/events", timeout=8.0)
            await send(RATE // 4)

        self.assertIn("event: status", raw)
        self.assertIn("event: log", raw)
        self.assertIn("esp32-sse", raw)
        self.assertIn("data: ", raw)

    async def test_dashboard_binds_ephemeral_port(self) -> None:
        self.assertGreater(self.port, 0)
        self.assertIn(str(self.port), self.dashboard.url)

    async def test_busy_port_reports_clear_error(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as busy:
            busy_port = busy.getsockname()[1]
            settings = Settings(
                host="127.0.0.1",
                port=0,
                output_dir=self.output,
                dashboard_host="127.0.0.1",
                dashboard_port=busy_port,
            )
            handlers = len(logging.getLogger().handlers)

            with self.assertRaises(DashboardError) as caught:
                await start_dashboard(self.server, settings)

            self.assertIn("cannot bind dashboard", str(caught.exception))
            self.assertEqual(len(logging.getLogger().handlers), handlers)


if __name__ == "__main__":
    unittest.main()
