from __future__ import annotations

from dataclasses import dataclass

from app.config import Config
from app.db import Database
from app.tbank import TBank


@dataclass
class Runtime:
    cfg: Config
    db: Database
    tbank: TBank | None
    username: str | None = None
