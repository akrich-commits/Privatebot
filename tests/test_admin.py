from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from openpyxl import load_workbook

from app.config import Config
from app.db import Database
from app.digest import diag_slot, report_slot, scheduled_mail
from app.http import build_app
from app.pricing import MOSCOW
from app.runtime import Runtime
from app.sheets import write_ledger


class Bot:
    def __init__(self):
        self.messages: list[tuple] = []
        self.documents: list[tuple] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, caption=None):
        self.documents.append((chat_id, getattr(document, "filename", "")))

    async def get_me(self):
        class Me:
            username = "private_bot"
            id = 9

        return Me()


def cfg(path: str) -> Config:
    return Config(
        bot_token="token",
        admin_ids=frozenset({1}),
        channel_id=-100123,
        public_base_url="https://example.test",
        tbank_terminal_key="TEST",
        tbank_password="secret",
        taxation="usn_income",
        port=8741,
        db_path=path,
        admin_password="secret-pass",
    )


class ScheduleTest(unittest.TestCase):
    def test_diag_every_six_hours_and_reports_twice(self):
        morning = datetime(2026, 11, 3, 11, 1, tzinfo=MOSCOW)
        self.assertEqual(diag_slot(morning).hour, 6)
        self.assertIsNone(report_slot(morning))
        ten = datetime(2026, 11, 3, 10, 5, tzinfo=MOSCOW)
        self.assertEqual(report_slot(ten).hour, 10)
        self.assertIsNone(report_slot(datetime(2026, 11, 3, 10, 25, tzinfo=MOSCOW)))
        self.assertEqual(report_slot(datetime(2026, 11, 3, 19, 0, tzinfo=MOSCOW)).hour, 19)
        self.assertEqual(diag_slot(datetime(2026, 11, 3, 0, 10, tzinfo=MOSCOW)).hour, 0)


class LedgerBookTest(unittest.TestCase):
    def test_amount_is_rubles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.xlsx"
            write_ledger(
                path,
                [
                    {
                        "created_at": "2026-11-03T07:00:00+00:00",
                        "order_id": "1m-1",
                        "payment_id": "9",
                        "telegram_id": 1,
                        "username": "nick",
                        "email": "a@b.c",
                        "months": 1,
                        "amount": 399000,
                        "kind": "payment",
                        "period_start": "2026-11-03T07:00:00+00:00",
                        "period_end": "2026-12-03T07:00:00+00:00",
                    }
                ],
                1,
            )
            book = load_workbook(path)
            self.assertEqual(book["Реестр"]["H2"].value, 3990)
            self.assertEqual(book["Реестр"]["I2"].value, "Оплата")
            self.assertEqual(book["Сводка"]["B3"].value, 3990)


class MailTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.sqlite3")
        self.db = Database(self.path)
        await self.db.connect()
        self.bot = Bot()
        self.cfg = cfg(self.path)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_diag_is_sent_once_and_report_waits_for_its_window(self):
        import app.digest as digest

        original = digest.utcnow
        digest.utcnow = lambda: datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
        try:
            await scheduled_mail(self.db, None, self.bot, self.cfg)
            await scheduled_mail(self.db, None, self.bot, self.cfg)
        finally:
            digest.utcnow = original
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("Самодиагностика", self.bot.messages[0][1])
        self.assertEqual(await self.db.setting("diag_slot"), "2026-11-03T06")
        self.assertIsNone(await self.db.setting("report_slot"))

    async def test_report_attaches_excel(self):
        import app.digest as digest

        await self.db.add_ledger(
            {
                "created_at": "2026-11-03T07:00:00+00:00",
                "order_id": "1m-1",
                "payment_id": "9",
                "telegram_id": 1,
                "username": "nick",
                "email": "a@b.c",
                "months": 1,
                "amount": 399000,
                "kind": "payment",
                "period_start": "2026-11-03T07:00:00+00:00",
                "period_end": "2026-12-03T07:00:00+00:00",
            }
        )
        original = digest.utcnow
        digest.utcnow = lambda: datetime(2026, 11, 3, 7, 5, tzinfo=timezone.utc)
        try:
            await scheduled_mail(self.db, None, self.bot, self.cfg)
        finally:
            digest.utcnow = original
        self.assertEqual(self.bot.documents, [(1, "reestr-private.xlsx")])
        self.assertTrue(any("3 990" in text or "3990" in text for _, text in self.bot.messages))


class AdminSiteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.sqlite3")
        self.db = Database(self.path)
        await self.db.connect()
        self.bot = Bot()
        self.cfg = cfg(self.path)
        self.app = build_app(Runtime(cfg=self.cfg, db=self.db, tbank=None), self.bot)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_login_creates_promo_and_excel(self):
        async with TestClient(TestServer(self.app)) as client:
            denied = await client.post("/admin/login", data={"password": "nope"})
            self.assertIn("Неверный пароль", await denied.text())
            page = await client.post("/admin/login", data={"password": "secret-pass"})
            html = await page.text()
            self.assertIn("Клиенты", html)
            self.assertIn("доступ открыт", html)
            promos = await client.get("/admin/promos")
            html = await promos.text()
            self.assertIn("Новый промокод", html)
            csrf = re.search(r'name="csrf" value="([^"]+)"', html)
            self.assertIsNotNone(csrf)
            created = await client.post(
                "/admin/promos",
                data={
                    "csrf": csrf.group(1),
                    "code": "site20",
                    "audience": "shared",
                    "mode": "percent",
                    "value": "20",
                    "months": "12",
                    "expires": "2026-11-30",
                },
            )
            body = await created.text()
            self.assertIn("SITE20", body)
            self.assertIn("сохранён", body)
            promo = await self.db.promo("SITE20")
            self.assertEqual(promo["kind"], "percent")
            self.assertEqual(promo["value"], 20)
            self.assertEqual(promo["months"], 12)
            self.assertEqual(promo["shared"], 1)
            self.assertTrue(any("SITE20" in text for _, text in self.bot.messages))
            again = await client.post(
                "/admin/promos",
                data={
                    "csrf": csrf.group(1),
                    "code": "SITE20",
                    "audience": "personal",
                    "mode": "percent",
                    "value": "10",
                    "months": "",
                    "expires": "",
                },
            )
            self.assertIn("уже есть", await again.text())
            book = await client.get("/admin/reestr.xlsx")
            self.assertEqual(book.status, 200)
            raw = await book.read()
            self.assertTrue(raw.startswith(b"PK"))
            closed = await client.post(
                "/admin/promos/off",
                data={"csrf": csrf.group(1), "code": "SITE20"},
            )
            self.assertIn("выключен", await closed.text())
            await self.db.consume_shared_promo("SITE20", 7, "order-1", "2026-11-03T07:00:00+00:00")
            self.assertTrue(await self.db.shared_promo_used("SITE20", 7))
            removed = await client.post(
                "/admin/promos/delete",
                data={"csrf": csrf.group(1), "code": "SITE20"},
            )
            self.assertIn("удалён", await removed.text())
            self.assertIsNone(await self.db.promo("SITE20"))
            self.assertFalse(await self.db.shared_promo_used("SITE20", 7))
            again_new = await client.post(
                "/admin/promos",
                data={
                    "csrf": csrf.group(1),
                    "code": "SITE20",
                    "audience": "shared",
                    "mode": "percent",
                    "value": "15",
                    "months": "",
                    "expires": "",
                },
            )
            self.assertIn("сохранён", await again_new.text())
        promo = await self.db.promo("SITE20")
        self.assertEqual(promo["disabled"], 0)
        self.assertEqual(promo["value"], 15)
        self.assertGreaterEqual(len(self.bot.messages), 3)

    async def test_clients_show_term_price_and_payment(self):
        await self.db.upsert_user(7, "nick", "Ник")
        await self.db.set_email(7, "nick@mail.ru")
        await self.db.save_subscription(
            {
                "telegram_id": 7,
                "months": 3,
                "price_kopecks": 1099000,
                "status": "active",
                "autopay": 0,
                "access_until": "2026-12-01T10:00:00+00:00",
            }
        )
        await self.db.add_ledger(
            {
                "created_at": "2026-11-03T07:00:00+00:00",
                "order_id": "3m-7",
                "payment_id": "9",
                "telegram_id": 7,
                "username": "nick",
                "email": "nick@mail.ru",
                "months": 3,
                "amount": 1099000,
                "kind": "payment",
                "period_start": "2026-11-03T07:00:00+00:00",
                "period_end": "2026-12-01T10:00:00+00:00",
            }
        )
        async with TestClient(TestServer(self.app)) as client:
            await client.post("/admin/login", data={"password": "secret-pass"})
            found = await client.get("/admin", params={"q": "nick@mail.ru"})
            html = await found.text()
            self.assertIn("@nick", html)
            self.assertIn("3 месяца", html)
            self.assertIn("10 990", html)
            self.assertIn("доступ открыт", html)
            card = await client.get("/admin/clients/7")
            card_html = await card.text()
            self.assertIn("nick@mail.ru", card_html)
            self.assertIn("10 990", card_html)
            self.assertIn("1 декабря", card_html)
            missing = await client.get("/admin/clients/404")
            self.assertIn("Нет такого клиента", await missing.text())
            payments = await client.get("/admin/payments")
            self.assertIn("10 990", await payments.text())
            self.assertIn("Оплата", await payments.text())
