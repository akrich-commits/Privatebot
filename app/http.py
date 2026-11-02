from __future__ import annotations

import logging

from aiohttp import web

from app.runtime import Runtime
from app.service import apply_note
from app.tbank import parse_note, verify_notification

log = logging.getLogger("http")


def build_app(runtime: Runtime, bot) -> web.Application:
    app = web.Application()
    app["runtime"] = runtime
    app["bot"] = bot

    async def health(_: web.Request) -> web.Response:
        cfg = runtime.cfg
        body = {
            "ok": True,
            "bot": bool(cfg.bot_token),
            "terminal": cfg.tbank_ready,
            "channel": bool(cfg.channel_id or await runtime.db.setting("channel_id")),
            "taxation": cfg.taxation,
            "public_url": bool(cfg.public_base_url),
        }
        return web.json_response(body)

    async def notify(request: web.Request) -> web.Response:
        if not runtime.cfg.tbank_password:
            return web.Response(status=503, text="NO TERMINAL")
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="BAD JSON")
        if not isinstance(payload, dict) or not verify_notification(payload, runtime.cfg.tbank_password):
            log.warning("rejected tbank notification")
            return web.Response(status=400, text="BAD TOKEN")
        terminal = str(payload.get("TerminalKey") or "")
        if terminal and terminal != runtime.cfg.tbank_terminal_key:
            return web.Response(status=400, text="BAD TERMINAL")
        await apply_note(runtime.db, bot, runtime.cfg, parse_note(payload))
        return web.Response(text="OK")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/tbank/notify", notify)
    return app
