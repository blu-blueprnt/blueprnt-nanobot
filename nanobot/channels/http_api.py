"""HTTP API channel — accepts chat messages over HTTP and streams responses as SSE."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


@dataclass
class HttpApiConfig:
    enabled: bool = False
    port: int = 8080
    host: str = "0.0.0.0"
    api_token: str = ""
    allow_from: list[str] = field(default_factory=lambda: ["*"])
    allowed_origins: list[str] = field(default_factory=lambda: ["*"])


class HttpApiChannel(BaseChannel):
    """HTTP API channel that exposes a REST endpoint for chat with SSE streaming responses."""

    name = "http_api"
    display_name = "HTTP API"

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            self.config = HttpApiConfig(**{k: v for k, v in config.items() if k != "enabled"})
            self.config.enabled = config.get("enabled", False)
        else:
            self.config = config
        super().__init__(self.config, bus)

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # Per-request queues: chat_id -> Queue[OutboundMessage | None]
        # None sentinel signals end of response
        self._pending: dict[str, asyncio.Queue[OutboundMessage | None]] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._app = web.Application(middlewares=[self._cors_middleware])
        self._app.router.add_post("/api/chat", self._handle_chat)
        self._app.router.add_get("/api/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self.config.host,
            self.config.port,
        )
        await self._site.start()
        logger.info(
            "HTTP API channel listening on {}:{}",
            self.config.host,
            self.config.port,
        )

    async def stop(self) -> None:
        self._running = False
        # Signal all pending requests to finish
        for q in self._pending.values():
            await q.put(None)
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("HTTP API channel stopped")

    # ── Outbound dispatch (called by ChannelManager) ──────────────────

    async def send(self, msg: OutboundMessage) -> None:
        q = self._pending.get(msg.chat_id)
        if q is None:
            logger.debug("http_api: no pending request for chat_id={}", msg.chat_id)
            return

        # Stream-done marker: don't enqueue content, just send sentinel to
        # close the SSE connection (all content was already streamed as deltas).
        if msg.metadata.get("_stream_done"):
            await q.put(None)
            return

        await q.put(msg)

        # If this is the final message (not a progress update), send sentinel
        if not msg.metadata.get("_progress"):
            await q.put(None)

    # ── HTTP handlers ─────────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_chat(self, request: web.Request) -> web.StreamResponse:
        # ── Auth ──
        if self.config.api_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {self.config.api_token}":
                return web.json_response({"error": "unauthorized"}, status=401)

        # ── Parse body ──
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        message = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "message is required"}, status=400)

        sender_id = str(body.get("sender_id", "api"))
        chat_id = str(body.get("chat_id") or uuid.uuid4().hex)
        metadata = body.get("metadata") or {}

        # ── ACL check ──
        if not self.is_allowed(sender_id):
            return web.json_response({"error": "sender not allowed"}, status=403)

        # ── Register response queue BEFORE publishing inbound ──
        q: asyncio.Queue[OutboundMessage | None] = asyncio.Queue()
        self._pending[chat_id] = q

        try:
            # Publish inbound message to the bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=message,
                metadata=metadata,
            )

            # ── Stream SSE response ──
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=300)
                except asyncio.TimeoutError:
                    # Send timeout finish and close
                    await response.write(b'd:{"finishReason":"timeout"}\n')
                    break

                if msg is None:
                    # Sentinel: end of response
                    await response.write(b'd:{"finishReason":"stop"}\n')
                    break

                if msg.content:
                    # Vercel AI SDK data stream format: 0:"text chunk"\n
                    escaped = (
                        msg.content
                        .replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                    )
                    chunk = f'0:"{escaped}"\n'
                    await response.write(chunk.encode("utf-8"))

            await response.write_eof()
            return response
        finally:
            self._pending.pop(chat_id, None)

    # ── CORS middleware ───────────────────────────────────────────────

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        # Handle preflight
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as ex:
                resp = ex

        origin = request.headers.get("Origin", "*")
        allowed = self.config.allowed_origins
        if "*" in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
        elif origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin

        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    # ── Config ────────────────────────────────────────────────────────

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return {
            "enabled": False,
            "port": 8080,
            "host": "0.0.0.0",
            "api_token": "",
            "allow_from": ["*"],
            "allowed_origins": ["*"],
        }
