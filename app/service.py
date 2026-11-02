from __future__ import annotations

import logging
from datetime import timedelta

from app.config import Config
from app.db import Database
from app.pricing import (
    MIN_KOPECKS,
    PROMO_HOLD,
    add_months,
    apply_promo,
    current_price,
    fmt_rub,
    fmt_when,
    iso,
    parse_dt,
    period_title,
    utcnow,
)
from app.tbank import BankNote, TBank, TBankError, parse_note

log = logging.getLogger("service")

ACTIVE_MEMBER = {"creator", "administrator", "member", "restricted"}


def order_id_for(months: int, telegram_id: int) -> str:
    stamp = utcnow().strftime("%m%d%H%M%S")
    return f"{months}m-{telegram_id}-{stamp}"[:36]


def promo_error(
    promo: dict | None,
    months: int,
    telegram_id: int,
    now,
    *,
    already_used: bool = False,
) -> str | None:
    if promo is None:
        return "Такого промокода нет."
    if promo["disabled"]:
        return "Промокод выключен."
    expires = parse_dt(promo.get("expires_at"))
    if expires and expires <= now:
        return "Срок промокода вышел."
    if promo["months"] and int(promo["months"]) != months:
        return f"Промокод только на {period_title(int(promo['months']))}."
    if promo.get("shared"):
        if already_used:
            return "Вы уже использовали этот промокод."
        return None
    if promo["used_by"]:
        return "Промокод уже использован."
    reserved_until = parse_dt(promo.get("reserved_until"))
    reserved_by = promo.get("reserved_by")
    if reserved_by and reserved_by != telegram_id and reserved_until and reserved_until > now:
        return "Промокод сейчас удерживается другим человеком. Попробуйте через час."
    return None


async def promo_problem(db: Database, promo: dict | None, months: int, telegram_id: int, now) -> str | None:
    already_used = False
    if promo is not None and promo.get("shared"):
        already_used = await db.shared_promo_used(str(promo["code"]), telegram_id)
    return promo_error(promo, months, telegram_id, now, already_used=already_used)


async def quoted_price(db: Database, user: dict, months: int):
    plan = await db.plan(months)
    if plan is None:
        raise ValueError("Нет такого тарифа.")
    now = utcnow()
    base = current_price(plan, now)
    code = user.get("pending_promo")
    promo = None
    if code:
        promo = await db.promo(code)
        problem = await promo_problem(db, promo, months, int(user["telegram_id"]), now)
        if problem:
            await db.set_pending(int(user["telegram_id"]), months, None)
            raise ValueError(problem)
        base = apply_promo(base, promo)
    if base < MIN_KOPECKS:
        raise ValueError("Сумма меньше 10 ₽.")
    return plan, base, promo


async def channel_id(db: Database, cfg: Config) -> int | None:
    if cfg.channel_id:
        return cfg.channel_id
    stored = await db.setting("channel_id")
    return int(stored) if stored else None


async def member_of_channel(bot, channel: int, telegram_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel, telegram_id)
    except Exception:
        return False
    return getattr(member, "status", "") in ACTIVE_MEMBER


async def send_invite(bot, db: Database, cfg: Config, telegram_id: int, months: int) -> str | None:
    channel = await channel_id(db, cfg)
    if channel is None or bot is None:
        return None
    if await member_of_channel(bot, channel, telegram_id):
        return None
    expire = int((utcnow() + timedelta(days=7)).timestamp())
    link = await bot.create_chat_invite_link(
        channel,
        name=f"Private {months}м {telegram_id}"[:32],
        expire_date=expire,
        member_limit=1,
    )
    url = link.invite_link
    sub = await db.subscription(telegram_id)
    if sub:
        sub["invite_link"] = url
        await db.save_subscription(sub)
    return url


async def remove_from_channel(bot, db: Database, cfg: Config, telegram_id: int) -> None:
    channel = await channel_id(db, cfg)
    if channel is None or bot is None:
        return
    sub = await db.subscription(telegram_id)
    if sub and sub.get("invite_link"):
        try:
            await bot.revoke_chat_invite_link(channel, sub["invite_link"])
        except Exception:
            log.info("invite revoke failed for %s", telegram_id)
    try:
        # unban снимает человека и не оставляет его в чёрном списке.
        await bot.unban_chat_member(channel, telegram_id)
    except Exception:
        log.info("remove from channel failed for %s", telegram_id)


