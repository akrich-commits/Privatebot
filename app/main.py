from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, LinkPreviewOptions

from app.bot import build_router
from app.config import Config
from app.db import Database
from app.http import build_app
from app.jobs import tick
from app.runtime import Runtime
from app.tbank import TBank

log = logging.getLogger("private-bot")


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
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
            BotCommand(command="export", description="Реестр CSV"),
            BotCommand(command="refund", description="Возврат платежа"),
        ]
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        for admin_id in cfg.admin_ids:
            try:
                await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                log.exception("admin commands were not set for %s", admin_id)
    app = build_app(runtime, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()
    log.info("http on :%s", cfg.port)

    async def loop() -> None:
        while True:
            try:
                await tick(db, tbank, bot, cfg)
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(30)

    task = asyncio.create_task(loop())
    try:
        if bot is None:
            log.warning("BOT_TOKEN пуст, бот не подключён к Telegram")
            await asyncio.Event().wait()
        else:
            dispatcher = Dispatcher()
            dispatcher.include_router(build_router(runtime))
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
