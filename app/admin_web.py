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
from app.pricing import MOSCOW, fmt_rub, fmt_when, iso, parse_dt, period_title
from app.runtime import Runtime
from app.sheets import write_ledger

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
  main {{ max-width: 720px; margin: 0 auto; padding: 28px 18px 64px; }}
  h1 {{ font-size: 32px; font-weight: 560; letter-spacing: -0.03em; margin: 0 0 8px; }}
  h2 {{ font-size: 20px; margin: 28px 0 12px; }}
  p {{ margin: 0 0 12px; }}
  .muted {{ color: var(--muted); }}
  form, .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }}
  label {{ display: block; font-size: 14px; margin: 12px 0 4px; }}
  input, select, button {{ font: 16px/1.3 inherit; color: inherit; }}
  input, select {{ width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }}
  .row {{ display: grid; gap: 12px; }}
  @media (min-width: 700px) {{ .row.two {{ grid-template-columns: 1fr 1fr; }} }}
  button, .link {{ display: inline-block; margin-top: 14px; background: var(--ink); color: #fff; border: 0; border-radius: 999px; padding: 10px 16px; text-decoration: none; cursor: pointer; }}
  button.quiet {{ background: transparent; color: var(--accent); padding: 0; margin: 0; }}
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


def _back(*, error: str = "", saved: str = "", off: str = "") -> web.HTTPFound:
    query = {}
    if error:
        query["error"] = error
    if saved:
        query["saved"] = saved
    if off:
        query["off"] = off
    return web.HTTPFound(str(URL("/admin").with_query(query)))


def _page(body: str) -> web.Response:
    return web.Response(text=PAGE.format(body=body), content_type="text/html", charset="utf-8")


def _login(error: str = "") -> str:
    note = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return (
        "<h1>Админка Private</h1>"
        '<p class="muted">Промокоды, которые вы сохраните здесь, бот увидит сразу.</p>'
        f"{note}"
        '<form method="post" action="/admin/login">'
        "<label for=\"password\">Пароль</label>"
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        "<button type=\"submit\">Войти</button>"
        "</form>"
    )


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


def _panel(
    runtime: Runtime,
    promos: list[dict],
    counts: dict[str, int],
    token: str,
    error: str,
    saved: str,
    off: str,
) -> str:
    notes = []
    if saved:
        notes.append(f'<p class="note">Код {html.escape(saved)} сохранён. Бот уже может его принять.</p>')
    if off:
        notes.append(f'<p class="note">Код {html.escape(off)} выключен. Бот его больше не примет.</p>')
    if error:
        notes.append(f'<p class="error">{html.escape(error)}</p>')
    items = []
    for promo in promos:
        line = _promo_line(promo, counts)
        if promo["disabled"] or promo["used_by"]:
            items.append(f"<li><span>{line}</span></li>")
            continue
        items.append(
            "<li><span>"
            + line
            + "</span>"
            + '<form method="post" action="/admin/promos/off">'
            + f'<input type="hidden" name="csrf" value="{html.escape(token)}">'
            + f'<input type="hidden" name="code" value="{html.escape(promo["code"])}">'
            + '<button class="quiet" type="submit">Выключить</button></form></li>'
        )
    listing = "<ul>" + "".join(items) + "</ul>" if items else '<p class="muted">Кодов пока нет.</p>'
    return (
        "<h1>Админка Private</h1>"
        '<p class="muted">Самодиагностика в 0:00, 6:00, 12:00 и 18:00 мск. '
        "Сводка и реестр Excel приходят в 10:00 и 19:00 мск.</p>"
        + "".join(notes)
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
    app.router.add_post("/admin/login", login)
    app.router.add_post("/admin/promos", create_promo)
    app.router.add_post("/admin/promos/off", disable_promo)
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
    token = _cookie_token(request) or ""
    promos = await runtime.db.promos()
    counts = await runtime.db.shared_use_counts()
    saved = request.query.get("saved", "")
    error = request.query.get("error", "")
    off = request.query.get("off", "")
    return _page(_panel(runtime, promos, counts, token, error, saved, off))


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
