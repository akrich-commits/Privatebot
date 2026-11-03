from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.pricing import MOSCOW, parse_dt

KINDS = {
    "payment": "Оплата",
    "renewal": "Продление",
    "refund": "Возврат",
    "grant": "Выдача",
}

HEADERS = (
    "Дата, мск",
    "Заказ",
    "Платёж банка",
    "Telegram id",
    "Ник",
    "Почта",
    "Срок, мес.",
    "Сумма, ₽",
    "Тип",
    "Начало периода, мск",
    "Конец периода, мск",
)


def _local(value: str | None) -> datetime | None:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return parsed.astimezone(MOSCOW).replace(tzinfo=None)


def write_ledger(path: Path, rows: list[dict], active: int) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "Реестр"
    sheet.append(list(HEADERS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append(
            [
                _local(row.get("created_at")),
                row.get("order_id") or "",
                row.get("payment_id") or "",
                row.get("telegram_id") or "",
                row.get("username") or "",
                row.get("email") or "",
                row.get("months") or "",
                int(row.get("amount") or 0) / 100,
                KINDS.get(str(row.get("kind") or ""), str(row.get("kind") or "")),
                _local(row.get("period_start")),
                _local(row.get("period_end")),
            ]
        )
    for index in (1, 10, 11):
        for cell in sheet.iter_cols(min_col=index, max_col=index, min_row=2):
            for item in cell:
                item.number_format = "DD.MM.YYYY HH:MM"
    for cell in sheet.iter_cols(min_col=8, max_col=8, min_row=2):
        for item in cell:
            item.number_format = '#,##0.00'
    if sheet.dimensions:
        sheet.auto_filter.ref = sheet.dimensions
    sheet.freeze_panes = "A2"
    widths = (20, 28, 16, 14, 18, 28, 12, 14, 14, 22, 22)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    summary = book.create_sheet("Сводка")
    paid = sum(int(row["amount"]) for row in rows if row.get("kind") != "refund") / 100
    refunds = sum(int(row["amount"]) for row in rows if row.get("kind") == "refund") / 100
    summary.append(["Показатель", "Значение"])
    summary["A1"].font = Font(bold=True)
    summary["B1"].font = Font(bold=True)
    summary.append(["Активных доступов", active])
    summary.append(["Оплаты без возвратов, ₽", paid])
    summary.append(["Возвраты, ₽", refunds])
    summary["B3"].number_format = '#,##0.00'
    summary["B4"].number_format = '#,##0.00'
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 18
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
