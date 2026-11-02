from __future__ import annotations

import hashlib
import logging
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Config
from app.pricing import LINK_TTL, MOSCOW, utcnow

log = logging.getLogger("tbank")

# T-Bank's certificate is issued by the Russian Trusted Root CA (Минцифры).
# That root is absent from the default trust store on many servers.
_EXTRA_CA = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_root_ca.pem"


def bank_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(_EXTRA_CA))
    return ctx


class TBankError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def make_token(payload: dict[str, Any], password: str) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in payload.items():
        if key == "Token" or value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        pairs.append((key, text))
    pairs.append(("Password", password))
    pairs.sort(key=lambda item: item[0])
    raw = "".join(value for _, value in pairs)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_notification(payload: dict[str, Any], password: str) -> bool:
    received = str(payload.get("Token") or "")
    if not received:
        return False
    return received.lower() == make_token(payload, password).lower()


@dataclass
class BankNote:
    order_id: str
    payment_id: str
    status: str
    success: bool
    amount: int | None
    rebill_id: str | None
    pan: str | None


def parse_note(payload: dict[str, Any]) -> BankNote:
    rebill = payload.get("RebillId")
    amount = payload.get("Amount")
    return BankNote(
        order_id=str(payload.get("OrderId") or ""),
        payment_id=str(payload.get("PaymentId") or ""),
        status=str(payload.get("Status") or ""),
        success=bool(payload.get("Success")),
        amount=int(amount) if amount is not None and amount != "" else None,
        rebill_id=str(rebill) if rebill else None,
        pan=payload.get("Pan") or None,
    )


class TBank:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession | None = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "TBank":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                connector=aiohttp.TCPConnector(ssl=bank_ssl_context()),
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in payload.items() if value is not None}
        body["TerminalKey"] = self.cfg.tbank_terminal_key
        body["Token"] = make_token(body, self.cfg.tbank_password)
        assert self._session is not None
        async with self._session.post(f"{self.cfg.tbank_url}/{method}", json=body) as response:
            data = await response.json(content_type=None)
        if not data.get("Success"):
            code = str(data.get("ErrorCode") or "")
            message = data.get("Message") or data.get("Details") or "Т-Банк отклонил запрос"
            log.warning("tbank %s failed code=%s message=%s", method, code, message)
            raise TBankError(str(message), code)
        return data

    def receipt(self, email: str, amount: int, months: int) -> dict[str, Any]:
        name = f"Подписка Экономизм Private, {months} мес."
        return {
            "Email": email,
            "Taxation": self.cfg.taxation,
            "FfdVersion": "1.2",
            "Items": [
                {
                    "Name": name[:128],
                    "Price": amount,
                    "Quantity": 1,
                    "Amount": amount,
                    "Tax": "none",
                    "PaymentMethod": "full_payment",
                    "PaymentObject": "service",
                    "MeasurementUnit": "шт",
                }
            ],
        }

    async def init_payment(
        self,
        *,
        order_id: str,
        amount: int,
        description: str,
        customer_key: str,
        email: str,
        months: int,
        recurrent: bool,
        operation_initiator: str,
        extra: dict[str, str],
        success_url: str | None,
        fail_url: str | None,
    ) -> dict[str, Any]:
        due = (utcnow() + LINK_TTL).astimezone(MOSCOW).strftime("%Y-%m-%dT%H:%M:%S+03:00")
        payload: dict[str, Any] = {
            "Amount": amount,
            "OrderId": order_id,
            "Description": description[:140],
            "CustomerKey": customer_key,
            "PayType": "O",
            "Language": "ru",
            "OperationInitiatorType": operation_initiator,
            "RedirectDueDate": due,
            "NotificationURL": f"{self.cfg.public_base_url}/tbank/notify" if self.cfg.public_base_url else None,
            "SuccessURL": success_url,
            "FailURL": fail_url,
            "Receipt": self.receipt(email, amount, months),
            "DATA": extra,
        }
        if recurrent:
            payload["Recurrent"] = "Y"
        return await self._post("Init", payload)

    async def charge(self, payment_id: str, rebill_id: str) -> dict[str, Any]:
        return await self._post("Charge", {"PaymentId": payment_id, "RebillId": rebill_id})

    async def get_state(self, payment_id: str) -> dict[str, Any]:
        return await self._post("GetState", {"PaymentId": payment_id})

    async def cancel(self, payment_id: str) -> dict[str, Any]:
        return await self._post("Cancel", {"PaymentId": payment_id})
