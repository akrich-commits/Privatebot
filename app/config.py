from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _save_card(terminal_key: str) -> bool:
    # Кабинет не засчитывает тест «Успешная оплата», если в Init есть Recurrent=Y.
    # На DEMO-терминале карту не сохраняем, пока это явно не включено.
    raw = os.environ.get("TBANK_RECURRENT", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not terminal_key.endswith("DEMO")


def _load_env_file() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    channel_id: int | None
    public_base_url: str
    tbank_terminal_key: str
    tbank_password: str
    taxation: str
    port: int
    db_path: str
    admin_password: str = ""
    save_card: bool = True
    tbank_url: str = "https://securepay.tinkoff.ru/v2"

    @property
    def tbank_ready(self) -> bool:
        return bool(self.tbank_terminal_key and self.tbank_password)

    @classmethod
    def from_env(cls) -> "Config":
        _load_env_file()
        admins: set[int] = set()
        for part in os.environ.get("ADMIN_IDS", "").split(","):
            part = part.strip()
            if part:
                admins.add(int(part))
        channel = os.environ.get("CHANNEL_ID", "").strip()
        taxation = os.environ.get("TAXATION", "usn_income").strip() or "usn_income"
        if taxation not in {"usn_income", "usn_income_outcome"}:
            raise SystemExit("TAXATION должен быть usn_income или usn_income_outcome")
        terminal_key = os.environ.get("TBANK_TERMINAL_KEY", "").strip()
        return cls(
            bot_token=os.environ.get("BOT_TOKEN", "").strip(),
            admin_ids=frozenset(admins),
            channel_id=int(channel) if channel else None,
            public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            tbank_terminal_key=terminal_key,
            tbank_password=os.environ.get("TBANK_PASSWORD", "").strip(),
            taxation=taxation,
            save_card=_save_card(terminal_key),
            port=int(os.environ.get("PORT", "8741")),
            db_path=os.environ.get("DB_PATH", "data/private.sqlite3"),
            admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        )
