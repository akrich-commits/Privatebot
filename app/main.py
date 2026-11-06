from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from aiohttp import ClientTimeout, web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, LinkPreviewOptions

from app.bot import build_router
from app.config import Config
from app.db import Database
from app.digest import tell_admins
from app.http import build_app
from app.jobs import tick
from app.runtime import Runtime
from app.tbank import TBank

log = logging.getLogger("private-bot")
STALL_LIMIT = 90
READ_CAP = 60


def telegram_timeout(seconds: float) -> ClientTimeout:
    """Bound both the whole call and any silent read.

    A numeric aiohttp timeout limits only the total and leaves sock_read empty.
    Telegram can send response headers and then keep the body open, so a long
    poll without a read limit never fails.
    """
    limit = min(max(float(seconds), 1.0), READ_CAP)
    return ClientTimeout(total=limit, sock_connect=15, sock_read=limit)


def keeps_bot_alive(method: object) -> bool:
    """Only a completed getUpdates proves the bot is still receiving people.

    Sending a warning or a diagnostic uses the same session. Those calls must
    not refresh the stall clock: otherwise a stuck long poll looks healthy.
    """
    return getattr(method, "__api_method__", "") == "getUpdates"


def supervisor_action(age: float, limit: float, armed: bool) -> str:
    if armed and age > limit:
        return "exit"
    return "beat"


class Pulse:
    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._at = time.monotonic()
        self._lock = threading.Lock()

    def mark(self) -> None:
        with self._lock:
            self._at = time.monotonic()

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._at

    def beat(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time()}\n", encoding="utf-8")

    def write_reason(self, reason: str) -> None:
        if not self.path:
            return
        path = Path(self.path).with_name("restart-reason")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason + "\n", encoding="utf-8")


def watch_pulse(pulse: Pulse, limit: float = STALL_LIMIT, armed: bool = True) -> None:
    while True:
        time.sleep(15)
        age = pulse.age()
        if supervisor_action(age, limit, armed) == "exit":
            log.error("telegram polling stalled for %.0fs, restarting", age)
            pulse.write_reason("stall")
            os._exit(1)
        pulse.beat()


class LimitedSession(AiohttpSession):
    """Drop a stalled Telegram connection instead of waiting indefinitely."""

    def __init__(self, pulse: Pulse, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.pulse = pulse

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[object],
        timeout: int | None = None,
    ) -> object:
        limit = float(self.timeout if timeout is None else timeout)
        result = await super().make_request(
            bot,
            method,
            timeout=telegram_timeout(limit),  # type: ignore[arg-type]
        )
        if keeps_bot_alive(method):
            self.pulse.mark()
            self.pulse.beat()
        return result


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    pulse = Pulse(str(Path(cfg.db_path).with_name("heartbeat")))
    pulse.beat()
    threading.Thread(
        target=watch_pulse,
        kwargs={"pulse": pulse, "armed": bool(cfg.bot_token)},
        name="telegram-pulse",
        daemon=True,
    ).start()
    db = Database(cfg.db_path)
    await db.connect()
    tbank = TBank(cfg) if cfg.tbank_ready else None
    if tbank is not None:
        await tbank.__aenter__()
    runtime = Runtime(cfg=cfg, db=db, tbank=tbank)
    bot: Bot | None = None
    if cfg.bot_token:
        bot = Bot(
            cfg.bot_token,
            session=LimitedSession(pulse, timeout=25),
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview=LinkPreviewOptions(is_disabled=True),
            ),
        )
        me = await bot.get_me()
        runtime.username = me.username
        public_commands = [
            BotCommand(command="start", description="Тарифы Private"),
            BotCommand(command="status", description="Мой доступ"),
            BotCommand(command="stop", description="Отменить автопродление"),
            BotCommand(command="id", description="Мой Telegram id"),
        ]
        admin_commands = public_commands + [
            BotCommand(command="admin", description="Админка"),
            BotCommand(command="price", description="Цены и акции"),
            BotCommand(command="promo", description="Промокоды"),
            BotCommand(command="grant", description="Выдать доступ"),
            BotCommand(command="who", description="Карточка человека"),
            BotCommand(command="stats", description="Сводка"),
            BotCommand(command="export", description="Реестр Excel"),
            BotCommand(command="refund", description="Возврат платежа"),
        ]
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        for admin_id in cfg.admin_ids:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                log.exception("admin commands were not set for %s", admin_id)
        reason_path = Path(cfg.db_path).with_name("restart-reason")
        if reason_path.is_file() and reason_path.read_text(encoding="utf-8").strip() == "stall":
            sent = await tell_admins(
                bot,
                cfg,
                "Бот завис и только что перезапустился сам. Сообщения за это время сейчас догонит.",
            )
            if sent:
                reason_path.unlink(missing_ok=True)
    app = build_app(runtime, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()
    log.info("http on :%s", cfg.port)

    async def loop() -> None:
        n = 0
        while True:
            try:
                await tick(db, tbank, bot, cfg)
            except Exception:
                log.exception("scheduler tick failed")
            n += 1
            if n % 20 == 0:
                log.info("scheduler alive")
            await asyncio.sleep(30)

    task = asyncio.create_task(loop())
    try:
        if bot is None:
            log.warning("BOT_TOKEN пуст, бот не подключён к Telegram")
            await asyncio.Event().wait()
        else:
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(runtime))
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(bot)
    finally:
        task.cancel()
        await runner.cleanup()
        if bot is not None:
            await bot.session.close()
        if tbank is not None:
            await tbank.close()
        await db.close()


def main() -> None:
    asyncio.run(run())
