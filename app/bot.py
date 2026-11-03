from __future__ import annotations

import html
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, ErrorEvent, FSInputFile, Message, MessageOriginChannel

from app.db import Database
from app.pricing import (
    MOSCOW,
    current_price,
    fmt_rub,
    fmt_when,
    iso,
    on_sale,
    parse_dt,
    period_title,
    utcnow,
)
from app.keyboards import admin_kb, confirm_kb, pay_kb, status_kb, tariffs_kb
from app.runtime import Runtime
from app.service import (
    channel_id,
    grant_access,
    member_of_channel,
    promo_problem,
    quoted_price,
    send_invite,
    start_checkout,
    sync_payment,
)
from app.tbank import TBankError, parse_note

log = logging.getLogger("bot")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE = re.compile(r"^[A-Z0-9_-]{3,32}$")


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def is_admin(runtime: Runtime, user_id: int | None) -> bool:
    return user_id is not None and user_id in runtime.cfg.admin_ids


async def touch(runtime: Runtime, message: Message) -> dict | None:
    if message.chat.type != "private" or message.from_user is None:
        return None
    return await runtime.db.upsert_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )


async def show_tariffs(runtime: Runtime, target: Message | CallbackQuery) -> None:
    plans = await runtime.db.plans()
    text = (
        "<b>Экономизм Private</b>\n\n"
        "Закрытый контур: портфель, разборы, эфиры. Выберите срок.\n"
        "Оплата картой или СБП. Чек придёт на почту. "
        "За двое суток до продления бот предупредит."
    )
    markup = tariffs_kb(plans, utcnow())
    if isinstance(target, CallbackQuery):
        await target.answer()
        if target.message is not None:
            await target.message.answer(text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


async def show_confirm(runtime: Runtime, message: Message, user: dict) -> None:
    months = user.get("pending_months")
    if not months:
        await show_tariffs(runtime, message)
        return
    try:
        plan, amount, promo = await quoted_price(runtime.db, user, int(months))
    except ValueError as exc:
        await message.answer(esc(exc))
        user = await runtime.db.user(int(user["telegram_id"]))
        if user is None:
            return
        plan = await runtime.db.plan(int(months))
        if plan is None:
            return
        amount = current_price(plan, utcnow())
        promo = None
    else:
        plan_row = plan
        plan = plan_row
    sale = on_sale(plan, utcnow())
    lines = [
        f"<b>{esc(plan['title'])}</b> — {fmt_rub(amount)}",
        f"Чек отправим на {esc(user.get('email'))}.",
        "Дальше эта же сумма списывается на тот же срок. "
        "За двое суток придёт предупреждение, автопродление отменяется здесь.",
    ]
    if sale and promo is None:
        lines.append(f"Обычная цена {fmt_rub(int(plan['price_kopecks']))}, сейчас акция.")
    if promo is not None:
        if promo.get("shared"):
            lines.append(f"Промокод {esc(promo['code'])} применён.")
        else:
            lines.append(f"Промокод {esc(promo['code'])} применён. После оплаты он сгорает.")
    await message.answer("\n".join(lines), reply_markup=confirm_kb())


def rub_to_kopecks(text: str) -> int:
    cleaned = text.replace(" ", "").replace("₽", "").replace(",", ".")
    if "." in cleaned:
        rub, kop = cleaned.split(".", 1)
        return int(rub) * 100 + int((kop + "00")[:2])
    return int(cleaned) * 100


def end_of_day(text: str) -> datetime:
    day, month, year = text.split(".")
    if len(year) == 2:
        year = "20" + year
    return datetime(int(year), int(month), int(day), 23, 59, tzinfo=MOSCOW)


async def prices_text(db: Database) -> str:
    lines = ["<b>Цены</b>"]
    now = utcnow()
    for plan in await db.plans():
        line = f"{plan['months']} мес. — {fmt_rub(current_price(plan, now))}"
        if on_sale(plan, now):
            until = parse_dt(plan["sale_until"])
            when = fmt_when(until) if until else "—"
            line += f"\nакция до {when}, обычная {fmt_rub(int(plan['price_kopecks']))}"
        lines.append(line)
    lines.append("")
    lines.append("/price 12 37990 — обычная цена")
    lines.append("/price 12 28490 10.11.2026 — акция до конца этого дня")
    lines.append("/price 12 clear — убрать акцию")
    return "\n".join(lines)


async def promos_text(db: Database) -> str:
    rows = await db.promos()
    counts = await db.shared_use_counts()
    lines = ["<b>Промокоды</b>"]
    if not rows:
        lines.append("Пока нет.")
    for promo in rows:
        if promo["disabled"]:
            state = "выключен"
        elif promo.get("shared"):
            used = counts.get(str(promo["code"]), 0)
            state = f"оплатили {used}" if used else "ещё никто не оплатил"
        elif promo["used_by"]:
            state = "использован"
        else:
            state = "активен"
        audience = "общий" if promo.get("shared") else "личный"
        scope = period_title(int(promo["months"])) if promo["months"] else "все сроки"
        value = f"{promo['value']}%" if promo["kind"] == "percent" else fmt_rub(int(promo["value"]))
        lines.append(f"{esc(promo['code'])} — {value}, {scope}, {audience}, {state}")
    lines.append("")
    lines.append("/promo общий NEWYEAR 20% — для всех, каждый один раз")
    lines.append("/promo общий NEWYEAR 20% 12 30.11.2026 — только 12 месяцев, до конца дня")
    lines.append("/promo личный GIFT 28490 — для одного человека")
    lines.append("/promo off NEWYEAR — выключить")
    return "\n".join(lines)


ADMIN_HELP = (
    "<b>Админка</b>\n"
    "Кнопки ниже открывают цены, промокоды, сводку и реестр. "
    "Менять цифры нужно командой.\n\n"
    "/price — цены и акции\n"
    "/promo — общий код для всех или личный для одного человека\n"
    "/grant 123456789 12 — выдать срок без оплаты\n"
    "/who 123456789 — карточка человека, можно и @ник\n"
    "/stats — сколько доступов и какая сумма в реестре\n"
    "/export — файл реестра\n"
    "/refund id — вернуть платёж, когда будет подключён Т-Банк\n\n"
    "Канал запоминается сам, когда вы назначаете бота администратором Private."
)


def build_router(runtime: Runtime) -> Router:
    router = Router()
    db: Database = runtime.db

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        user = await touch(runtime, message)
        if user is None:
            return
        if command.args == "paid":
            await message.answer("Платёж проверяю. Если ссылка на канал не пришла, нажмите «Я оплатил».")
        elif command.args == "fail":
            await message.answer("Оплата не прошла. Можно выбрать срок ещё раз.")
        await show_tariffs(runtime, message)

    @router.message(Command("id"))
    async def my_id(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None:
            return
        await message.answer(f"Ваш Telegram id: <code>{user['telegram_id']}</code>")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None:
            return
        sub = await db.subscription(int(user["telegram_id"]))
        until = parse_dt(sub["access_until"]) if sub else None
        if sub is None or until is None or sub["status"] == "expired" or until <= utcnow():
            await message.answer("Сейчас доступа в Private нет.", reply_markup=tariffs_kb(await db.plans(), utcnow()))
            return
        renewal = "включено" if sub["autopay"] and sub.get("rebill_id") else "выключено"
        price = fmt_rub(int(sub["price_kopecks"])) if int(sub["price_kopecks"]) else "без оплаты"
        lines = [
            f"Доступ до {fmt_when(until)}.",
            f"Тариф: {period_title(int(sub['months']))}, продление {price}.",
            f"Автопродление {renewal}.",
        ]
        link = await send_invite(
            message.bot, runtime.db, runtime.cfg, int(user["telegram_id"]), int(sub["months"])
        )
        channel = await channel_id(runtime.db, runtime.cfg)
        inside = channel is not None and await member_of_channel(message.bot, channel, int(user["telegram_id"]))
        if link:
            lines.append(f"Ссылка в канал, одноразовая, семь дней:\n{link}")
        elif not inside:
            lines.append("Ссылку в канал сейчас выпустить не удалось. Напишите ещё раз.")
        await message.answer(
            "\n".join(lines),
            reply_markup=status_kb(bool(sub["autopay"] and sub.get("rebill_id"))),
        )

    @router.message(Command("stop"))
    async def stop(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None:
            return
        await db.set_autopay(int(user["telegram_id"]), False)
        await message.answer("Автопродление выключено. Оплаченный срок не обрывается.")

    @router.callback_query(F.data == "plans")
    async def on_plans(query: CallbackQuery) -> None:
        if query.from_user is None:
            return
        await db.upsert_user(query.from_user.id, query.from_user.username, query.from_user.first_name)
        await show_tariffs(runtime, query)

    @router.callback_query(F.data.startswith("plan:"))
    async def on_plan(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            return
        await query.answer()
        user = await db.upsert_user(query.from_user.id, query.from_user.username, query.from_user.first_name)
        months = int((query.data or "").split(":", 1)[1])
        await db.set_pending(query.from_user.id, months, None)
        user["pending_months"] = months
        user["pending_promo"] = None
        if not user.get("email"):
            await db.set_awaiting(query.from_user.id, "email")
            await query.message.answer("Напишите почту для чека. Телефон не нужен.")
            return
        await db.set_awaiting(query.from_user.id, None)
        await show_confirm(runtime, query.message, user)

    @router.callback_query(F.data == "promo:ask")
    async def ask_promo(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            return
        await query.answer()
        await db.set_awaiting(query.from_user.id, "promo")
        await query.message.answer("Напишите промокод одним словом.")

    @router.callback_query(F.data == "pay:go")
    async def pay(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            return
        await query.answer()
        user = await db.user(query.from_user.id)
        if user is None or not user.get("pending_months"):
            await query.message.answer("Сначала выберите срок: /start")
            return
        try:
            url, amount = await start_checkout(runtime.db, runtime.tbank, runtime.cfg, user, runtime.username)
        except (ValueError, TBankError) as exc:
            await query.message.answer(esc(exc))
            return
        await query.message.answer(
            f"Ссылка на {fmt_rub(amount)}. Она действует один час.",
            reply_markup=pay_kb(url),
        )

    @router.callback_query(F.data == "paid:check")
    async def paid_check(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            return
        await query.answer()
        payment = await db.latest_payment(query.from_user.id)
        if payment is None:
            await query.message.answer("Платежа пока нет.")
            return
        if payment["status"] != "confirmed" and runtime.tbank is not None and payment.get("payment_id"):
            await sync_payment(runtime.db, runtime.tbank, query.bot, runtime.cfg, payment)
            payment = await db.payment_by_order(payment["order_id"])
        if payment and payment["status"] == "confirmed":
            link = await send_invite(
                query.bot, runtime.db, runtime.cfg, query.from_user.id, int(payment["months"])
            )
            channel = await channel_id(runtime.db, runtime.cfg)
            inside = channel is not None and await member_of_channel(query.bot, channel, query.from_user.id)
            if link:
                await query.message.answer(f"Оплата проведена. Ссылка одноразовая, семь дней:\n{link}")
            elif inside:
                await query.message.answer("Оплата проведена, вы уже в канале.")
            else:
                await query.message.answer("Оплата проведена, но ссылку выпустить не удалось. Напишите ещё раз.")
            return
        await query.message.answer("Банк ещё не подтвердил оплату. Если деньги списались, нажмите ещё раз через минуту.")

    @router.callback_query(F.data.in_({"auto:off", "auto:keep"}))
    async def autopay(query: CallbackQuery) -> None:
        if query.from_user is None:
            return
        enabled = query.data == "auto:keep"
        await db.set_autopay(query.from_user.id, enabled)
        await query.answer("Автопродление оставлено" if enabled else "Автопродление выключено")
        if query.message is not None:
            text = (
                "Автопродление оставлено. Спишу сумму тарифа в срок."
                if enabled
                else "Автопродление выключено. Доступ сохранится до конца оплаченного периода."
            )
            await query.message.answer(text)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None or message.text is None:
            return
        if await remember_channel(runtime, message):
            return
        text = message.text.strip()
        if user.get("awaiting") == "email":
            if not EMAIL.match(text):
                await message.answer("Это не похоже на почту. Пример: name@mail.ru")
                return
            email = text.lower()
            await db.set_email(int(user["telegram_id"]), email)
            await db.set_awaiting(int(user["telegram_id"]), None)
            user["email"] = email
            await message.answer(f"Почта {esc(email)} сохранена.")
            await show_confirm(runtime, message, user)
            return
        if user.get("awaiting") == "promo":
            code = text.upper()
            if not CODE.match(code):
                await message.answer("Промокод — одно слово латиницей, без пробелов.")
                return
            months = user.get("pending_months")
            if not months:
                await db.set_awaiting(int(user["telegram_id"]), None)
                await show_tariffs(runtime, message)
                return
            promo = await db.promo(code)
            problem = await promo_problem(db, promo, int(months), int(user["telegram_id"]), utcnow())
            if problem:
                await message.answer(esc(problem))
                return
            await db.set_pending(int(user["telegram_id"]), int(months), code)
            await db.set_awaiting(int(user["telegram_id"]), None)
            user["pending_promo"] = code
            await show_confirm(runtime, message, user)
            return
        await show_tariffs(runtime, message)

    @router.message(Command("admin"))
    async def admin_help(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None or not is_admin(runtime, message.from_user.id if message.from_user else None):
            return
        await message.answer(ADMIN_HELP, reply_markup=admin_kb())

    @router.callback_query(F.data.startswith("admin:"))
    async def admin_panel(query: CallbackQuery) -> None:
        if query.from_user is None or not is_admin(runtime, query.from_user.id):
            await query.answer("Недоступно", show_alert=True)
            return
        action = (query.data or "").split(":", 1)[1]
        if action == "prices":
            await query.answer()
            if query.message is not None:
                await query.message.answer(await prices_text(db), reply_markup=admin_kb())
            return
        if action == "promos":
            await query.answer()
            if query.message is not None:
                await query.message.answer(await promos_text(db), reply_markup=admin_kb())
            return
        if action == "stats":
            await query.answer()
            stats = await db.stats()
            if query.message is not None:
                await query.message.answer(
                    f"Активных доступов: {stats['active']}\n"
                    f"Сумма реестра без возвратов: {fmt_rub(int(stats['paid']))}",
                    reply_markup=admin_kb(),
                )
            return
        if action == "export":
            await query.answer()
            if query.message is not None:
                await send_ledger(query.message, db)
            return
        await query.answer()
        if query.message is not None:
            await query.message.answer(ADMIN_HELP, reply_markup=admin_kb())

    @router.message(Command("price"))
    async def price_cmd(message: Message, command: CommandObject) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        args = (command.args or "").split()
        if not args:
            await message.answer(await prices_text(db), reply_markup=admin_kb())
            return
        try:
            months = int(args[0])
        except ValueError:
            await message.answer("Тариф: 1, 3, 6 или 12.")
            return
        if await db.plan(months) is None:
            await message.answer("Тариф: 1, 3, 6 или 12.")
            return
        if len(args) == 1:
            await message.answer("Нужна цена в рублях.")
            return
        if args[1].lower() == "clear":
            await db.set_sale(months, None, None)
            await message.answer(f"Акция на {period_title(months)} выключена.")
            return
        try:
            amount = rub_to_kopecks(args[1])
            until = end_of_day(args[2]) if len(args) >= 3 else None
        except ValueError:
            await message.answer("Цена — число в рублях. Дата акции — ДД.ММ.ГГГГ.")
            return
        if until is not None:
            await db.set_sale(months, amount, iso(until))
            await message.answer(f"Акция: {period_title(months)} за {fmt_rub(amount)} до {fmt_when(until)}.")
            return
        await db.set_price(months, amount)
        await message.answer(f"Обычная цена {period_title(months)}: {fmt_rub(amount)}.")

    @router.message(Command("promo"))
    async def promo_cmd(message: Message, command: CommandObject) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        args = (command.args or "").split()
        if not args:
            await message.answer(await promos_text(db), reply_markup=admin_kb())
            return
        if args[0].lower() == "off" and len(args) == 2:
            await db.disable_promo(args[1])
            await message.answer(f"Промокод {esc(args[1].upper())} выключен.")
            return
        mode = args[0].lower()
        if mode in {"общий", "общая", "общее", "общие"}:
            shared = True
        elif mode in {"личный", "личная", "личное", "личные"}:
            shared = False
        else:
            await message.answer(
                "Укажите тип.\n"
                "/promo общий NEWYEAR 20% — для всех, каждый один раз\n"
                "/promo личный GIFT 28490 — для одного человека"
            )
            return
        args = args[1:]
        if not args:
            await message.answer("Формат: /promo общий NEWYEAR 20% или /promo личный GIFT 28490")
            return
        code = args[0].upper()
        if not CODE.match(code) or len(args) < 2:
            await message.answer("Формат: /promo общий NEWYEAR 20% или /promo личный GIFT 28490")
            return
        raw_value = args[1]
        months = None
        expires = None
        rest = args[2:]
        if rest and rest[0].isdigit():
            months = int(rest.pop(0))
            if await db.plan(months) is None:
                await message.answer("Тариф: 1, 3, 6 или 12.")
                return
        try:
            if rest:
                expires = iso(end_of_day(rest[0]))
            if raw_value.endswith("%"):
                kind = "percent"
                value = int(raw_value[:-1])
                if not 1 <= value <= 99:
                    await message.answer("Процент от 1 до 99.")
                    return
            else:
                kind = "final"
                value = rub_to_kopecks(raw_value)
        except ValueError:
            await message.answer("Формат: /promo общий NEWYEAR 20% 12 30.11.2026")
            return
        try:
            await db.create_promo(code, kind, value, months, expires, shared)
        except Exception:
            await message.answer("Такой код уже есть.")
            return
        if shared:
            await message.answer(f"Общий промокод {esc(code)} создан. Каждый может оплатить им один раз.")
        else:
            await message.answer(f"Личный промокод {esc(code)} создан. Сработает у одного человека.")

    @router.message(Command("grant"))
    async def grant_cmd(message: Message, command: CommandObject) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        args = (command.args or "").split()
        if len(args) != 2:
            await message.answer("Формат: /grant 123456789 12")
            return
        try:
            months = int(args[1])
        except ValueError:
            await message.answer("Формат: /grant 123456789 12")
            return
        if await db.plan(months) is None:
            await message.answer("Тариф: 1, 3, 6 или 12.")
            return
        if args[0].startswith("@"):
            person = await db.user_by_username(args[0])
            if person is None:
                await message.answer("Человек ещё не нажимал /start в боте.")
                return
            telegram_id = int(person["telegram_id"])
        else:
            telegram_id = int(args[0])
            if await db.user(telegram_id) is None:
                await db.upsert_user(telegram_id, None, None)
        until = await grant_access(db, message.bot, runtime.cfg, telegram_id, months)
        await message.answer(f"Доступ {telegram_id} до {fmt_when(until)}. В реестре сумма 0.")

    @router.message(Command("refund"))
    async def refund_cmd(message: Message, command: CommandObject) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        token = (command.args or "").strip()
        if runtime.tbank is None or not token:
            await message.answer("Нужны терминал и id платежа.")
            return
        payment = await db.payment_by_provider(token) or await db.payment_by_order(token)
        if payment is None or not payment.get("payment_id"):
            await message.answer("Платёж не найден.")
            return
        try:
            result = await runtime.tbank.cancel(str(payment["payment_id"]))
        except TBankError as exc:
            await message.answer(esc(exc))
            return
        note = parse_note({**result, "OrderId": payment["order_id"], "PaymentId": payment["payment_id"]})
        from app.service import apply_note

        await apply_note(db, message.bot, runtime.cfg, note)
        await message.answer(f"Банк ответил статусом {esc(note.status)}.")

    @router.message(Command("who"))
    async def who_cmd(message: Message, command: CommandObject) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        token = (command.args or "").strip()
        if not token:
            await message.answer("Формат: /who 123456789 или /who @nick")
            return
        try:
            person = await db.user_by_username(token) if token.startswith("@") else await db.user(int(token))
        except ValueError:
            await message.answer("Формат: /who 123456789 или /who @nick")
            return
        if person is None:
            await message.answer("Не найден. Нужен /start в боте.")
            return
        sub = await db.subscription(int(person["telegram_id"]))
        until = parse_dt(sub["access_until"]) if sub else None
        access = fmt_when(until) if until else "нет"
        await message.answer(
            f"id <code>{person['telegram_id']}</code>\n"
            f"ник {esc(person.get('username'))}\n"
            f"почта {esc(person.get('email'))}\n"
            f"доступ до {access}\n"
            f"статус {esc(sub['status']) if sub else 'нет'}"
        )

    @router.message(Command("stats"))
    async def stats_cmd(message: Message) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        stats = await db.stats()
        await message.answer(
            f"Активных доступов: {stats['active']}\n"
            f"Сумма реестра без возвратов: {fmt_rub(int(stats['paid']))}"
        )

    @router.message(Command("export"))
    async def export_cmd(message: Message) -> None:
        if message.from_user is None or not is_admin(runtime, message.from_user.id):
            return
        await send_ledger(message, db)

    @router.message(F.forward_origin | F.forward_from_chat)
    async def bind_channel(message: Message) -> None:
        await remember_channel(runtime, message)

    @router.my_chat_member()
    async def bot_added_to_channel(event: ChatMemberUpdated) -> None:
        log.info(
            "bot status in %s (%s) is %s, changed by %s",
            event.chat.id,
            event.chat.type,
            event.new_chat_member.status,
            event.from_user.id,
        )
        if event.chat.type != "channel":
            return
        if event.new_chat_member.status not in {"administrator", "creator"}:
            return
        if not await owner_runs_channel(event.bot, runtime, event.chat.id, event.from_user.id):
            return
        notify = event.from_user.id if is_admin(runtime, event.from_user.id) else next(iter(runtime.cfg.admin_ids))
        await save_channel(runtime, event.bot, event.chat.id, event.chat.title or "", notify)

    @router.message()
    async def fallback(message: Message) -> None:
        user = await touch(runtime, message)
        if user is None:
            return
        await show_tariffs(runtime, message)

    @router.error()
    async def on_error(event: ErrorEvent) -> None:
        log.exception("update failed", exc_info=event.exception)
        message = event.update.message
        if message is None and event.update.callback_query is not None:
            message = event.update.callback_query.message
        if message is not None:
            try:
                await message.answer("Не вышло обработать сообщение. Нажмите /start.")
            except Exception:
                log.info("error reply was not delivered")

    @router.channel_post()
    async def remember_from_post(message: Message) -> None:
        if runtime.cfg.channel_id or await runtime.db.setting("channel_id") or message.bot is None:
            return
        if not await owner_runs_channel(message.bot, runtime, message.chat.id, None):
            return
        await save_channel(
            runtime,
            message.bot,
            message.chat.id,
            message.chat.title or "",
            next(iter(runtime.cfg.admin_ids)),
        )

    return router


async def owner_runs_channel(bot: Bot, runtime: Runtime, chat_id: int, actor_id: int | None) -> bool:
    if is_admin(runtime, actor_id):
        return True
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception:
        log.exception("cannot read admins of %s", chat_id)
        return False
    return any(is_admin(runtime, member.user.id) for member in admins)


async def send_ledger(message: Message, db: Database) -> None:
    if await db.ledger_count() == 0:
        await message.answer("Реестр пока пустой. Первая строка появится после оплаты или /grant.")
        return
    path = await db.ledger_csv()
    await message.answer_document(FSInputFile(path), caption="Реестр: почта, ник, срок, сумма, период.")


async def save_channel(runtime: Runtime, bot: Bot, chat_id: int, title: str, notify_id: int) -> None:
    if runtime.cfg.channel_id and runtime.cfg.channel_id != chat_id:
        await bot.send_message(notify_id, "В настройках уже указан другой CHANNEL_ID. Он и используется.")
        return
    await runtime.db.set_setting("channel_id", str(chat_id))
    await bot.send_message(notify_id, f"Канал запомнен: {esc(title)} <code>{chat_id}</code>")


async def remember_channel(runtime: Runtime, message: Message) -> bool:
    if message.from_user is None or not is_admin(runtime, message.from_user.id) or message.bot is None:
        return False
    origin = message.forward_origin
    chat_id = None
    title = ""
    if isinstance(origin, MessageOriginChannel):
        chat_id = origin.chat.id
        title = origin.chat.title or ""
    elif message.forward_from_chat is not None and message.forward_from_chat.type == "channel":
        chat_id = message.forward_from_chat.id
        title = message.forward_from_chat.title or ""
    if chat_id is None:
        return False
    await save_channel(runtime, message.bot, chat_id, title, message.from_user.id)
    return True