async def start_checkout(
    db: Database,
    tbank: TBank | None,
    cfg: Config,
    user: dict,
    bot_username: str | None,
) -> tuple[str, int]:
    months = int(user["pending_months"])
    plan, amount, promo = await quoted_price(db, user, months)
    if tbank is None:
        raise TBankError("Терминал Т-Банка ещё не подключён.")
    email = user.get("email")
    if not email:
        raise ValueError("Нужна почта для чека.")
    order_id = order_id_for(months, int(user["telegram_id"]))
    now = utcnow()
    if promo is not None:
        if promo.get("shared"):
            held = await db.reserve_shared_promo(
                promo["code"],
                int(user["telegram_id"]),
                order_id,
                iso(now + PROMO_HOLD),
            )
        else:
            held = await db.reserve_promo(
                promo["code"],
                int(user["telegram_id"]),
                order_id,
                iso(now + PROMO_HOLD),
                iso(now),
            )
        if not held:
            raise ValueError("Промокод уже использован.")
    await db.create_payment(
        {
            "order_id": order_id,
            "payment_id": None,
            "telegram_id": int(user["telegram_id"]),
            "months": months,
            "amount": amount,
            "email": email,
            "promo_code": None if promo is None else promo["code"],
            "is_renewal": 0,
            "status": "new",
            "rebill_id": None,
            "pan": None,
            "created_at": iso(now),
            "confirmed_at": None,
        }
    )
    username = user.get("username") or ""
    success = f"https://t.me/{bot_username}?start=paid" if bot_username else None
    fail = f"https://t.me/{bot_username}?start=fail" if bot_username else None
    common = dict(
        order_id=order_id,
        amount=amount,
        description=f"Экономизм Private, {plan['title']}",
        customer_key=str(user["telegram_id"]),
        email=email,
        months=months,
        extra={
            "telegram_id": str(user["telegram_id"]),
            "username": username[:32],
            "months": str(months),
            "email": email,
        },
        success_url=success,
        fail_url=fail,
    )
    try:
        if cfg.save_card:
            try:
                result = await tbank.init_payment(**common, recurrent=True, operation_initiator="1")
            except TBankError as exc:
                if exc.code != "3013":
                    raise
                log.warning("recurrent is disabled at the terminal, payment without saved card")
                result = await tbank.init_payment(**common, recurrent=False, operation_initiator="0")
        else:
            result = await tbank.init_payment(**common, recurrent=False, operation_initiator="0")
    except Exception:
        await db.mark_payment(order_id, "failed")
        await db.release_promo(order_id)
        raise
    payment_id = str(result.get("PaymentId") or "")
    await db.bind_payment_id(order_id, payment_id)
    url = result.get("PaymentURL")
    if not url:
        raise TBankError("Банк не вернул ссылку на оплату.")
    return str(url), amount


async def apply_note(db: Database, bot, cfg: Config, note: BankNote) -> None:
    payment = None
    if note.order_id:
        payment = await db.payment_by_order(note.order_id)
    if payment is None and note.payment_id:
        payment = await db.payment_by_provider(note.payment_id)
    if payment is None:
        log.warning("unknown payment order=%s id=%s", note.order_id, note.payment_id)
        return
    if note.amount is not None and int(note.amount) != int(payment["amount"]):
        log.error("amount mismatch order=%s bank=%s ours=%s", payment["order_id"], note.amount, payment["amount"])
        return
    if note.rebill_id or note.pan:
        await db.save_card(payment["order_id"], note.rebill_id, note.pan, "authorized")
        payment = await db.payment_by_order(payment["order_id"])
    status = note.status.upper()
    if status == "CONFIRMED" and note.success:
        await confirm_payment(db, bot, cfg, payment)
    elif status in {"REJECTED", "CANCELED", "DEADLINE_EXPIRED", "AUTH_FAIL"}:
        if payment["status"] != "confirmed":
            await db.mark_payment(payment["order_id"], "failed")
            await db.release_promo(payment["order_id"])
    elif status in {"REFUNDED", "PARTIAL_REFUNDED"}:
        await refund_payment(db, bot, cfg, payment)
    elif status == "AUTHORIZED":
        if payment["status"] == "new":
            await db.mark_payment(payment["order_id"], "authorized")


