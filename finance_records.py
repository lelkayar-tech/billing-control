import datetime
import re
from decimal import Decimal, InvalidOperation
from typing import Any


class FinanceValidationError(ValueError):
    """Ошибка проверки данных счёта или платежа."""


UPD_STATUS_VALUES = {
    "waiting",
    "formed",
    "sent",
    "signed",
}


def effective_upd_status(
    stored_status: Any,
    shipment_date: Any,
    today: datetime.date,
) -> str:
    """Возвращает отображаемый статус УПД без изменения данных в БД."""

    normalized_status = str(stored_status or "waiting").strip().lower()

    if normalized_status == "signed":
        return "signed"

    if normalized_status == "sent":
        return "sent"

    try:
        parsed_shipment_date = datetime.date.fromisoformat(
            str(shipment_date or "")
        )
    except ValueError:
        return "waiting"

    return "formed" if parsed_shipment_date <= today else "waiting"


def invoice_base_number(invoice_no: Any) -> str:
    """
    Возвращает базовую часть номера счёта без числового суффикса.

    ОМ-13290/1 и ОМ-13290/2 относятся к базе ОМ-13290.
    Произвольные окончания после косой черты не изменяются.
    """

    normalized_invoice_no = str(invoice_no or "").strip()

    if not normalized_invoice_no:
        return ""

    match = re.fullmatch(r"(.+)/([1-9]\d*)", normalized_invoice_no)
    return match.group(1).strip() if match else normalized_invoice_no


def suggest_next_invoice_number(
    invoices: list[dict[str, Any]],
) -> str:
    """
    Предлагает следующий номер в серии уже существующих счетов.

    Например, после ОМ-13290/1 возвращает ОМ-13290/2. Номер
    приложения не используется, потому что он может не совпадать
    с номером счёта.
    """

    numbered_invoices: list[tuple[int, int, str]] = []

    for position, invoice in enumerate(invoices):
        invoice_no = str(invoice.get("invoice_no") or "").strip()

        if not invoice_no:
            continue

        try:
            invoice_id = int(invoice.get("id"))
        except (TypeError, ValueError):
            invoice_id = -1

        numbered_invoices.append((invoice_id, position, invoice_no))

    if not numbered_invoices:
        return ""

    _, _, reference_invoice_no = max(numbered_invoices)
    reference_base = invoice_base_number(reference_invoice_no)
    pattern = re.compile(
        rf"^{re.escape(reference_base)}/([1-9]\d*)$",
        re.IGNORECASE,
    )
    suffixes: list[int] = []

    for invoice in invoices:
        invoice_no = str(invoice.get("invoice_no") or "").strip()
        match = pattern.fullmatch(invoice_no)

        if match:
            suffixes.append(int(match.group(1)))

    # Ненумерованный счёт считается первым в серии. При добавлении
    # следующего предлагаем /2; существующий первый счёт при
    # необходимости можно переименовать в /1.
    next_suffix = max(suffixes, default=1) + 1
    return f"{reference_base}/{next_suffix}"


def _required_text(value: Any, label: str) -> str:
    normalized = str(value or "").strip()

    if not normalized:
        raise FinanceValidationError(f"Заполните поле «{label}».")

    return normalized


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


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


def _non_negative_amount(value: Any, label: str) -> str:
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
            f"Поле «{label}» должно содержать число."
        ) from None

    if not amount.is_finite() or amount < 0:
        raise FinanceValidationError(
            f"Поле «{label}» не может быть отрицательным."
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


def normalize_application_signed_at_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    """Проверяет дату подписания приложения."""

    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Дата подписания передана в неверном формате."
        )

    return {
        "signed_at": _optional_date(
            raw_payload.get("signed_at"),
            "Дата подписания",
        ),
    }


def normalize_application_sent_at_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    """Проверяет дату отправки приложения."""

    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Дата отправки передана в неверном формате."
        )

    return {
        "sent_at": _optional_date(
            raw_payload.get("sent_at"),
            "Дата отправки",
        ),
    }


def normalize_application_card_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    """Проверяет все редактируемые поля карточки приложения."""

    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Данные приложения переданы в неверном формате."
        )

    try:
        legal_entity_id = int(raw_payload.get("legal_entity_id"))
    except (TypeError, ValueError):
        raise FinanceValidationError(
            "Выберите юридическое лицо."
        ) from None

    if legal_entity_id <= 0:
        raise FinanceValidationError(
            "Выберите юридическое лицо."
        )

    return {
        "app_no": _optional_text(raw_payload.get("app_no")),
        "legal_entity_id": legal_entity_id,
        "content": _required_text(
            raw_payload.get("content"),
            "Содержание",
        ),
        "total_sum": _non_negative_amount(
            raw_payload.get("total_sum"),
            "Сумма приложения",
        ),
        "sent_at": _optional_date(
            raw_payload.get("sent_at"),
            "Дата отправки",
        ),
        "signed_at": _optional_date(
            raw_payload.get("signed_at"),
            "Дата подписания",
        ),
    }


def normalize_invoice_workflow_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    """Проверяет упрощённую строку счёта этапа 5."""

    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Данные счёта переданы в неверном формате."
        )

    is_paid = raw_payload.get("is_paid")

    if not isinstance(is_paid, bool):
        raise FinanceValidationError(
            "Отметка оплаты передана в неверном формате."
        )

    upd_status = str(raw_payload.get("upd_status") or "").strip().lower()

    if upd_status not in UPD_STATUS_VALUES:
        raise FinanceValidationError(
            "Выберите допустимый статус УПД."
        )

    return {
        "invoice_no": _required_text(
            raw_payload.get("invoice_no"),
            "Номер счёта",
        ),
        "amount": _positive_amount(raw_payload.get("amount")),
        "due_date": _optional_date(
            raw_payload.get("due_date"),
            "Срок оплаты",
        ),
        "shipment_date": _optional_date(
            raw_payload.get("shipment_date"),
            "Дата отгрузки",
        ),
        "is_paid": is_paid,
        "upd_status": upd_status,
    }


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


def normalize_upd_payload(raw_payload: Any) -> dict[str, Any]:
    """Проверяет ручные поля УПД и связанную дату отгрузки счёта."""

    if not isinstance(raw_payload, dict):
        raise FinanceValidationError(
            "Данные УПД переданы в неверном формате."
        )

    return {
        "upd_no": _optional_text(raw_payload.get("upd_no")),
        "shipment_date": _optional_date(
            raw_payload.get("shipment_date"),
            "Дата УПД",
        ),
        "sent_at": _optional_date(
            raw_payload.get("sent_at"),
            "Дата отправки УПД",
        ),
        "signed_at": _optional_date(
            raw_payload.get("signed_at"),
            "Дата подписания УПД",
        ),
    }
