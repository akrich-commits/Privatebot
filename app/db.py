from __future__ import annotations

import asyncio
import csv
from datetime import timedelta
from pathlib import Path

import aiosqlite

from app.pricing import iso, period_title, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    email TEXT,
    pending_months INTEGER,
    pending_promo TEXT,
    awaiting TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    months INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    price_kopecks INTEGER NOT NULL,
    sale_price_kopecks INTEGER,
    sale_until TEXT
);
CREATE TABLE IF NOT EXISTS promos (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL,
    months INTEGER,
    expires_at TEXT,
    used_by INTEGER,
    used_at TEXT,
    reserved_by INTEGER,
    reserved_order TEXT,
    reserved_until TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    shared INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
    code TEXT NOT NULL,
    telegram_id INTEGER NOT NULL,
    order_id TEXT,
    state TEXT NOT NULL,
    until_at TEXT,
    used_at TEXT,
    PRIMARY KEY (code, telegram_id)
);
CREATE TABLE IF NOT EXISTS payments (
    order_id TEXT PRIMARY KEY,
    payment_id TEXT UNIQUE,
    telegram_id INTEGER NOT NULL,
    months INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    email TEXT,
    promo_code TEXT,
    is_renewal INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    rebill_id TEXT,
    pan TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
    telegram_id INTEGER PRIMARY KEY,
    months INTEGER NOT NULL,
    price_kopecks INTEGER NOT NULL,
    status TEXT NOT NULL,
    autopay INTEGER NOT NULL,
    rebill_id TEXT,
    pan TEXT,
    access_until TEXT,
    warned_at TEXT,
    charge_failed_at TEXT,
    retry_at TEXT,
    remove_at TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    invite_link TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    order_id TEXT,
    payment_id TEXT,
    telegram_id INTEGER,
    username TEXT,
    email TEXT,
    months INTEGER,
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SEED = (
    (1, "1 месяц", 399_000),
    (3, "3 месяца", 1_099_000),
    (6, "6 месяцев", 1_999_000),
    (12, "12 месяцев", 3_799_000),
)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        for months, title, price in SEED:
            await self._db.execute(
                "INSERT OR IGNORE INTO plans (months, title, price_kopecks) VALUES (?, ?, ?)",
                (months, title, price),
            )
        await self._db.commit()

    async def _migrate(self) -> None:
        assert self._db is not None
        cur = await self._db.execute("PRAGMA table_info(promos)")
        columns = {row[1] for row in await cur.fetchall()}
        if "shared" not in columns:
            await self._db.execute("ALTER TABLE promos ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None
        return self._db

    async def setting(self, key: str) -> str | None:
        async with self._lock:
            cur = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
        return None if row is None else str(row["value"])

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.db.commit()

    async def upsert_user(self, telegram_id: int, username: str | None, first_name: str | None) -> dict:
        now = iso(utcnow())
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (telegram_id, username, first_name, now),
            )
            await self.db.commit()
            cur = await self.db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = await cur.fetchone()
        return dict(row)

    async def set_email(self, telegram_id: int, email: str) -> None:
        async with self._lock:
            await self.db.execute("UPDATE users SET email = ? WHERE telegram_id = ?", (email, telegram_id))
            await self.db.commit()

    async def set_pending(self, telegram_id: int, months: int | None, promo: str | None) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE users SET pending_months = ?, pending_promo = ? WHERE telegram_id = ?",
                (months, promo, telegram_id),
            )
            await self.db.commit()

    async def set_awaiting(self, telegram_id: int, awaiting: str | None) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE users SET awaiting = ? WHERE telegram_id = ?",
                (awaiting, telegram_id),
            )
            await self.db.commit()

    async def user(self, telegram_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def user_by_username(self, username: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?)",
                (username.lstrip("@"),),
            )
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def plans(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM plans ORDER BY months")
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def plan(self, months: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM plans WHERE months = ?", (months,))
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def set_price(self, months: int, price: int) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE plans SET price_kopecks = ? WHERE months = ?",
                (price, months),
            )
            await self.db.commit()

    async def set_sale(self, months: int, price: int | None, until: str | None) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE plans SET sale_price_kopecks = ?, sale_until = ? WHERE months = ?",
                (price, until, months),
            )
            await self.db.commit()

    async def create_promo(
        self,
        code: str,
        kind: str,
        value: int,
        months: int | None,
        expires_at: str | None,
        shared: bool = False,
    ) -> None:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO promos (code, kind, value, months, expires_at, shared)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code, kind, value, months, expires_at, 1 if shared else 0),
            )
            await self.db.commit()

    async def promo(self, code: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM promos WHERE code = ?", (code.upper(),))
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def promos(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM promos ORDER BY code")
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def disable_promo(self, code: str) -> None:
        async with self._lock:
            await self.db.execute("UPDATE promos SET disabled = 1 WHERE code = ?", (code.upper(),))
            await self.db.commit()

    async def delete_promo(self, code: str) -> bool:
        code = code.upper()
        async with self._lock:
            await self.db.execute("DELETE FROM promo_redemptions WHERE code = ?", (code,))
            cur = await self.db.execute("DELETE FROM promos WHERE code = ?", (code,))
            await self.db.commit()
            return cur.rowcount == 1

    async def reserve_promo(self, code: str, telegram_id: int, order_id: str, until: str, now: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE promos
                SET reserved_by = ?, reserved_order = ?, reserved_until = ?
                WHERE code = ?
                  AND disabled = 0
                  AND used_by IS NULL
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND (
                        reserved_until IS NULL
                        OR reserved_until < ?
                        OR reserved_by = ?
                        OR reserved_order = ?
                  )
                """,
                (telegram_id, order_id, until, code, now, now, telegram_id, order_id),
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def release_promo(self, order_id: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                UPDATE promos
                SET reserved_by = NULL, reserved_order = NULL, reserved_until = NULL
                WHERE reserved_order = ? AND used_by IS NULL
                """,
                (order_id,),
            )
            await self.db.execute(
                "DELETE FROM promo_redemptions WHERE order_id = ? AND state = 'hold'",
                (order_id,),
            )
            await self.db.commit()

    async def release_expired_reservations(self, now: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                UPDATE promos
                SET reserved_by = NULL, reserved_order = NULL, reserved_until = NULL
                WHERE used_by IS NULL AND reserved_until IS NOT NULL AND reserved_until < ?
                """,
                (now,),
            )
            await self.db.execute(
                """
                DELETE FROM promo_redemptions
                WHERE state = 'hold' AND until_at IS NOT NULL AND until_at < ?
                """,
                (now,),
            )
            await self.db.commit()

    async def shared_promo_used(self, code: str, telegram_id: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT 1 FROM promo_redemptions
                WHERE code = ? AND telegram_id = ? AND state = 'used'
                """,
                (code.upper(), telegram_id),
            )
            row = await cur.fetchone()
        return row is not None

    async def shared_use_counts(self) -> dict[str, int]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT code, COUNT(*) AS n FROM promo_redemptions
                WHERE state = 'used' GROUP BY code
                """
            )
            rows = await cur.fetchall()
        return {str(row["code"]): int(row["n"]) for row in rows}

    async def reserve_shared_promo(self, code: str, telegram_id: int, order_id: str, until: str) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT state FROM promo_redemptions WHERE code = ? AND telegram_id = ?",
                (code, telegram_id),
            )
            row = await cur.fetchone()
            if row is not None and row["state"] == "used":
                return False
            await self.db.execute(
                """
                INSERT INTO promo_redemptions (code, telegram_id, order_id, state, until_at)
                VALUES (?, ?, ?, 'hold', ?)
                ON CONFLICT(code, telegram_id) DO UPDATE SET
                    order_id = excluded.order_id,
                    state = 'hold',
                    until_at = excluded.until_at
                WHERE promo_redemptions.state = 'hold'
                """,
                (code, telegram_id, order_id, until),
            )
            await self.db.commit()
            return True

    async def consume_shared_promo(self, code: str, telegram_id: int, order_id: str, now: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                INSERT INTO promo_redemptions (code, telegram_id, order_id, state, until_at, used_at)
                VALUES (?, ?, ?, 'used', NULL, ?)
                ON CONFLICT(code, telegram_id) DO UPDATE SET
                    state = 'used',
                    order_id = excluded.order_id,
                    used_at = excluded.used_at,
                    until_at = NULL
                """,
                (code, telegram_id, order_id, now),
            )
            await self.db.commit()

    async def consume_promo(self, code: str, telegram_id: int, now: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                UPDATE promos
                SET used_by = ?, used_at = ?, reserved_by = NULL, reserved_order = NULL, reserved_until = NULL
                WHERE code = ? AND used_by IS NULL
                """,
                (telegram_id, now, code),
            )
            await self.db.commit()

    async def create_payment(self, row: dict) -> None:
        columns = list(row)
        marks = ", ".join("?" for _ in columns)
        async with self._lock:
            await self.db.execute(
                f"INSERT INTO payments ({', '.join(columns)}) VALUES ({marks})",
                tuple(row[column] for column in columns),
            )
            await self.db.commit()

    async def payment_by_order(self, order_id: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM payments WHERE order_id = ?", (order_id,))
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def payment_by_provider(self, payment_id: str) -> dict | None:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,))
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def bind_payment_id(self, order_id: str, payment_id: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE payments SET payment_id = ? WHERE order_id = ?",
                (payment_id, order_id),
            )
            await self.db.commit()

    async def save_card(self, order_id: str, rebill_id: str | None, pan: str | None, status: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                UPDATE payments
                SET rebill_id = COALESCE(?, rebill_id),
                    pan = COALESCE(?, pan),
                    status = CASE WHEN status = 'confirmed' THEN status ELSE ? END
                WHERE order_id = ?
                """,
                (rebill_id, pan, status, order_id),
            )
            await self.db.commit()

    async def claim_status(self, order_id: str, status: str, confirmed_at: str | None = None) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE payments
                SET status = ?, confirmed_at = COALESCE(?, confirmed_at)
                WHERE order_id = ? AND status != ?
                """,
                (status, confirmed_at, order_id, status),
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def mark_payment(self, order_id: str, status: str, confirmed_at: str | None = None) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE payments SET status = ?, confirmed_at = COALESCE(?, confirmed_at) WHERE order_id = ?",
                (status, confirmed_at, order_id),
            )
            await self.db.commit()

    async def open_payments(self, created_before: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM payments
                WHERE status IN ('new', 'authorized')
                  AND payment_id IS NOT NULL
                  AND created_at < ?
                ORDER BY created_at
                """,
                (created_before,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def subscription(self, telegram_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM subscriptions WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def save_subscription(self, row: dict) -> None:
        row = {**row, "updated_at": iso(utcnow())}
        columns = list(row)
        marks = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "telegram_id")
        async with self._lock:
            await self.db.execute(
                f"INSERT INTO subscriptions ({', '.join(columns)}) VALUES ({marks}) "
                f"ON CONFLICT(telegram_id) DO UPDATE SET {updates}",
                tuple(row[column] for column in columns),
            )
            await self.db.commit()

    async def mark_warned(self, telegram_id: int, when: str) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE subscriptions SET warned_at = ? WHERE telegram_id = ? AND warned_at IS NULL",
                (when, telegram_id),
            )
            await self.db.commit()

    async def due_warnings(self, now: str, horizon: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM subscriptions
                WHERE status = 'active' AND warned_at IS NULL
                  AND access_until > ? AND access_until <= ?
                """,
                (now, horizon),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def due_charges(self, now: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM subscriptions
                WHERE status = 'active' AND autopay = 1
                  AND rebill_id IS NOT NULL AND price_kopecks >= 1000
                  AND access_until <= ?
                """,
                (now,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def due_retries(self, now: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM subscriptions
                WHERE status = 'past_due' AND autopay = 1
                  AND rebill_id IS NOT NULL AND retries < 1
                  AND retry_at IS NOT NULL AND retry_at <= ?
                  AND remove_at IS NOT NULL AND remove_at > ?
                """,
                (now, now),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def due_removals(self, now: str) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT * FROM subscriptions
                WHERE status != 'expired' AND (
                    (status = 'past_due' AND remove_at IS NOT NULL AND remove_at <= ?)
                    OR (status = 'active' AND access_until <= ? AND (autopay = 0 OR rebill_id IS NULL))
                )
                """,
                (now, now),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def set_autopay(self, telegram_id: int, enabled: bool) -> None:
        async with self._lock:
            await self.db.execute(
                "UPDATE subscriptions SET autopay = ?, updated_at = ? WHERE telegram_id = ?",
                (1 if enabled else 0, iso(utcnow()), telegram_id),
            )
            await self.db.commit()

    async def release_stale_charges(self, before: str) -> None:
        async with self._lock:
            await self.db.execute(
                """
                UPDATE subscriptions
                SET status = CASE WHEN charge_failed_at IS NULL THEN 'active' ELSE 'past_due' END,
                    updated_at = ?
                WHERE status = 'charging' AND updated_at < ?
                """,
                (iso(utcnow()), before),
            )
            await self.db.commit()

    async def latest_payment(self, telegram_id: int) -> dict | None:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC LIMIT 1",
                (telegram_id,),
            )
            row = await cur.fetchone()
        return None if row is None else dict(row)

    async def try_lock_charge(self, telegram_id: int) -> bool:
        async with self._lock:
            cur = await self.db.execute(
                """
                UPDATE subscriptions
                SET status = 'charging', updated_at = ?
                WHERE telegram_id = ? AND status IN ('active', 'past_due')
                """,
                (iso(utcnow()), telegram_id),
            )
            await self.db.commit()
            return cur.rowcount == 1

    async def add_ledger(self, row: dict) -> None:
        columns = list(row)
        marks = ", ".join("?" for _ in columns)
        async with self._lock:
            await self.db.execute(
                f"INSERT INTO ledger ({', '.join(columns)}) VALUES ({marks})",
                tuple(row[column] for column in columns),
            )
            await self.db.commit()
        self._append_csv(row)

    def _append_csv(self, row: dict) -> None:
        path = Path(self.path).with_name("ledger.csv")
        new_file = not path.exists()
        fields = [
            "created_at",
            "order_id",
            "payment_id",
            "telegram_id",
            "username",
            "email",
            "months",
            "amount",
            "kind",
            "period_start",
            "period_end",
        ]
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    async def ledger_rows(self) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM ledger ORDER BY id")
            return [dict(row) for row in await cur.fetchall()]

    async def ledger_count(self) -> int:
        async with self._lock:
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM ledger")
            row = await cur.fetchone()
        return int(row["n"])

    async def ledger_csv(self) -> str:
        path = Path(self.path).with_name("ledger.csv")
        if path.exists():
            return str(path)
        async with self._lock:
            cur = await self.db.execute("SELECT * FROM ledger ORDER BY id")
            rows = [dict(row) for row in await cur.fetchall()]
        if not rows:
            path.write_text("", encoding="utf-8")
            return str(path)
        for row in rows:
            self._append_csv(row)
        return str(path)

    async def directory(self, query: str = "") -> list[dict]:
        needle = query.strip().lstrip("@").lower()
        like = f"%{needle}%"
        now = iso(utcnow())
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT u.telegram_id, u.username, u.first_name, u.email, u.created_at,
                       u.pending_months, u.pending_promo, u.awaiting,
                       s.months, s.price_kopecks, s.status, s.autopay, s.pan,
                       s.access_until, s.charge_failed_at, s.retry_at, s.remove_at,
                       s.rebill_id, s.updated_at
                FROM users u
                LEFT JOIN subscriptions s ON s.telegram_id = u.telegram_id
                WHERE ? = ''
                   OR CAST(u.telegram_id AS TEXT) LIKE ?
                   OR lower(COALESCE(u.username, '')) LIKE ?
                   OR lower(COALESCE(u.first_name, '')) LIKE ?
                   OR lower(COALESCE(u.email, '')) LIKE ?
                ORDER BY
                    CASE
                        WHEN s.status = 'active' AND s.access_until > ? THEN 0
                        WHEN s.status = 'past_due' THEN 1
                        ELSE 2
                    END,
                    COALESCE(s.access_until, '9999') ASC,
                    u.telegram_id ASC
                """,
                (needle, like, like, like, like, now),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def admin_summary(self) -> dict:
        now = utcnow()
        now_iso = iso(now)
        week_iso = iso(now + timedelta(days=7))
        async with self._lock:
            cur = await self.db.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'active' AND access_until > ? THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status = 'active' AND access_until > ? AND autopay = 1 AND rebill_id IS NOT NULL THEN 1 ELSE 0 END) AS autopay,
                    SUM(CASE WHEN status = 'active' AND access_until > ? AND access_until <= ? THEN 1 ELSE 0 END) AS ending,
                    SUM(CASE WHEN status = 'past_due' THEN 1 ELSE 0 END) AS past_due
                FROM subscriptions
                """,
                (now_iso, now_iso, now_iso, week_iso),
            )
            counts = dict(await cur.fetchone())
            cur = await self.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind = 'refund' THEN 0 ELSE amount END), 0) AS paid, "
                "COALESCE(SUM(CASE WHEN kind = 'refund' THEN amount ELSE 0 END), 0) AS refunds "
                "FROM ledger"
            )
            money = dict(await cur.fetchone())
        return {
            "active": int(counts["active"] or 0),
            "autopay": int(counts["autopay"] or 0),
            "ending": int(counts["ending"] or 0),
            "past_due": int(counts["past_due"] or 0),
            "paid": int(money["paid"] or 0),
            "refunds": int(money["refunds"] or 0),
        }

    async def payments_for(self, telegram_id: int) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM payments WHERE telegram_id = ? ORDER BY created_at DESC",
                (telegram_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def ledger_for(self, telegram_id: int) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM ledger WHERE telegram_id = ? ORDER BY id DESC",
                (telegram_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def recent_ledger(self, limit: int = 200) -> list[dict]:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT * FROM ledger ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def stats(self) -> dict:
        async with self._lock:
            cur = await self.db.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE status = 'active' AND access_until > ?",
                (iso(utcnow()),),
            )
            active = (await cur.fetchone())["n"]
            cur = await self.db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM ledger WHERE kind != 'refund'"
            )
            paid = (await cur.fetchone())["s"]
        return {"active": active, "paid": paid}

    def plan_label(self, months: int) -> str:
        return period_title(months)