async def confirm_payment(db: Database, bot, cfg: Config, payment: dict) -> None:
    now = utcnow()
    if not await db.claim_status(payment["order_id"], "confirmed", iso(now)):
        return
    payment = await db.payment_by_order(payment["order_id"])
    if payment is None:
        return
    if payment.get("promo_code"):
        promo = await db.promo(str(payment["promo_code"]))
        if promo is not None and promo.get("shared"):
            await db.consume_shared_promo(
                str(payment["promo_code"]),
                int(payment["telegram_id"]),
                str(payment["order_id"]),
                iso(now),
            )
        else:
            await db.consume_promo(payment["promo_code"], int(payment["telegram_id"]), iso(now))
    sub = await db.subscription(int(payment["telegram_id"]))
    current_end = parse_dt(sub["access_until"]) if sub else None
    start = current_end if current_end and current_end > now else now
    access_until = add_months(start, int(payment["months"]))
    rebill = payment.get("rebill_id") or (sub.get("rebill_id") if sub else None)
    pan = payment.get("pan") or (sub.get("pan") if sub else None)
    await db.save_subscription(
        {
            "telegram_id": int(payment["telegram_id"]),
            "months": int(payment["months"]),
            "price_kopecks": int(payment["amount"]),
            "status": "active",
            "autopay": 1 if rebill else 0,
            "rebill_id": rebill,
            "pan": pan,
            "access_until": iso(access_until),
            "warned_at": None,
            "charge_failed_at": None,
            "retry_at": None,
            "remove_at": None,
            "retries": 0,
            "invite_link": None if sub is None else sub.get("invite_link"),
        }
    )
    user = await db.user(int(payment["telegram_id"]))
    await db.add_ledger(
        {
            "created_at": iso(now),
            "order_id": payment["order_id"],
            "payment_id": payment.get("payment_id"),
            "telegram_id": int(payment["telegram_id"]),
            "username": None if user is None else user.get("username"),
            "email": payment.get("email"),
            "months": int(payment["months"]),
            "amount": int(payment["amount"]),
            "kind": "renewal" if payment["is_renewal"] else "payment",
            "period_start": iso(start),
            "period_end": iso(access_until),
        }
    )
    if bot is None:
        return
    link = await send_invite(bot, db, cfg, int(payment["telegram_id"]), int(payment["months"]))
    until = fmt_when(access_until)
    price = fmt_rub(int(payment["amount"]))
    if link:
        text = (
            f"Оплата {price} прошла. Доступ до {until}.\n"
            f"Ссылка одноразовая, семь дней:\n{link}"
        )
    else:
        text = f"Оплата {price} прошла. Доступ продлён до {until}."
    if rebill:
        text += "\nДальше бот предупредит за двое суток и спишет ту же сумму."
    else:
        text += "\nКарта для автосписания не сохранилась. Перед концом периода пришлю ссылку на оплату."
    await bot.send_message(int(payment["telegram_id"]), text)


async def refund_payment(db: Database, bot, cfg: Config, payment: dict) -> None:
    now = utcnow()
    if not await db.claim_status(payment["order_id"], "refunded", iso(now)):
        return
    payment = await db.payment_by_order(payment["order_id"])
    if payment is None:
        return
    user = await db.user(int(payment["telegram_id"]))
    sub = await db.subscription(int(payment["telegram_id"]))
    await db.add_ledger(
        {
            "created_at": iso(now),
            "order_id": payment["order_id"],
            "payment_id": payment.get("payment_id"),
            "telegram_id": int(payment["telegram_id"]),
            "username": None if user is None else user.get("username"),
            "email": payment.get("email"),
            "months": int(payment["months"]),
            "amount": -int(payment["amount"]),
            "kind": "refund",
            "period_start": None if sub is None else sub.get("access_until"),
            "period_end": iso(now),
        }
    )
    if sub is not None:
        sub["status"] = "expired"
        sub["autopay"] = 0
        sub["access_until"] = iso(now)
        sub["remove_at"] = None
        await db.save_subscription(sub)
    await remove_from_channel(bot, db, cfg, int(payment["telegram_id"]))
    if bot is not None:
        await bot.send_message(
            int(payment["telegram_id"]),
            f"Возврат {fmt_rub(int(payment['amount']))} отправлен. Доступ к Private закрыт.",
        )


async def grant_access(db: Database, bot, cfg: Config, telegram_id: int, months: int) -> datetime:
    now = utcnow()
    sub = await db.subscription(telegram_id)
    current_end = parse_dt(sub["access_until"]) if sub else None
    start = current_end if current_end and current_end > now else now
    access_until = add_months(start, months)
    await db.save_subscription(
        {
            "telegram_id": telegram_id,
            "months": months,
            "price_kopecks": 0,
            "status": "active",
            "autopay": 0,
            "rebill_id": None,
            "pan": None if sub is None else sub.get("pan"),
            "access_until": iso(access_until),
            "warned_at": None,
            "charge_failed_at": None,
            "retry_at": None,
            "remove_at": None,
            "retries": 0,
            "invite_link": None if sub is None else sub.get("invite_link"),
        }
    )
    user = await db.user(telegram_id)
    await db.add_ledger(
        {
            "created_at": iso(now),
            "order_id": None,
            "payment_id": None,
            "telegram_id": telegram_id,
            "username": None if user is None else user.get("username"),
            "email": None if user is None else user.get("email"),
            "months": months,
            "amount": 0,
            "kind": "grant",
            "period_start": iso(start),
            "period_end": iso(access_until),
        }
    )
    if bot is not None:
        link = await send_invite(bot, db, cfg, telegram_id, months)
        text = f"Доступ к Private открыт до {fmt_when(access_until)}."
        if link:
            text += f"\n{link}"
        await bot.send_message(telegram_id, text)
    return access_until


