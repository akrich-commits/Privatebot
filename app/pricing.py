from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))
MIN_KOPECKS = 1_000
WARN_BEFORE = timedelta(hours=48)
REMOVE_AFTER = timedelta(hours=24)
RETRY_AFTER = timedelta(hours=12)
PROMO_HOLD = timedelta(hours=1)
LINK_TTL = timedelta(hours=1)

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def fmt_rub(kopecks: int) -> str:
    sign = "−" if kopecks < 0 else ""
    kopecks = abs(int(kopecks))
    rub, kop = divmod(kopecks, 100)
    body = f"{rub:,}".replace(",", " ")
    if kop:
        return f"{sign}{body},{kop:02d} ₽"
    return f"{sign}{body} ₽"


def fmt_when(dt: datetime) -> str:
    local = dt.astimezone(MOSCOW)
    return f"{local.day} {_MONTHS[local.month - 1]}, {local:%H:%M} мск"


def add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def current_price(plan: dict, now: datetime) -> int:
    sale_until = parse_dt(plan.get("sale_until"))
    sale = plan.get("sale_price_kopecks")
    if sale and sale_until and sale_until > now:
        return int(sale)
    return int(plan["price_kopecks"])


def on_sale(plan: dict, now: datetime) -> bool:
    return current_price(plan, now) != int(plan["price_kopecks"])


def apply_promo(base: int, promo: dict | None) -> int:
    if not promo:
        return base
    if promo["kind"] == "percent":
        discounted = base * (100 - int(promo["value"])) // 100
    elif promo["kind"] == "final":
        discounted = int(promo["value"])
    else:
        raise ValueError("неизвестный тип промокода")
    if discounted < MIN_KOPECKS:
        raise ValueError("после скидки сумма меньше 10 ₽")
    if discounted > base:
        raise ValueError("промокод не снижает цену")
    return discounted


def period_title(months: int) -> str:
    if months == 1:
        return "1 месяц"
    if months in (3,):
        return "3 месяца"
    return f"{months} месяцев"
