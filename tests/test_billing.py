from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from app.config import Config
from app.db import Database
from app.http import build_app
from app.runtime import Runtime
from app.pricing import add_months, apply_promo, current_price, utcnow
from app.service import apply_note, confirm_payment, promo_error, promo_problem, start_checkout
from app.tbank import BankNote, bank_ssl_context, make_token, verify_notification


class FakeBot:
    def __init__(self):
        self.messages: list[tuple] = []
        self.unbanned: list[int] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))

    async def get_chat_member(self, chat_id, user_id):
        class Member:
            status = "left"

        return Member()

    async def create_chat_invite_link(self, chat_id, name, expire_date, member_limit):
        class Link:
            invite_link = "https://t.me/+test"

        return Link()

    async def unban_chat_member(self, chat_id, user_id):
        self.unbanned.append(user_id)

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        return True


class FakeTBank:
    def __init__(self):
        self.calls: list[dict] = []

    async def init_payment(self, **kwargs):
        self.calls.append(kwargs)
        return {"PaymentId": "555", "PaymentURL": "https://pay.example/555"}


def cfg() -> Config:
    return Config(
        bot_token="",
        admin_ids=frozenset({1}),
        channel_id=-100123,
        public_base_url="",
        tbank_terminal_key="TEST",
        tbank_password="secret",
        taxation="usn_income",
        port=8741,
        db_path=":memory:",
    )


class TokenTest(unittest.TestCase):
    def test_receipt_is_not_part_of_the_signature(self):
        payload = {
            "TerminalKey": "TEST",
            "Amount": 399000,
            "OrderId": "1m-1",
            "Receipt": {"Email": "a@b.c", "Items": []},
            "DATA": {"telegram_id": "1"},
        }
        changed = dict(payload, Receipt={"Email": "other@b.c"})
        self.assertEqual(make_token(payload, "secret"), make_token(changed, "secret"))

    def test_notification_roundtrip(self):
        payload = {
            "TerminalKey": "TEST",
            "OrderId": "1m-1",
            "Success": True,
            "Status": "CONFIRMED",
            "PaymentId": "555",
            "ErrorCode": "0",
            "Amount": 399000,
        }
        payload["Token"] = make_token(payload, "secret")
        self.assertTrue(verify_notification(payload, "secret"))
        payload["Amount"] = 1
        self.assertFalse(verify_notification(payload, "secret"))


