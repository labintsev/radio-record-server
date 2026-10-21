"""Web dashboard: live logs and connection status over HTTP + SSE.

The dashboard is deliberately read-only and lives outside the audio path: it
observes the ingest server, never participates in it. It runs on the same event
loop as the TCP server so snapshots are plain reads of loop-owned state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from collections import deque
from dataclasses import dataclass
from importlib import resources
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from radio_record.config import Settings
from radio_record.receiver import RadioServer

log = logging.getLogger(__name__)

SSE_POLL_SECONDS = 0.25
SSE_STATUS_EVERY = 4  # ticks, ~1s
LOG_BACKLOG = 200


class DashboardError(RuntimeError):
    """Raised when the dashboard cannot start."""


class LogBuffer(logging.Handler):
    """Ring buffer of log records, readable by any thread.

    Records come from the event loop and from worker threads (transcoding runs
    in ``asyncio.to_thread``), so the buffer is lock protected and the SSE
    endpoint polls it instead of being pushed to. Polling at 4 Hz costs nothing
    and avoids cross-thread wakeups.
    """

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._entries: deque[dict[str, object]] = deque(maxlen=max(100, capacity))
        self._lock = threading.Lock()
        self._seq = 0

    def attach(self) -> None:
        root = logging.getLogger()
        # INFO is the level the server logs its useful events at, and the root
        # logger defaults to WARNING, which would drop them before they reach
        # any handler. An explicitly configured level is left alone.
        if root.level == logging.WARNING:
            root.setLevel(logging.INFO)
        root.addHandler(self)

    def detach(self) -> None:
        logging.getLogger().removeHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad %-format must not break logging
            message = str(record.msg)
        if record.exc_info:
            message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
        entry = {
            "time": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._entries.append(entry)

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, seq: int, limit: int = LOG_BACKLOG) -> list[dict[str, object]]:
        with self._lock:
            fresh = [entry for entry in self._entries if int(entry["seq"]) > seq]
        return fresh[-limit:]


def _sse(event: str, payload: object) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {data}\n\n".encode()


def build_app(server: RadioServer, logs: LogBuffer) -> FastAPI:
    app = FastAPI(title="radio-record dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = resources.files("radio_record").joinpath("static/index.html").read_text("utf-8")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(server.snapshot())

    @app.get("/api/logs")
    async def logs_endpoint(
        after: int = Query(0, ge=0), limit: int = Query(LOG_BACKLOG, ge=1, le=1000)
    ) -> JSONResponse:
        return JSONResponse({"latest": logs.latest_seq, "entries": logs.since(after, limit)})

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _stream(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _stream(request: Request) -> AsyncIterator[bytes]:
        cursor = max(0, logs.latest_seq - LOG_BACKLOG)
        try:
            yield _sse("status", server.snapshot())
            tick = 0
            while not await request.is_disconnected():
                for entry in logs.since(cursor):
                    cursor = int(entry["seq"])
                    yield _sse("log", entry)
                tick += 1
                if tick % SSE_STATUS_EVERY == 0:
                    yield _sse("status", server.snapshot())
                await asyncio.sleep(SSE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dropped browser tab is normal
            log.debug("event stream closed", exc_info=True)

    return app


@dataclass
class Dashboard:
    """Handle for a running dashboard server."""

    uvicorn_server: uvicorn.Server
    task: asyncio.Task[None]
    logs: LogBuffer
    host: str
    port: int

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    async def stop(self) -> None:
        self.uvicorn_server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        except BaseException:  # noqa: BLE001 - uvicorn re-raises captured signals
            pass
        finally:
            self.logs.detach()


async def start_dashboard(
    server: RadioServer, settings: Settings, logs: LogBuffer | None = None
) -> Dashboard:
    logs = logs or LogBuffer(settings.log_buffer)

    # Bind first so a busy port fails before any global logging state changes.
    try:
        sock = socket.create_server((settings.dashboard_host, settings.dashboard_port))
    except OSError as exc:
        raise DashboardError(
            f"cannot bind dashboard to {settings.dashboard_host}:{settings.dashboard_port} "
            f"(errno {exc.errno}): {exc.strerror}"
        ) from exc
    sock.setblocking(False)
    logs.attach()
    host, port = sock.getsockname()[:2]

    try:
        config = uvicorn.Config(
            build_app(server, logs),
            log_config=None,
            access_log=False,
            lifespan="off",
        )
        uv = uvicorn.Server(config)
        task = asyncio.create_task(uv.serve(sockets=[sock]), name="dashboard")

        for _ in range(200):
            if uv.started or task.done():
                break
            await asyncio.sleep(0.02)

        if not uv.started:
            error = task.exception() if task.done() else "startup timed out"
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise DashboardError(f"dashboard failed to start: {error}")
    except BaseException:
        logs.detach()
        sock.close()
        raise

    dashboard = Dashboard(uvicorn_server=uv, task=task, logs=logs, host=host, port=port)
    log.info("dashboard on %s", dashboard.url)
    return dashboard