async def fail_charge(db: Database, bot, sub: dict, reason: str, retry: bool) -> None:
    now = utcnow()
    retries = int(sub.get("retries") or 0)
    if retry:
        retries += 1
    remove_at = parse_dt(sub.get("remove_at")) or (now + timedelta(hours=24))
    retry_at = None if retries >= 1 else now + timedelta(hours=12)
    await db.save_subscription(
        {
            **sub,
            "status": "past_due",
            "retries": retries,
            "charge_failed_at": iso(now),
            "retry_at": None if retry_at is None else iso(retry_at),
            "remove_at": iso(remove_at),
        }
    )
    if bot is None:
        return
    until = fmt_when(remove_at)
    await bot.send_message(
        int(sub["telegram_id"]),
        f"Списать {fmt_rub(int(sub['price_kopecks']))} не получилось: {reason}\n"
        f"Доступ пока сохранён. Повторю ещё раз, затем уберу из канала {until}, "
        "если оплаты так и не будет.",
    )


async def charge_subscription(db: Database, tbank: TBank, bot, cfg: Config, sub: dict, retry: bool) -> None:
    telegram_id = int(sub["telegram_id"])
    if not await db.try_lock_charge(telegram_id):
        return
    locked = await db.subscription(telegram_id)
    if locked is None:
        return
    user = await db.user(telegram_id)
    email = (user or {}).get("email")
    if not email:
        await fail_charge(db, bot, locked, "нет почты для чека", retry)
        return
    order_id = order_id_for(int(locked["months"]), telegram_id)
    now = utcnow()
    await db.create_payment(
        {
            "order_id": order_id,
            "payment_id": None,
            "telegram_id": telegram_id,
            "months": int(locked["months"]),
            "amount": int(locked["price_kopecks"]),
            "email": email,
            "promo_code": None,
            "is_renewal": 1,
            "status": "new",
            "rebill_id": locked.get("rebill_id"),
            "pan": locked.get("pan"),
            "created_at": iso(now),
            "confirmed_at": None,
        }
    )
    try:
        created = await tbank.init_payment(
            order_id=order_id,
            amount=int(locked["price_kopecks"]),
            description=f"Продление Экономизм Private, {period_title(int(locked['months']))}",
            customer_key=str(telegram_id),
            email=email,
            months=int(locked["months"]),
            recurrent=False,
            operation_initiator="R",
            extra={"telegram_id": str(telegram_id), "months": str(locked["months"]), "email": email},
            success_url=None,
            fail_url=None,
        )
        payment_id = str(created.get("PaymentId") or "")
        await db.bind_payment_id(order_id, payment_id)
        charged = await tbank.charge(payment_id, str(locked["rebill_id"]))
    except TBankError as exc:
        await db.mark_payment(order_id, "failed")
        await fail_charge(db, bot, locked, str(exc), retry)
        return
    except Exception:
        log.exception("charge failed order=%s", order_id)
        await db.mark_payment(order_id, "failed")
        await fail_charge(db, bot, locked, "банк не ответил", retry)
        return
    note = parse_note({**charged, "OrderId": order_id, "PaymentId": payment_id})
    if note.status.upper() == "CONFIRMED":
        payment = await db.payment_by_order(order_id)
        await confirm_payment(db, bot, cfg, payment)
        return
    await apply_note(db, bot, cfg, note)
    payment = await db.payment_by_order(order_id)
    if payment and payment["status"] != "confirmed":
        await fail_charge(db, bot, locked, note.status or "отказ банка", retry)


async def kick_subscription(db: Database, bot, cfg: Config, sub: dict) -> None:
    now = utcnow()
    sub["status"] = "expired"
    sub["autopay"] = 0
    sub["remove_at"] = None
    sub["updated_at"] = iso(now)
    await db.save_subscription(sub)
    await remove_from_channel(bot, db, cfg, int(sub["telegram_id"]))
    if bot is None:
        return
    await bot.send_message(
        int(sub["telegram_id"]),
        "Доступ к Private закрыт. Вы вышли из канала и можете вернуться после оплаты.\n"
        "Откройте /start и выберите срок.",
    )


async def sync_payment(db: Database, tbank: TBank, bot, cfg: Config, payment: dict) -> None:
    if not payment.get("payment_id"):
        return
    try:
        state = await tbank.get_state(str(payment["payment_id"]))
    except TBankError as exc:
        log.info("getstate %s: %s", payment["order_id"], exc)
        return
    note = parse_note({**state, "OrderId": payment["order_id"]})
    await apply_note(db, bot, cfg, note)
