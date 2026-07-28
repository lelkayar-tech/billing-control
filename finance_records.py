import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any


class FinanceValidationError(ValueError):
    """Ошибка проверки данных счёта или платежа."""


def suggest_next_invoice_number(
    application_no: Any,
    invoices: list[dict[str, Any]],
) -> str:
    """
    Предлагает следующий номер счёта в серии приложения.

    Например, для приложения ОМ-12948 и счетов ОМ-12948/1.../3
    возвращает ОМ-12948/4. Номера других серий не учитываются.
    """

    normalized_application_no = str(application_no or "").strip()

    if not normalized_application_no:
        return ""

    pattern = re.compile(
        rf"^{re.escape(normalized_application_no)}/([1-9]\d*)$",
        re.IGNORECASE,
    )
    suffixes: list[int] = []

    for invoice in invoices:
        invoice_no = str(invoice.get("invoice_no") or "").strip()
        match = pattern.fullmatch(invoice_no)

        if match:
            suffixes.append(int(match.group(1)))

    next_suffix = max(suffixes, default=0) + 1
    return f"{normalized_application_no}/{next_suffix}"


def _required_text(value: Any, label: str) -> str:
    normalized = str(value or "").strip()

    if not normalized:
        raise FinanceValidationError(f"Заполните поле «{label}».")

    return normalized


def _positive_amount(value: Any) -> str:
    normalized = (
        str(value or "")
        .strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError):
        raise FinanceValidationError(
            "Сумма должна быть положительным числом."
        ) from None

    if not amount.is_finite() or amount <= 0:
        raise FinanceValidationError(
            "Сумма должна быть больше нуля."
        )

    return format(amount, "f")


def _optional_date(value: Any, label: str) -> str | None:
    normalized = str(value or "").strip()

    if not normalized:
        return None

    try:
        parsed = datetime.date.fromisoformat(normalized)
    except ValueError:
        raise FinanceValidationError(
            f"Поле «{label}» содержит неверную дату."
        ) from None

    if parsed < datetime.date(2000, 1, 1):
        raise FinanceValidationError(
            f"Поле «{label}» не может быть раньше 01.01.2000."
        )

    if parsed > datetime.date(2100, 12, 31):
        raise FinanceValidationError(
            f"Поле «{label}» не может быть позже 31.12.2100."
        )

    return parsed.isoformat()


def normalize_invoice_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Данные счёта переданы в неверном формате."
        )

    service_start_date = _optional_date(
        raw_payload.get("service_start_date"),
        "Начало услуг",
    )
    service_end_date = _optional_date(
        raw_payload.get("service_end_date"),
        "Окончание услуг",
    )

    if (
        service_start_date is not None
        and service_end_date is not None
        and service_end_date < service_start_date
    ):
        raise FinanceValidationError(
            "Окончание услуг не может быть раньше начала."
        )

    return {
        "invoice_no": _required_text(
            raw_payload.get("invoice_no"),
            "Номер счёта",
        ),
        "content": _required_text(
            raw_payload.get("content"),
            "Содержание",
        ),
        "amount": _positive_amount(raw_payload.get("amount")),
        "service_start_date": service_start_date,
        "service_end_date": service_end_date,
        "due_date": _optional_date(
            raw_payload.get("due_date"),
            "Срок оплаты",
        ),
    }


def normalize_payment_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Данные платежа переданы в неверном формате."
        )

    return {
        "amount": _positive_amount(raw_payload.get("amount")),
        "payment_date": _optional_date(
            raw_payload.get("payment_date"),
            "Дата оплаты",
        ),
    }
