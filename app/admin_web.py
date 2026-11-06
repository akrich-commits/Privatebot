from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets
from datetime import datetime
from pathlib import Path

from aiohttp import web
from yarl import URL

from app.digest import tell_admins
from app.pricing import MOSCOW, current_price, fmt_rub, fmt_when, iso, parse_dt, period_title, utcnow
from app.runtime import Runtime
from app.sheets import KINDS, write_ledger

log = logging.getLogger("admin-web")
CODE = re.compile(r"^[A-Z0-9_-]{3,32}$")
COOKIE = "private_admin"

PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Админка Private</title>
<style>
  :root {{ color-scheme: light; --ink: #1c1915; --muted: #6b645c; --line: #e4ddd4; --paper: #f7f4ef; --card: #fff; --accent: #8c3a2f; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 16px/1.45 "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif; color: var(--ink); background: var(--paper); }}
  main {{ max-width: 1080px; margin: 0 auto; padding: 28px 18px 64px; }}
  h1 {{ font-size: 32px; font-weight: 560; letter-spacing: -0.03em; margin: 0 0 8px; }}
  h2 {{ font-size: 20px; margin: 28px 0 12px; }}
  p {{ margin: 0 0 12px; }}
  a {{ color: inherit; }}
  .muted {{ color: var(--muted); }}
  nav {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0 22px; }}
  nav a {{ text-decoration: none; border: 1px solid var(--line); background: var(--card); border-radius: 999px; padding: 8px 14px; }}
  nav a.here {{ background: var(--ink); color: #fff; border-color: var(--ink); }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 0 0 18px; }}
  .stat {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; }}
  .stat b {{ display: block; font-size: 22px; font-weight: 560; letter-spacing: -0.03em; }}
  .stat span {{ color: var(--muted); font-size: 14px; }}
  form, .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }}
  form.search {{ display: flex; gap: 8px; align-items: center; padding: 10px; }}
  form.search input {{ margin: 0; }}
  form.search button {{ margin: 0; white-space: nowrap; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 10px 8px; border-top: 1px solid var(--line); vertical-align: top; }}
  th {{ font-size: 13px; color: var(--muted); font-weight: 500; }}
  tr:first-child th {{ border-top: 0; }}
  .person {{ font-weight: 560; text-decoration: none; }}
  dl {{ display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; margin: 0; }}
  dt {{ color: var(--muted); }}
  dd {{ margin: 0; }}
  @media (max-width: 720px) {{
    table, thead, tbody, tr, th, td {{ display: block; }}
    thead {{ display: none; }}
    tr {{ padding: 8px 0; border-top: 1px solid var(--line); }}
    td {{ border: 0; padding: 2px 0; }}
    dl {{ grid-template-columns: 1fr; }}
  }}
  label {{ display: block; font-size: 14px; margin: 12px 0 4px; }}
  input, select, button {{ font: 16px/1.3 inherit; color: inherit; }}
  input, select {{ width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }}
  .row {{ display: grid; gap: 12px; }}
  @media (min-width: 700px) {{ .row.two {{ grid-template-columns: 1fr 1fr; }} }}
  button, .link {{ display: inline-block; margin-top: 14px; background: var(--ink); color: #fff; border: 0; border-radius: 999px; padding: 10px 16px; text-decoration: none; cursor: pointer; }}
  button.quiet {{ background: transparent; color: var(--accent); padding: 0; margin: 0; }}
  form.inline {{ background: none; border: 0; padding: 0; margin: 0; display: inline; }}
  .actions {{ display: flex; gap: 14px; align-items: center; flex-shrink: 0; }}
  .note {{ background: #f3ece4; border-radius: 12px; padding: 12px 14px; margin: 0 0 16px; }}
  .error {{ color: var(--accent); }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; padding: 12px 0; border-top: 1px solid var(--line); }}
  li:first-child {{ border-top: 0; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; }}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def _key(runtime: Runtime) -> bytes:
    raw = f"{runtime.cfg.admin_password}|{runtime.cfg.bot_token}".encode()
    return hashlib.sha256(raw).digest()


def sign(runtime: Runtime, nonce: str) -> str:
    mac = hmac.new(_key(runtime), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{mac}"


def token_ok(runtime: Runtime, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, mac = token.rsplit(".", 1)
    expected = hmac.new(_key(runtime), nonce.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


def passwords_match(given: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def _back(
    path: str = "/admin/promos",
    *,
    error: str = "",
    saved: str = "",
    off: str = "",
    deleted: str = "",
) -> web.HTTPFound:
    query = {}
    if error:
        query["error"] = error
    if saved:
        query["saved"] = saved
    if off:
        query["off"] = off
    if deleted:
        query["deleted"] = deleted
    return web.HTTPFound(str(URL(path).with_query(query)))


def _page(body: str) -> web.Response:
    return web.Response(text=PAGE.format(body=body), content_type="text/html", charset="utf-8")


def _login(error: str = "") -> str:
    note = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return (
        "<h1>Админка Private</h1>"
        '<p class="muted">Клиенты, сроки доступа, оплаты и промокоды.</p>'
        f"{note}"
        '<form method="post" action="/admin/login">'
        "<label for=\"password\">Пароль</label>"
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        "<button type=\"submit\">Войти</button>"
        "</form>"
    )


def _nav(here: str) -> str:
    items = (
        ("/admin", "clients", "Клиенты"),
        ("/admin/payments", "payments", "Оплаты"),
        ("/admin/promos", "promos", "Промокоды"),
        ("/admin/reestr.xlsx", "excel", "Реестр Excel"),
    )
    links = []
    for href, key, title in items:
        mark = ' class="here"' if key == here else ""
        links.append(f'<a href="{href}"{mark}>{title}</a>')
    return "<nav>" + "".join(links) + "</nav>"


def _when(value: str | None) -> str:
    moment = parse_dt(value)
    return html.escape(fmt_when(moment)) if moment else "—"


def _person(row: dict) -> str:
    nick = row.get("username")
    name = row.get("first_name") or ""
    label = f"@{nick}" if nick else (name or "без имени")
    return f'<a class="person" href="/admin/clients/{int(row["telegram_id"])}">{html.escape(label)}</a>'


def _access(row: dict, now: datetime) -> str:
    status = row.get("status")
    until = parse_dt(row.get("access_until"))
    if not status:
        return "доступа нет"
    if status == "past_due":
        return "оплата не прошла"
    if status == "charging":
        return "идёт списание"
    if status == "expired" or (until is not None and until <= now):
        return "срок закончился"
    if status == "active":
        return "доступ открыт"
    return html.escape(str(status))


def _notes(error: str = "", saved: str = "", off: str = "", deleted: str = "") -> str:
    chunks = []
    if saved:
        chunks.append(f'<p class="note">Код {html.escape(saved)} сохранён. Бот уже может его принять.</p>')
    if off:
        chunks.append(f'<p class="note">Код {html.escape(off)} выключен. Бот его больше не примет.</p>')
    if deleted:
        chunks.append(f'<p class="note">Код {html.escape(deleted)} удалён. Его можно завести заново.</p>')
    if error:
        chunks.append(f'<p class="error">{html.escape(error)}</p>')
    return "".join(chunks)


def _promo_line(promo: dict, counts: dict[str, int]) -> str:
    audience = "общий" if promo.get("shared") else "личный"
    scope = period_title(int(promo["months"])) if promo["months"] else "все сроки"
    value = f"{promo['value']}%" if promo["kind"] == "percent" else fmt_rub(int(promo["value"]))
    if promo["disabled"]:
        state = "выключен"
    elif promo.get("shared"):
        used = counts.get(str(promo["code"]), 0)
        state = f"оплатили {used}" if used else "ещё никто не оплатил"
    elif promo["used_by"]:
        state = "использован"
    else:
        state = "активен"
    expires = parse_dt(promo.get("expires_at"))
    until = f", до {fmt_when(expires)}" if expires else ""
    return f"<code>{html.escape(promo['code'])}</code> — {html.escape(value)}, {html.escape(scope)}, {audience}, {html.escape(state)}{html.escape(until)}"


def _promo_actions(code: str, token: str, *, can_disable: bool) -> str:
    hidden = (
        f'<input type="hidden" name="csrf" value="{html.escape(token)}">'
        f'<input type="hidden" name="code" value="{html.escape(code)}">'
    )
    disable = ""
    if can_disable:
        disable = (
            '<form class="inline" method="post" action="/admin/promos/off">'
            + hidden
            + '<button class="quiet" type="submit">Выключить</button></form>'
        )
    delete = (
        '<form class="inline" method="post" action="/admin/promos/delete" '
        'onsubmit="return confirm(\'Удалить код? Его можно будет завести заново.\')">'
        + hidden
        + '<button class="quiet" type="submit">Удалить</button></form>'
    )
    return f'<span class="actions">{disable}{delete}</span>'


def _panel(
    promos: list[dict],
    counts: dict[str, int],
    token: str,
    error: str,
    saved: str,
    off: str,
    deleted: str = "",
) -> str:
    items = []
    for promo in promos:
        line = _promo_line(promo, counts)
        can_disable = not promo["disabled"] and not promo["used_by"]
        items.append(
            "<li><span>"
            + line
            + "</span>"
            + _promo_actions(promo["code"], token, can_disable=can_disable)
            + "</li>"
        )
    listing = "<ul>" + "".join(items) + "</ul>" if items else '<p class="muted">Кодов пока нет.</p>'
    return (
        "<h1>Промокоды</h1>"
        + _nav("promos")
        + '<p class="muted">Код, сохранённый здесь, бот принимает сразу. Удаление стирает код и тех, кто его уже использовал, поэтому те же буквы можно выдать снова.</p>'
        + _notes(error, saved, off, deleted)
        + "<h2>Новый промокод</h2>"
        + '<form method="post" action="/admin/promos">'
        + f'<input type="hidden" name="csrf" value="{html.escape(token)}">'
        + '<div class="row two">'
        + '<div><label for="code">Код</label><input id="code" name="code" required maxlength="32" placeholder="NEWYEAR" autocapitalize="characters"></div>'
        + '<div><label for="audience">Кому</label><select id="audience" name="audience">'
        + '<option value="shared">Общий, каждый один раз</option>'
        + '<option value="personal">Личный, один человек</option></select></div>'
        + "</div>"
        + '<div class="row two">'
        + '<div><label for="mode">Скидка</label><select id="mode" name="mode">'
        + '<option value="percent">Процент</option><option value="final">Конечная цена, ₽</option></select></div>'
        + '<div><label for="value">Число</label><input id="value" name="value" inputmode="decimal" required placeholder="20"></div>'
        + "</div>"
        + '<div class="row two">'
        + '<div><label for="months">Срок</label><select id="months" name="months">'
        + '<option value="">Все сроки</option><option>1</option><option>3</option><option>6</option><option>12</option>'
        + "</select></div>"
        + '<div><label for="expires">Действует до конца дня</label><input id="expires" name="expires" type="date"></div>'
        + "</div>"
        + "<button type=\"submit\">Сохранить</button>"
        + "</form>"
        + "<h2>Коды</h2>"
        + '<div class="card">'
        + listing
        + "</div>"
        + '<p><a class="link" href="/admin/reestr.xlsx">Скачать реестр Excel</a></p>'
    )


def register_admin(app: web.Application) -> None:
    app.router.add_get("/admin", office)
    app.router.add_get("/admin/clients/{telegram_id:\\d+}", client_page)
    app.router.add_get("/admin/payments", payments_page)
    app.router.add_get("/admin/promos", promos_page)
    app.router.add_post("/admin/login", login)
    app.router.add_post("/admin/promos", create_promo)
    app.router.add_post("/admin/promos/off", disable_promo)
    app.router.add_post("/admin/promos/delete", delete_promo_page)
    app.router.add_get("/admin/reestr.xlsx", download_ledger)


def _runtime(request: web.Request) -> Runtime:
    return request.app["runtime"]


def _cookie_token(request: web.Request) -> str | None:
    return request.cookies.get(COOKIE)


def _guard(request: web.Request) -> web.Response | None:
    runtime = _runtime(request)
    if not runtime.cfg.admin_password:
        return _page("<h1>Админка закрыта</h1><p>В настройках нет пароля.</p>")
    if not token_ok(runtime, _cookie_token(request)):
        return _page(_login())
    return None


async def office(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    query = request.query.get("q", "").strip()[:80]
    summary = await runtime.db.admin_summary()
    rows = await runtime.db.directory(query)
    plans = await runtime.db.plans()
    return _page(_clients(summary, rows, plans, query))


def _clients(summary: dict, rows: list[dict], plans: list[dict], query: str) -> str:
    now = utcnow()
    stats = (
        '<div class="stats">'
        f'<div class="stat"><b>{summary["active"]}</b><span>доступ открыт</span></div>'
        f'<div class="stat"><b>{summary["autopay"]}</b><span>с автопродлением</span></div>'
        f'<div class="stat"><b>{summary["ending"]}</b><span>кончается за 7 суток</span></div>'
        f'<div class="stat"><b>{summary["past_due"]}</b><span>оплата не прошла</span></div>'
        f'<div class="stat"><b>{html.escape(fmt_rub(summary["paid"]))}</b><span>оплаты в реестре</span></div>'
        f'<div class="stat"><b>{html.escape(fmt_rub(summary["refunds"]))}</b><span>возвраты</span></div>'
        "</div>"
    )
    tariffs = []
    for plan in plans:
        price = current_price(plan, now)
        tariffs.append(f"{html.escape(period_title(int(plan['months'])))} — {html.escape(fmt_rub(price))}")
    tariff_line = '<p class="muted">Тарифы сейчас: ' + " · ".join(tariffs) + "</p>" if tariffs else ""
    body = []
    for row in rows:
        until = _when(row.get("access_until"))
        term = period_title(int(row["months"])) if row.get("months") else "—"
        price = fmt_rub(int(row["price_kopecks"])) if row.get("price_kopecks") is not None else "—"
        autopay = "да" if row.get("autopay") and row.get("rebill_id") else "нет"
        mail = row.get("email") or "почты нет"
        body.append(
            "<tr>"
            f"<td>{_person(row)}<br><span class=\"muted\">{html.escape(str(row['telegram_id']))}</span></td>"
            f"<td>{html.escape(mail)}</td>"
            f"<td>{html.escape(_access(row, now))}</td>"
            f"<td>{html.escape(term)}<br><span class=\"muted\">{html.escape(price)}</span></td>"
            f"<td>{until}</td>"
            f"<td>{autopay}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Человек</th><th>Почта</th><th>Доступ</th><th>Срок</th><th>До</th><th>Авто</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        if body
        else '<p class="muted">Никого не нашлось.</p>'
    )
    found = f"{len(rows)}" if not query else f"{len(rows)} по запросу «{html.escape(query)}»"
    return (
        "<h1>Клиенты</h1>"
        + _nav("clients")
        + stats
        + tariff_line
        + '<form class="search" method="get" action="/admin">'
        + f'<input name="q" value="{html.escape(query)}" placeholder="Ник, почта или Telegram id">'
        + "<button type=\"submit\">Найти</button></form>"
        + f'<p class="muted">{found}</p>'
        + '<div class="card">'
        + table
        + "</div>"
    )


async def client_page(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    telegram_id = int(request.match_info["telegram_id"])
    user = await runtime.db.user(telegram_id)
    if user is None:
        return _page("<h1>Нет такого клиента</h1>" + _nav("clients") + '<p><a href="/admin">К списку</a></p>')
    sub = await runtime.db.subscription(telegram_id)
    payments = await runtime.db.payments_for(telegram_id)
    ledger = await runtime.db.ledger_for(telegram_id)
    return _page(_client(user, sub, payments, ledger))


def _client(user: dict, sub: dict | None, payments: list[dict], ledger: list[dict]) -> str:
    now = utcnow()
    row = {**user, **(sub or {})}
    nick = f"@{user['username']}" if user.get("username") else (user.get("first_name") or "без имени")
    facts = [
        ("Telegram id", str(user["telegram_id"])),
        ("Имя", user.get("first_name") or "—"),
        ("Почта", user.get("email") or "нет"),
        ("В боте с", _when(user.get("created_at"))),
        ("Доступ", _access(row, now)),
    ]
    if sub:
        facts.extend(
            [
                ("Срок", period_title(int(sub["months"]))),
                ("Цена продления", fmt_rub(int(sub["price_kopecks"]))),
                ("Доступ до", _when(sub.get("access_until"))),
                ("Автопродление", "включено" if sub.get("autopay") and sub.get("rebill_id") else "нет"),
                ("Карта", sub.get("pan") or "не сохранена"),
                ("Предупреждение", _when(sub.get("warned_at"))),
                ("Сбой оплаты", _when(sub.get("charge_failed_at"))),
                ("Повтор списания", _when(sub.get("retry_at"))),
                ("Исключение", _when(sub.get("remove_at"))),
            ]
        )
    elif user.get("awaiting") == "email":
        term = period_title(int(user["pending_months"])) if user.get("pending_months") else "срок не выбран"
        facts.append(("Сейчас в боте", f"ждёт почту для чека, {term}"))
    definition = _facts(facts)
    pay_rows = []
    for payment in payments:
        pay_rows.append(
            "<tr>"
            f"<td>{_when(payment.get('created_at'))}</td>"
            f"<td>{html.escape(str(payment.get('status') or ''))}</td>"
            f"<td>{html.escape(period_title(int(payment['months'])))}</td>"
            f"<td>{html.escape(fmt_rub(int(payment['amount'])))}</td>"
            f"<td>{html.escape(payment.get('promo_code') or '—')}</td>"
            f"<td><code>{html.escape(str(payment.get('order_id') or ''))}</code></td>"
            "</tr>"
        )
    ledger_rows = []
    for entry in ledger:
        kind = KINDS.get(str(entry.get("kind") or ""), str(entry.get("kind") or ""))
        ledger_rows.append(
            "<tr>"
            f"<td>{_when(entry.get('created_at'))}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{html.escape(fmt_rub(int(entry['amount'])))}</td>"
            f"<td>{_when(entry.get('period_start'))} — {_when(entry.get('period_end'))}</td>"
            "</tr>"
        )
    payments_html = (
        "<table><thead><tr><th>Когда</th><th>Статус</th><th>Срок</th><th>Сумма</th><th>Промокод</th><th>Заказ</th></tr></thead><tbody>"
        + "".join(pay_rows)
        + "</tbody></table>"
        if pay_rows
        else '<p class="muted">Платежей нет.</p>'
    )
    ledger_html = (
        "<table><thead><tr><th>Когда</th><th>Тип</th><th>Сумма</th><th>Период</th></tr></thead><tbody>"
        + "".join(ledger_rows)
        + "</tbody></table>"
        if ledger_rows
        else '<p class="muted">В реестре пусто.</p>'
    )
    return (
        f"<h1>{html.escape(nick)}</h1>"
        + _nav("clients")
        + '<p><a href="/admin">Все клиенты</a></p>'
        + '<div class="card"><dl>'
        + definition
        + "</dl></div>"
        + "<h2>Платежи</h2><div class=\"card\">"
        + payments_html
        + "</div><h2>Реестр</h2><div class=\"card\">"
        + ledger_html
        + "</div>"
    )


def _facts(facts: list[tuple[str, str]]) -> str:
    already = {"В боте с", "Доступ до", "Предупреждение", "Сбой оплаты", "Повтор списания", "Исключение"}
    chunks = []
    for title, value in facts:
        shown = value if title in already else html.escape(value)
        chunks.append(f"<dt>{html.escape(title)}</dt><dd>{shown}</dd>")
    return "".join(chunks)


async def payments_page(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    rows = await _runtime(request).db.recent_ledger()
    return _page(_payments(rows))


def _payments(rows: list[dict]) -> str:
    body = []
    for entry in rows:
        kind = KINDS.get(str(entry.get("kind") or ""), str(entry.get("kind") or ""))
        who = entry.get("username") or entry.get("email") or str(entry.get("telegram_id") or "")
        link = ""
        if entry.get("telegram_id"):
            link = f'<a class="person" href="/admin/clients/{int(entry["telegram_id"])}">{html.escape(str(who))}</a>'
        else:
            link = html.escape(str(who))
        term = period_title(int(entry["months"])) if entry.get("months") else "—"
        body.append(
            "<tr>"
            f"<td>{_when(entry.get('created_at'))}</td>"
            f"<td>{link}</td>"
            f"<td>{html.escape(term)}</td>"
            f"<td>{html.escape(fmt_rub(int(entry['amount'])))}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{_when(entry.get('period_start'))}<br>{_when(entry.get('period_end'))}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Когда</th><th>Кто</th><th>Срок</th><th>Сумма</th><th>Тип</th><th>Период</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        if body
        else '<p class="muted">Реестр пуст. Здесь появятся оплаты, продления и возвраты.</p>'
    )
    return (
        "<h1>Оплаты</h1>"
        + _nav("payments")
        + '<p class="muted">Это реестр для ДДС, не банковская выписка. Суммы в рублях.</p>'
        + '<div class="card">'
        + table
        + "</div>"
    )


async def promos_page(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    token = _cookie_token(request) or ""
    promos = await runtime.db.promos()
    counts = await runtime.db.shared_use_counts()
    return _page(
        _panel(
            promos,
            counts,
            token,
            request.query.get("error", ""),
            request.query.get("saved", ""),
            request.query.get("off", ""),
            request.query.get("deleted", ""),
        )
    )


async def login(request: web.Request) -> web.Response:
    runtime = _runtime(request)
    if not runtime.cfg.admin_password:
        return _page("<h1>Админка закрыта</h1><p>В настройках нет пароля.</p>")
    form = await request.post()
    given = str(form.get("password") or "")
    if not passwords_match(given, runtime.cfg.admin_password):
        return _page(_login("Неверный пароль."))
    token = sign(runtime, secrets.token_hex(16))
    response = web.HTTPFound("/admin")
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="Lax",
        secure=request.scheme == "https",
        max_age=60 * 60 * 24 * 14,
        path="/admin",
    )
    raise response


async def create_promo(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    form = await request.post()
    if not token_ok(runtime, str(form.get("csrf") or "")) or str(form.get("csrf") or "") != _cookie_token(request):
        return _page(_login("Сессия устарела. Войдите ещё раз."))
    try:
        code, kind, value, months, expires, shared = _promo_fields(form)
        await runtime.db.create_promo(code, kind, value, months, expires, shared)
    except ValueError as exc:
        raise _back(error=str(exc)) from exc
    except Exception as exc:
        raise _back(error="Такой код уже есть.") from exc
    audience = "общий" if shared else "личный"
    detail = f"{value}%" if kind == "percent" else fmt_rub(value)
    scope = period_title(months) if months else "все сроки"
    await tell_admins(
        request.app["bot"],
        runtime.cfg,
        f"На сайте создан {audience} промокод <code>{html.escape(code)}</code>, {html.escape(detail)}, {html.escape(scope)}. Бот уже его принимает.",
    )
    raise _back(saved=code)


def _promo_fields(form) -> tuple[str, str, int, int | None, str | None, bool]:
    code = str(form.get("code") or "").strip().upper()
    if not CODE.match(code):
        raise ValueError("Код — от 3 до 32 латинских букв и цифр.")
    shared = str(form.get("audience") or "") == "shared"
    mode = str(form.get("mode") or "")
    raw = str(form.get("value") or "").strip().replace(" ", "").replace(",", ".")
    if mode == "percent":
        if not raw.isdigit() or not 1 <= int(raw) <= 99:
            raise ValueError("Процент от 1 до 99.")
        kind, value = "percent", int(raw)
    elif mode == "final":
        if "." in raw:
            rub, kop = raw.split(".", 1)
            if not rub.isdigit() or not kop.isdigit():
                raise ValueError("Цена должна быть числом.")
            value = int(rub) * 100 + int((kop + "00")[:2])
        elif raw.isdigit():
            value = int(raw) * 100
        else:
            raise ValueError("Цена должна быть числом.")
        if value < 1000:
            raise ValueError("Конечная цена не меньше 10 ₽.")
        kind = "final"
    else:
        raise ValueError("Выберите процент или конечную цену.")
    months_raw = str(form.get("months") or "").strip()
    months = int(months_raw) if months_raw else None
    if months is not None and months not in {1, 3, 6, 12}:
        raise ValueError("Срок: все, 1, 3, 6 или 12 месяцев.")
    expires_raw = str(form.get("expires") or "").strip()
    expires = None
    if expires_raw:
        try:
            year, month, day = (int(part) for part in expires_raw.split("-"))
            expires = iso(datetime(year, month, day, 23, 59, tzinfo=MOSCOW))
        except ValueError as exc:
            raise ValueError("Дата должна быть календарной.") from exc
    return code, kind, value, months, expires, shared


async def disable_promo(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    form = await request.post()
    if not token_ok(runtime, str(form.get("csrf") or "")) or str(form.get("csrf") or "") != _cookie_token(request):
        return _page(_login("Сессия устарела. Войдите ещё раз."))
    code = str(form.get("code") or "").strip().upper()
    if not CODE.match(code):
        raise _back(error="Нет такого кода.")
    await runtime.db.disable_promo(code)
    await tell_admins(
        request.app["bot"],
        runtime.cfg,
        f"На сайте выключен промокод <code>{html.escape(code)}</code>.",
    )
    raise _back(off=code)


async def delete_promo_page(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    form = await request.post()
    if not token_ok(runtime, str(form.get("csrf") or "")) or str(form.get("csrf") or "") != _cookie_token(request):
        return _page(_login("Сессия устарела. Войдите ещё раз."))
    code = str(form.get("code") or "").strip().upper()
    if not CODE.match(code):
        raise _back(error="Нет такого кода.")
    removed = await runtime.db.delete_promo(code)
    if not removed:
        raise _back(error="Нет такого кода.")
    await tell_admins(
        request.app["bot"],
        runtime.cfg,
        f"На сайте удалён промокод <code>{html.escape(code)}</code>. Его можно завести заново.",
    )
    raise _back(deleted=code)


async def download_ledger(request: web.Request) -> web.Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    runtime = _runtime(request)
    rows = await runtime.db.ledger_rows()
    stats = await runtime.db.stats()
    path = Path(runtime.cfg.db_path).with_name("ledger.xlsx")
    write_ledger(path, rows, int(stats["active"]))
    return web.FileResponse(
        path,
        headers={"Content-Disposition": 'attachment; filename="reestr-private.xlsx"'},
    )
