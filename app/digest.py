from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram.types import FSInputFile

from app.config import Config
from app.db import Database
from app.pricing import MOSCOW, fmt_rub, fmt_when, utcnow
from app.service import channel_id
from app.sheets import write_ledger
from app.tbank import TBank, TBankError

log = logging.getLogger("digest")

DIAG_EVERY_HOURS = 6
REPORT_HOURS = (10, 19)
REPORT_WINDOW = timedelta(minutes=20)


def diag_slot(now_msk: datetime) -> datetime:
    hour = (now_msk.hour // DIAG_EVERY_HOURS) * DIAG_EVERY_HOURS
    return now_msk.replace(hour=hour, minute=0, second=0, microsecond=0)


def report_slot(now_msk: datetime) -> datetime | None:
    for hour in REPORT_HOURS:
        slot = now_msk.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot <= now_msk < slot + REPORT_WINDOW:
            return slot
    return None


async def scheduled_mail(db: Database, tbank: TBank | None, bot, cfg: Config) -> None:
    now = utcnow().astimezone(MOSCOW)
    slot = diag_slot(now)
    key = slot.strftime("%Y-%m-%dT%H")
    if await db.setting("diag_slot") != key:
        if await send_diag(db, tbank, bot, cfg, now):
            await db.set_setting("diag_slot", key)
    due = report_slot(now)
    if due is None:
        return
    report_key = due.strftime("%Y-%m-%dT%H")
    if await db.setting("report_slot") != report_key:
        if await send_report(db, bot, cfg, now):
            await db.set_setting("report_slot", report_key)


async def send_diag(db: Database, tbank: TBank | None, bot, cfg: Config, now: datetime) -> bool:
    lines = [f"<b>Самодиагностика</b> · {fmt_when(now)}"]
    if bot is None:
        lines.append("Бот не подключён.")
    else:
        try:
            me = await bot.get_me()
            lines.append(f"Бот на связи: @{me.username}.")
        except Exception as exc:
            lines.append(f"Бот не отвечает: {exc.__class__.__name__}.")
    if tbank is None:
        lines.append("Терминал не задан.")
    else:
        lines.append(await _terminal_line(tbank))
    lines.append(await _channel_line(db, bot, cfg))
    lines.append(f"Записей в реестре: {await db.ledger_count()}.")
    if cfg.public_base_url:
        lines.append(f"Сайт админки: {cfg.public_base_url}/admin")
    text = "\n".join(lines)
    await db.set_setting("diag_text", text)
    return await tell_admins(bot, cfg, text)


async def send_report(db: Database, bot, cfg: Config, now: datetime) -> bool:
    rows = await db.ledger_rows()
    stats = await db.stats()
    paid = sum(int(row["amount"]) for row in rows if row.get("kind") != "refund")
    refunds = sum(int(row["amount"]) for row in rows if row.get("kind") == "refund")
    text = (
        f"<b>Сводка</b> · {fmt_when(now)}\n"
        f"Активных доступов: {stats['active']}\n"
        f"Оплаты: {fmt_rub(paid)}\n"
        f"Возвраты: {fmt_rub(refunds)}"
    )
    path = None
    if rows:
        path = Path(cfg.db_path).with_name("ledger.xlsx")
        write_ledger(path, rows, int(stats["active"]))
        text += "\nРеестр во вложении, суммы в рублях."
    else:
        text += "\nРеестр пока пустой."
    if bot is None or not cfg.admin_ids:
        return False
    delivered = False
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text)
            if path is not None:
                await bot.send_document(admin_id, FSInputFile(path, filename="reestr-private.xlsx"))
            delivered = True
        except Exception:
            log.exception("report was not delivered to %s", admin_id)
    return delivered


async def tell_admins(bot, cfg: Config, text: str) -> bool:
    if bot is None or not cfg.admin_ids:
        return False
    delivered = False
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text)
            delivered = True
        except Exception:
            log.exception("note was not delivered to %s", admin_id)
    return delivered


async def _terminal_line(tbank: TBank) -> str:
    try:
        await tbank.get_state("0")
        return "Терминал отвечает."
    except TBankError as exc:
        if exc.code == "204":
            return "Терминал отклонил пароль."
        return "Терминал отвечает."
    except Exception:
        return "Нет связи с терминалом."


async def _channel_line(db: Database, bot, cfg: Config) -> str:
    channel = await channel_id(db, cfg)
    if channel is None:
        return "Канал не привязан."
    if bot is None:
        return f"Канал {channel} записан, бот не подключён."
    try:
        chat = await bot.get_chat(channel)
        me = await bot.get_me()
        member = await bot.get_chat_member(channel, me.id)
    except Exception:
        return "Канал записан, но бот не смог его открыть."
    title = chat.title or str(channel)
    if member.status not in {"administrator", "creator"}:
        return f"Канал «{title}»: бот не администратор."
    invite = getattr(member, "can_invite_users", False)
    restrict = getattr(member, "can_restrict_members", False)
    if invite and restrict:
        return f"Канал «{title}»: бот администратор, приглашает и исключает."
    return f"Канал «{title}»: боту не хватает права приглашать или исключать."
