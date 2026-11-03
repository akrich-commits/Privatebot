from __future__ import annotations

from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.pricing import current_price, fmt_rub, on_sale


def tariffs_kb(plans: list[dict], now: datetime) -> InlineKeyboardMarkup:
    rows = []
    for plan in plans:
        price = current_price(plan, now)
        label = f"{plan['title']} — {fmt_rub(price)}"
        if on_sale(plan, now):
            label += " · акция"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"plan:{plan['months']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", callback_data="pay:go")],
            [
                InlineKeyboardButton(text="Промокод", callback_data="promo:ask"),
                InlineKeyboardButton(text="Другой срок", callback_data="plans"),
            ],
        ]
    )


def pay_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти к оплате", url=url)],
            [InlineKeyboardButton(text="Я оплатил", callback_data="paid:check")],
        ]
    )


def warning_kb(sub: dict) -> InlineKeyboardMarkup | None:
    if sub.get("autopay") and sub.get("rebill_id") and int(sub["price_kopecks"]) > 0:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Оставить", callback_data="auto:keep"),
                    InlineKeyboardButton(text="Отменить продление", callback_data="auto:off"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Выбрать тариф", callback_data="plans")]]
    )


def admin_kb(site_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Цены", callback_data="admin:prices"),
            InlineKeyboardButton(text="Промокоды", callback_data="admin:promos"),
        ],
        [
            InlineKeyboardButton(text="Сводка", callback_data="admin:stats"),
            InlineKeyboardButton(text="Реестр", callback_data="admin:export"),
        ],
        [InlineKeyboardButton(text="Команды", callback_data="admin:help")],
    ]
    if site_url:
        rows.append([InlineKeyboardButton(text="Промокоды на сайте", url=site_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_kb(autopay: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Тарифы", callback_data="plans")]]
    if autopay:
        rows.append([InlineKeyboardButton(text="Отменить автопродление", callback_data="auto:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