class PriceTest(unittest.TestCase):
    def test_sale_window_and_promo(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        plan = {
            "price_kopecks": 3_799_000,
            "sale_price_kopecks": 2_849_000,
            "sale_until": (now + timedelta(days=1)).isoformat(),
        }
        self.assertEqual(current_price(plan, now), 2_849_000)
        self.assertEqual(current_price(plan, now + timedelta(days=2)), 3_799_000)
        self.assertEqual(apply_promo(1_000_000, {"kind": "percent", "value": 20}), 800_000)
        self.assertEqual(apply_promo(3_799_000, {"kind": "final", "value": 2_849_000}), 2_849_000)
        with self.assertRaises(ValueError):
            apply_promo(50_000, {"kind": "percent", "value": 99})

    def test_add_months_clips_short_month(self):
        start = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
        self.assertEqual(add_months(start, 1).day, 28)
        self.assertEqual(add_months(start, 12).year, 2027)


class BillingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.sqlite3"))
        await self.db.connect()
        self.bot = FakeBot()
        self.cfg = cfg()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_checkout_sends_the_bot_price(self):
        user = await self.db.upsert_user(7, "nick", "Ник")
        await self.db.set_email(7, "nick@mail.ru")
        await self.db.set_pending(7, 12, None)
        user = await self.db.user(7)
        bank = FakeTBank()
        url, amount = await start_checkout(self.db, bank, self.cfg, user, "private_bot")
        self.assertEqual(url, "https://pay.example/555")
        self.assertEqual(amount, 3_799_000)
        self.assertEqual(bank.calls[0]["amount"], 3_799_000)
        self.assertEqual(bank.calls[0]["operation_initiator"], "1")
        self.assertTrue(bank.calls[0]["recurrent"])
        self.assertEqual(bank.calls[0]["email"], "nick@mail.ru")

    async def test_demo_terminal_does_not_save_the_card(self):
        user = await self.db.upsert_user(7, "nick", "Ник")
        await self.db.set_email(7, "nick@mail.ru")
        await self.db.set_pending(7, 1, None)
        user = await self.db.user(7)
        bank = FakeTBank()
        demo = Config(
            bot_token="",
            admin_ids=frozenset({1}),
            channel_id=-100123,
            public_base_url="",
            tbank_terminal_key="1793639084268DEMO",
            tbank_password="secret",
            taxation="usn_income",
            port=8741,
            db_path=":memory:",
            save_card=False,
        )
        await start_checkout(self.db, bank, demo, user, "private_bot")
        self.assertFalse(bank.calls[0]["recurrent"])
        self.assertEqual(bank.calls[0]["operation_initiator"], "0")

    async def test_promo_is_one_time_and_locks_the_price(self):
        await self.db.upsert_user(7, "nick", "Ник")
        await self.db.create_promo("GIFT", "final", 2_849_000, 12, None)
        promo = await self.db.promo("gift")
        self.assertIsNone(promo_error(promo, 12, 7, utcnow()))
        self.assertIsNotNone(promo_error(promo, 1, 7, utcnow()))
        now = utcnow().isoformat()
        self.assertTrue(await self.db.reserve_promo("GIFT", 7, "order-a", now, "2000-01-01T00:00:00+00:00"))
        self.assertFalse(await self.db.reserve_promo("GIFT", 8, "order-b", now, "2000-01-01T00:00:00+00:00"))

    async def test_shared_promo_is_once_per_person(self):
        await self.db.create_promo("SALE", "percent", 20, None, None, shared=True)
        promo = await self.db.promo("sale")
        now = utcnow()
        until = (now + timedelta(hours=1)).isoformat()
        self.assertIsNone(await promo_problem(self.db, promo, 1, 7, now))
        self.assertIsNone(await promo_problem(self.db, promo, 12, 8, now))
        self.assertTrue(await self.db.reserve_shared_promo("SALE", 7, "order-a", until))
        self.assertTrue(await self.db.reserve_shared_promo("SALE", 8, "order-b", until))
        await self.db.consume_shared_promo("SALE", 7, "order-a", now.isoformat())
        self.assertEqual(
            await promo_problem(self.db, promo, 1, 7, now),
            "Вы уже использовали этот промокод.",
        )
        self.assertIsNone(await promo_problem(self.db, promo, 1, 8, now))
        self.assertFalse(await self.db.reserve_shared_promo("SALE", 7, "order-c", until))
        self.assertEqual((await self.db.shared_use_counts())["SALE"], 1)

    async def test_confirm_is_idempotent_and_refund_removes_access(self):
        await self.db.upsert_user(7, "nick", "Ник")
        await self.db.create_payment(
            {
                "order_id": "12m-7",
                "payment_id": "555",
                "telegram_id": 7,
                "months": 12,
                "amount": 3_799_000,
                "email": "nick@mail.ru",
                "promo_code": None,
                "is_renewal": 0,
                "status": "authorized",
                "rebill_id": "rebill-1",
                "pan": "430000******0777",
                "created_at": utcnow().isoformat(),
                "confirmed_at": None,
            }
        )
        payment = await self.db.payment_by_order("12m-7")
        await confirm_payment(self.db, self.bot, self.cfg, payment)
        await confirm_payment(self.db, self.bot, self.cfg, payment)
        sub = await self.db.subscription(7)
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["price_kopecks"], 3_799_000)
        self.assertEqual(sub["rebill_id"], "rebill-1")
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("https://t.me/+test", self.bot.messages[0][1])
        await apply_note(
            self.db,
            self.bot,
            self.cfg,
            BankNote("12m-7", "555", "REFUNDED", True, 3_799_000, None, None),
        )
        sub = await self.db.subscription(7)
        self.assertEqual(sub["status"], "expired")
        self.assertEqual(self.bot.unbanned, [7])

    async def test_amount_mismatch_does_not_open_access(self):
        await self.db.upsert_user(7, "nick", "Ник")
        await self.db.create_payment(
            {
                "order_id": "1m-7",
                "payment_id": "1",
                "telegram_id": 7,
                "months": 1,
                "amount": 399_000,
                "email": "nick@mail.ru",
                "promo_code": None,
                "is_renewal": 0,
                "status": "new",
                "rebill_id": None,
                "pan": None,
                "created_at": utcnow().isoformat(),
                "confirmed_at": None,
            }
        )
        await apply_note(
            self.db,
            self.bot,
            self.cfg,
            BankNote("1m-7", "1", "CONFIRMED", True, 100, None, None),
        )
        self.assertIsNone(await self.db.subscription(7))


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.sqlite3"))
        await self.db.connect()
        self.cfg = cfg()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_bank_notification_gets_plain_ok(self):
        app = build_app(Runtime(cfg=self.cfg, db=self.db, tbank=None), bot=None)
        payload = {
            "TerminalKey": "TEST",
            "OrderId": "missing",
            "Success": True,
            "Status": "CONFIRMED",
            "PaymentId": "1",
            "ErrorCode": "0",
            "Amount": 399000,
        }
        payload["Token"] = make_token(payload, "secret")
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/tbank/notify", json=payload)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.text(), "OK")


class SslTests(unittest.TestCase):
    def test_russian_trusted_root_is_loaded(self):
        subjects = {
            item.get("subject")[-1][0][1]
            for item in bank_ssl_context().get_ca_certs()
            if item.get("subject")
        }
        self.assertIn("Russian Trusted Root CA", subjects)


if __name__ == "__main__":
    unittest.main()
