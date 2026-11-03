from __future__ import annotations

import logging
from datetime import timedelta

from app.config import Config
from app.db import Database
from app.digest import scheduled_mail
from app.pricing import WARN_BEFORE, fmt_rub, fmt_when, iso, parse_dt, period_title, utcnow
from app.keyboards import warning_kb
from app.service import charge_subscription, kick_subscription, sync_payment
from app.tbank import TBank

log = logging.getLogger("jobs")


def warning_text(sub: dict) -> str:
    until = parse_dt(sub["access_until"])
    when = fmt_when(until) if until else "скоро"
    price = fmt_rub(int(sub["price_kopecks"]))
    title = period_title(int(sub["months"]))
    if sub.get("autopay") and sub.get("rebill_id") and int(sub["price_kopecks"]) > 0:
        card = f" Карта {sub['pan']}." if sub.get("pan") else ""
        return (
            f"{when} спишу {price} за {title} Private.{card}\n"
            "Это предупреждение за двое суток. Отменить автопродление можно кнопкой ниже. "
            "Оплаченный срок при этом не обрывается."
        )
    return (
        f"Доступ к Private до {when}. Автосписания нет.\n"
        f"Чтобы остаться, оплатите {title}. Откройте /start."
    )


async def tick(db: Database, tbank: TBank | None, bot, cfg: Config) -> None:
    now = utcnow()
    await db.release_expired_reservations(iso(now))
    await db.release_stale_charges(iso(now - timedelta(minutes=15)))
    if tbank is not None:
        for payment in await db.open_payments(iso(now - timedelta(seconds=20))):
            created = parse_dt(payment["created_at"])
            if created and created < now - timedelta(hours=6) and payment["status"] != "confirmed":
                await db.mark_payment(payment["order_id"], "failed")
                await db.release_promo(payment["order_id"])
                continue
            await sync_payment(db, tbank, bot, cfg, payment)
        for sub in await db.due_charges(iso(now)):
            await charge_subscription(db, tbank, bot, cfg, sub, retry=False)
        for sub in await db.due_retries(iso(now)):
            await charge_subscription(db, tbank, bot, cfg, sub, retry=True)
    horizon = iso(now + WARN_BEFORE)
    for sub in await db.due_warnings(iso(now), horizon):
        await db.mark_warned(int(sub["telegram_id"]), iso(now))
        if bot is not None:
            try:
                await bot.send_message(
                    int(sub["telegram_id"]),
                    warning_text(sub),
                    reply_markup=warning_kb(sub),
                )
            except Exception:
                log.info("warning not delivered to %s", sub["telegram_id"])
    for sub in await db.due_removals(iso(now)):
        await kick_subscription(db, bot, cfg, sub)
    await scheduled_mail(db, tbank, bot, cfg)
