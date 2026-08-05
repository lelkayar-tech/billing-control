import datetime
import html
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client

from app_mode import is_request_allowed, resolve_app_mode
from finance_records import (
    FinanceValidationError,
    invoice_base_number,
    normalize_application_card_payload,
    normalize_application_sent_at_payload,
    normalize_application_signed_at_payload,
    normalize_invoice_payload,
    normalize_invoice_workflow_payload,
    normalize_payment_payload,
    normalize_upd_payload,
    suggest_next_invoice_number,
)
from periods_photos import (
    InputValidationError,
    build_filter_url,
    normalize_periods,
    normalize_photo_report_payload,
    photo_report_is_overdue,
    report_matches_state,
)


# --------------------------------------------------
# Настройки
# --------------------------------------------------

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")
APP_MODE = resolve_app_mode(os.getenv("APP_MODE"))
IS_EDIT_MODE = APP_MODE == "edit"

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError(
        "В .env должны быть заполнены SUPABASE_URL "
        "и SUPABASE_SECRET_KEY"
    )

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SECRET_KEY,
)

app = FastAPI(title="Billing Control v2")


@app.middleware("http")
async def block_mutations_in_read_mode(request: Request, call_next):
    """
    Клиентский режим защищён на сервере.

    Любой текущий или будущий изменяющий HTTP-маршрут блокируется,
    пока APP_MODE явно не установлен в edit.
    """

    if not is_request_allowed(APP_MODE, request.method):
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "Изменение данных запрещено: "
                    "приложение работает в режиме APP_MODE=read."
                ),
                "app_mode": APP_MODE,
            },
        )

    return await call_next(request)


MONTHS = {
    1: "ЯНВ",
    2: "ФЕВ",
    3: "МАР",
    4: "АПР",
    5: "МАЙ",
    6: "ИЮН",
    7: "ИЮЛ",
    8: "АВГ",
    9: "СЕН",
    10: "ОКТ",
    11: "НОЯ",
    12: "ДЕК",
}

MONTH_ORDER = list(MONTHS.values())


# --------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------

def fetch_all(table: str, columns: str = "*") -> list[dict[str, Any]]:
    """Загружает все строки таблицы порциями."""

    rows: list[dict[str, Any]] = []
    page_size = 1000
    start = 0

    while True:
        response = (
            supabase
            .table(table)
            .select(columns)
            .range(start, start + page_size - 1)
            .execute()
        )

        batch = response.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def format_money(value: Any) -> str:
    amount = money(value)
    return f"{amount:,.2f}".replace(",", " ").replace(".00", "")


def escape(value: Any) -> str:
    return html.escape(str(value or ""))


def parse_date(value: Any) -> datetime.date | None:
    if not value:
        return None

    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def format_date(value: Any) -> str:
    date_value = parse_date(value)

    if date_value is None:
        return "—"

    return date_value.strftime("%d.%m.%Y")


def period_label(
    periods: list[dict[str, Any]],
) -> str:
    if not periods:
        return "—"

    labels: list[str] = []

    for period in sorted(
        periods,
        key=lambda row: (
            row.get("start_date") or "",
            row.get("end_date") or "",
        ),
    ):
        start = parse_date(period.get("start_date"))
        end = parse_date(period.get("end_date"))

        if start is None or end is None:
            continue

        if start.year == end.year:
            if start.month == end.month:
                labels.append(MONTHS.get(start.month, str(start.month)))
            else:
                labels.append(
                    f"{MONTHS.get(start.month, start.month)}–"
                    f"{MONTHS.get(end.month, end.month)}"
                )
        else:
            labels.append(
                f"{MONTHS.get(start.month, start.month)} {start.year}–"
                f"{MONTHS.get(end.month, end.month)} {end.year}"
            )

    return ", ".join(labels) if labels else "—"


def get_application_status(
    application: dict[str, Any],
    invoices: list[dict[str, Any]],
    today: datetime.date,
) -> tuple[str, str]:
    """
    Возвращает:
    - текст статуса;
    - CSS-класс статуса.
    """

    if invoices:
        if all(bool(invoice.get("is_paid")) for invoice in invoices):
            return "ОПЛАЧЕНО", "status-paid"

        if any(
            not bool(invoice.get("is_paid"))
            and (parse_date(invoice.get("due_date")) or today) < today
            for invoice in invoices
            if invoice.get("due_date")
        ):
            return "ПРОСРОЧКА", "status-overdue"

    if application.get("signed_at"):
        return "ПОДПИСАНО", "status-signed"

    if application.get("sent_at"):
        return "ОТПРАВЛЕНО", "status-sent"

    return "—", "status-empty"


def get_sort_key(
    application: dict[str, Any],
    periods: list[dict[str, Any]],
) -> tuple[str, str]:
    latest_end = max(
        (
            str(period.get("end_date") or "")
            for period in periods
        ),
        default="",
    )

    app_no = str(application.get("app_no") or "")

    return latest_end, app_no


def mutation_error(
    detail: str,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
    )


def unwrap_rpc_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict)
    ):
        return value[0]

    raise RuntimeError(
        "Supabase вернула неожиданный результат сохранения периодов."
    )


# --------------------------------------------------
# Изменение счетов и платежей
# --------------------------------------------------

async def read_json_payload(
    request: Request,
    error_message: str,
) -> dict[str, Any] | JSONResponse:
    try:
        value = await request.json()
    except Exception:
        return mutation_error(error_message, 400)

    if not isinstance(value, dict):
        return mutation_error(error_message, 400)

    return value


def fetch_application_for_mutation(
    application_id: int,
) -> dict[str, Any] | None:
    response = (
        supabase
        .table("applications")
        .select(
            "id,legal_entity_id,app_no,content,total_sum,sent_at,signed_at"
        )
        .eq("id", application_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def fetch_invoice_for_mutation(
    application_id: int,
    invoice_id: int,
) -> dict[str, Any] | None:
    response = (
        supabase
        .table("invoices")
        .select(
            "id,application_id,legal_entity_id,invoice_no,base_invoice_no"
        )
        .eq("id", invoice_id)
        .eq("application_id", application_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


@app.put("/applications/{application_id}")
async def update_application_card(
    application_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные приложения.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_application_card_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        existing = fetch_application_for_mutation(application_id)

        if existing is None:
            return mutation_error("Приложение не найдено.", 404)

        entity = (
            supabase
            .table("legal_entities")
            .select("id")
            .eq("id", payload["legal_entity_id"])
            .limit(1)
            .execute()
        )

        if not entity.data:
            return mutation_error("Юридическое лицо не найдено.", 422)

        supabase.rpc(
            "save_application_card",
            {
                "p_application_id": application_id,
                "p_app_no": payload["app_no"],
                "p_legal_entity_id": payload["legal_entity_id"],
                "p_content": payload["content"],
                "p_total_sum": payload["total_sum"],
                "p_sent_at": payload["sent_at"],
                "p_signed_at": payload["signed_at"],
            },
        ).execute()

        verified = (
            supabase
            .table("applications")
            .select(
                "id,legal_entity_id,app_no,content,total_sum,"
                "sent_at,signed_at"
            )
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось сохранить приложение. Проверьте введённые данные.",
            409,
        )

    if not verified.data:
        return mutation_error(
            "Не удалось подтвердить сохранение приложения.",
            500,
        )

    return JSONResponse(
        content={
            "status": "saved",
            "application": verified.data[0],
        },
    )


async def update_application_date(
    application_id: int,
    request: Request,
    *,
    field_name: str,
    read_error: str,
    save_error: str,
    verify_error: str,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        read_error,
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        if field_name == "sent_at":
            payload = normalize_application_sent_at_payload(raw_payload)
        else:
            payload = normalize_application_signed_at_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        application = fetch_application_for_mutation(application_id)

        if application is None:
            return mutation_error("Приложение не найдено.", 404)

        (
            supabase
            .table("applications")
            .update(payload)
            .eq("id", application_id)
            .execute()
        )

        verified = (
            supabase
            .table("applications")
            .select(f"id,{field_name}")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            save_error,
            500,
        )

    if not verified.data:
        return mutation_error(
            verify_error,
            500,
        )

    return JSONResponse(
        content={
            "status": "saved",
            "application": verified.data[0],
        },
    )


@app.put("/applications/{application_id}/sent-at")
async def update_application_sent_at(
    application_id: int,
    request: Request,
) -> JSONResponse:
    return await update_application_date(
        application_id,
        request,
        field_name="sent_at",
        read_error="Не удалось прочитать дату отправки.",
        save_error="Не удалось сохранить дату отправки.",
        verify_error="Не удалось подтвердить дату отправки.",
    )


@app.put("/applications/{application_id}/signed-at")
async def update_application_signed_at(
    application_id: int,
    request: Request,
) -> JSONResponse:
    return await update_application_date(
        application_id,
        request,
        field_name="signed_at",
        read_error="Не удалось прочитать дату подписания.",
        save_error="Не удалось сохранить дату подписания.",
        verify_error="Не удалось подтвердить дату подписания.",
    )


@app.post("/applications/{application_id}/invoices")
async def create_invoice(
    application_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные счёта.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_invoice_workflow_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        application = fetch_application_for_mutation(application_id)

        if application is None:
            return mutation_error("Приложение не найдено.", 404)

        rpc_response = (
            supabase
            .rpc(
                "create_invoice_workflow",
                {
                    "p_application_id": application_id,
                    "p_invoice_no": payload["invoice_no"],
                    "p_base_invoice_no": invoice_base_number(
                        payload["invoice_no"]
                    ),
                    "p_amount": payload["amount"],
                    "p_due_date": payload["due_date"],
                    "p_shipment_date": payload["shipment_date"],
                    "p_is_paid": payload["is_paid"],
                    "p_upd_status": payload["upd_status"],
                },
            )
            .execute()
        )
        rpc_result = unwrap_rpc_result(rpc_response.data)
        invoice_id = int(rpc_result["invoice_id"])
        inserted = (
            supabase
            .table("invoices")
            .select(
                "id,application_id,invoice_no,amount,due_date,"
                "shipment_date,is_paid"
            )
            .eq("id", invoice_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось добавить счёт. Проверьте, что его номер "
            "не повторяется у этого юридического лица.",
            409,
        )

    if not inserted.data:
        return mutation_error(
            "Не удалось подтвердить добавление счёта.",
            500,
        )

    return JSONResponse(
        status_code=201,
        content={"status": "created", "invoice": inserted.data[0]},
    )


@app.put(
    "/applications/{application_id}/invoices/{invoice_id}"
)
async def update_invoice(
    application_id: int,
    invoice_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные счёта.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_invoice_workflow_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        existing = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if existing is None:
            return mutation_error("Счёт не найден.", 404)

        supabase.rpc(
            "save_invoice_workflow",
            {
                "p_application_id": application_id,
                "p_invoice_id": invoice_id,
                "p_invoice_no": payload["invoice_no"],
                "p_base_invoice_no": invoice_base_number(
                    payload["invoice_no"]
                ),
                "p_amount": payload["amount"],
                "p_due_date": payload["due_date"],
                "p_shipment_date": payload["shipment_date"],
                "p_is_paid": payload["is_paid"],
                "p_upd_status": payload["upd_status"],
            },
        ).execute()

        verified = (
            supabase
            .table("invoices")
            .select(
                "id,invoice_no,amount,due_date,shipment_date,is_paid"
            )
            .eq("id", invoice_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось сохранить счёт. Проверьте, что его номер "
            "не повторяется у этого юридического лица.",
            409,
        )

    if not verified.data:
        return mutation_error(
            "Не удалось подтвердить сохранение счёта.",
            500,
        )

    verified_upd = (
        supabase
        .table("upds")
        .select("invoice_id,status")
        .eq("invoice_id", invoice_id)
        .limit(1)
        .execute()
    )

    if (
        not verified_upd.data
        or verified_upd.data[0].get("status") != payload["upd_status"]
    ):
        return mutation_error(
            "Не удалось подтвердить статус УПД.",
            500,
        )

    return JSONResponse(
        content={
            "status": "saved",
            "invoice": verified.data[0],
            "upd": verified_upd.data[0],
        },
    )


@app.delete(
    "/applications/{application_id}/invoices/{invoice_id}"
)
def delete_invoice(
    application_id: int,
    invoice_id: int,
    confirmed: bool = Query(default=False),
) -> JSONResponse:
    if not confirmed:
        return mutation_error(
            "Удаление счёта требует подтверждения.",
            409,
        )

    try:
        existing = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if existing is None:
            return mutation_error("Счёт не найден.", 404)

        (
            supabase
            .table("invoices")
            .delete()
            .eq("id", invoice_id)
            .eq("application_id", application_id)
            .execute()
        )

        verified = (
            supabase
            .table("invoices")
            .select("id")
            .eq("id", invoice_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error("Не удалось удалить счёт.", 500)

    if verified.data:
        return mutation_error(
            "Не удалось подтвердить удаление счёта.",
            500,
        )

    return JSONResponse(content={"status": "deleted"})


@app.post(
    "/applications/{application_id}/invoices/{invoice_id}/payments"
)
async def create_payment(
    application_id: int,
    invoice_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные платежа.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_payment_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        invoice = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if invoice is None:
            return mutation_error("Счёт не найден.", 404)

        payload["invoice_id"] = invoice_id
        inserted = (
            supabase
            .table("payments")
            .insert(payload)
            .execute()
        )
    except Exception:
        return mutation_error("Не удалось добавить платёж.", 500)

    if not inserted.data:
        return mutation_error(
            "Не удалось подтвердить добавление платежа.",
            500,
        )

    return JSONResponse(
        status_code=201,
        content={"status": "created", "payment": inserted.data[0]},
    )


@app.put(
    "/applications/{application_id}/invoices/{invoice_id}"
    "/payments/{payment_id}"
)
async def update_payment(
    application_id: int,
    invoice_id: int,
    payment_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные платежа.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_payment_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        invoice = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if invoice is None:
            return mutation_error("Счёт не найден.", 404)

        existing = (
            supabase
            .table("payments")
            .select("id")
            .eq("id", payment_id)
            .eq("invoice_id", invoice_id)
            .limit(1)
            .execute()
        )

        if not existing.data:
            return mutation_error("Платёж не найден.", 404)

        (
            supabase
            .table("payments")
            .update(payload)
            .eq("id", payment_id)
            .eq("invoice_id", invoice_id)
            .execute()
        )

        verified = (
            supabase
            .table("payments")
            .select("id,invoice_id,amount,payment_date")
            .eq("id", payment_id)
            .eq("invoice_id", invoice_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error("Не удалось сохранить платёж.", 500)

    if not verified.data:
        return mutation_error(
            "Не удалось подтвердить сохранение платежа.",
            500,
        )

    return JSONResponse(
        content={"status": "saved", "payment": verified.data[0]},
    )


@app.delete(
    "/applications/{application_id}/invoices/{invoice_id}"
    "/payments/{payment_id}"
)
def delete_payment(
    application_id: int,
    invoice_id: int,
    payment_id: int,
    confirmed: bool = Query(default=False),
) -> JSONResponse:
    if not confirmed:
        return mutation_error(
            "Удаление платежа требует подтверждения.",
            409,
        )

    try:
        invoice = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if invoice is None:
            return mutation_error("Счёт не найден.", 404)

        existing = (
            supabase
            .table("payments")
            .select("id")
            .eq("id", payment_id)
            .eq("invoice_id", invoice_id)
            .limit(1)
            .execute()
        )

        if not existing.data:
            return mutation_error("Платёж не найден.", 404)

        (
            supabase
            .table("payments")
            .delete()
            .eq("id", payment_id)
            .eq("invoice_id", invoice_id)
            .execute()
        )

        verified = (
            supabase
            .table("payments")
            .select("id")
            .eq("id", payment_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error("Не удалось удалить платёж.", 500)

    if verified.data:
        return mutation_error(
            "Не удалось подтвердить удаление платежа.",
            500,
        )

    return JSONResponse(content={"status": "deleted"})


# --------------------------------------------------
# Изменение УПД
# --------------------------------------------------

@app.put(
    "/applications/{application_id}/invoices/{invoice_id}/upd"
)
async def update_upd(
    application_id: int,
    invoice_id: int,
    request: Request,
) -> JSONResponse:
    raw_payload = await read_json_payload(
        request,
        "Не удалось прочитать данные УПД.",
    )

    if isinstance(raw_payload, JSONResponse):
        return raw_payload

    try:
        payload = normalize_upd_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        invoice = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if invoice is None:
            return mutation_error("Счёт не найден.", 404)

        supabase.rpc(
            "save_invoice_upd",
            {
                "p_application_id": application_id,
                "p_invoice_id": invoice_id,
                "p_upd_no": payload["upd_no"],
                "p_shipment_date": payload["shipment_date"],
                "p_sent_at": payload["sent_at"],
                "p_signed_at": payload["signed_at"],
            },
        ).execute()

        verified_invoice = (
            supabase
            .table("invoices")
            .select("id,shipment_date")
            .eq("id", invoice_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )
        verified_upd = (
            supabase
            .table("upds")
            .select("id,invoice_id,upd_no,upd_date,sent_at,signed_at")
            .eq("invoice_id", invoice_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось сохранить УПД. Данные не изменены.",
            500,
        )

    if not verified_invoice.data or not verified_upd.data:
        return mutation_error(
            "Не удалось подтвердить сохранение УПД.",
            500,
        )

    saved_invoice = verified_invoice.data[0]
    saved_upd = verified_upd.data[0]

    if saved_upd.get("upd_date") != saved_invoice.get("shipment_date"):
        return mutation_error(
            "Дата УПД не совпала с датой отгрузки счёта.",
            500,
        )

    return JSONResponse(
        content={
            "status": "saved",
            "invoice": saved_invoice,
            "upd": saved_upd,
        },
    )


# --------------------------------------------------
# Изменение периодов и фотоотчётов
# --------------------------------------------------

@app.put("/applications/{application_id}/periods")
async def save_application_periods(
    application_id: int,
    request: Request,
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return mutation_error(
            "Не удалось прочитать список периодов.",
            400,
        )

    try:
        periods = normalize_periods(
            payload.get("periods") if isinstance(payload, dict) else None
        )
    except InputValidationError as error:
        return mutation_error(str(error), 422)

    confirm_remove_filled = (
        isinstance(payload, dict)
        and payload.get("confirm_remove_filled") is True
    )

    try:
        response = supabase.rpc(
            "save_application_periods",
            {
                "p_application_id": application_id,
                "p_periods": periods,
                "p_confirm_remove_filled": confirm_remove_filled,
            },
        ).execute()
        result = unwrap_rpc_result(response.data)
    except Exception:
        return mutation_error(
            "Не удалось сохранить периоды. Данные не изменены.",
            500,
        )

    return JSONResponse(content=result)


@app.put(
    "/applications/{application_id}/photo-reports/{report_id}"
)
async def update_photo_report(
    application_id: int,
    report_id: int,
    request: Request,
) -> JSONResponse:
    try:
        raw_payload = await request.json()
        payload = normalize_photo_report_payload(raw_payload)
    except InputValidationError as error:
        return mutation_error(str(error), 422)
    except Exception:
        return mutation_error(
            "Не удалось прочитать данные фотоотчёта.",
            400,
        )

    try:
        existing = (
            supabase
            .table("photo_reports")
            .select("id")
            .eq("id", report_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )

        if not existing.data:
            return mutation_error("Фотоотчёт не найден.", 404)

        (
            supabase
            .table("photo_reports")
            .update(payload)
            .eq("id", report_id)
            .eq("application_id", application_id)
            .execute()
        )

        verified = (
            supabase
            .table("photo_reports")
            .select("id,url,not_required,comment")
            .eq("id", report_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось сохранить фотоотчёт.",
            500,
        )

    if not verified.data:
        return mutation_error(
            "Не удалось подтвердить сохранение фотоотчёта.",
            500,
        )

    return JSONResponse(
        content={
            "status": "saved",
            "report": verified.data[0],
        },
    )


@app.delete(
    "/applications/{application_id}/photo-reports/{report_id}"
)
def delete_photo_report(
    application_id: int,
    report_id: int,
    confirmed: bool = Query(default=False),
) -> JSONResponse:
    if not confirmed:
        return mutation_error(
            "Удаление строки фотоотчёта требует подтверждения.",
            409,
        )

    try:
        existing = (
            supabase
            .table("photo_reports")
            .select("id")
            .eq("id", report_id)
            .eq("application_id", application_id)
            .limit(1)
            .execute()
        )

        if not existing.data:
            return mutation_error("Фотоотчёт не найден.", 404)

        (
            supabase
            .table("photo_reports")
            .delete()
            .eq("id", report_id)
            .eq("application_id", application_id)
            .execute()
        )
    except Exception:
        return mutation_error(
            "Не удалось удалить строку фотоотчёта.",
            500,
        )

    return JSONResponse(content={"status": "deleted"})


# --------------------------------------------------
# Загрузка модели страницы
# --------------------------------------------------

def load_dashboard_data() -> list[dict[str, Any]]:
    legal_entities = fetch_all(
        "legal_entities",
        "id,name",
    )
    applications = fetch_all(
        "applications",
        "id,legal_entity_id,app_no,content,total_sum,sent_at,signed_at",
    )
    periods = fetch_all(
        "application_periods",
        "id,application_id,start_date,end_date",
    )
    invoices = fetch_all(
        "invoices",
        "id,legal_entity_id,application_id,invoice_no,base_invoice_no,"
        "content,amount,service_start_date,service_end_date,due_date,"
        "shipment_date,is_paid",
    )
    upds = fetch_all(
        "upds",
        "id,invoice_id,upd_no,upd_date,sent_at,signed_at,status",
    )
    photo_reports = fetch_all(
        "photo_reports",
        "id,application_id,report_year,report_month,url,not_required,comment",
    )

    entity_name_by_id = {
        row["id"]: str(row.get("name") or "")
        for row in legal_entities
    }

    periods_by_application: dict[int, list[dict[str, Any]]] = defaultdict(list)
    invoices_by_application: dict[int, list[dict[str, Any]]] = defaultdict(list)
    upds_by_invoice: dict[int, dict[str, Any]] = {}
    reports_by_application: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in periods:
        periods_by_application[row["application_id"]].append(row)

    for row in invoices:
        application_id = row.get("application_id")

        if application_id is not None:
            invoices_by_application[application_id].append(row)

    for row in upds:
        upds_by_invoice[row["invoice_id"]] = row

    for row in photo_reports:
        reports_by_application[row["application_id"]].append(row)

    dashboard_rows: list[dict[str, Any]] = []

    for application in applications:
        application_id = application["id"]
        app_invoices = sorted(
            invoices_by_application.get(application_id, []),
            key=lambda row: str(row.get("invoice_no") or ""),
        )

        dashboard_rows.append({
            "application": application,
            "entity_name": entity_name_by_id.get(
                application.get("legal_entity_id"),
                "—",
            ),
            "legal_entities": legal_entities,
            "periods": periods_by_application.get(application_id, []),
            "invoices": app_invoices,
            "upds_by_invoice": upds_by_invoice,
            "photo_reports": sorted(
                reports_by_application.get(application_id, []),
                key=lambda row: (
                    int(row.get("report_year") or 0),
                    int(row.get("report_month") or 0),
                ),
            ),
        })

    dashboard_rows.sort(
        key=lambda item: get_sort_key(
            item["application"],
            item["periods"],
        ),
        reverse=True,
    )

    return dashboard_rows


# --------------------------------------------------
# HTML-фрагменты
# --------------------------------------------------

def application_date_html(
    application: dict[str, Any],
    *,
    field_name: str,
    label: str,
    action_label: str,
    input_label: str,
    editor_key: str,
    input_class: str,
    save_function: str,
) -> str:
    application_id = int(application["id"])
    date_value = escape(application.get(field_name) or "")
    edit_button = ""
    editor = ""

    if IS_EDIT_MODE:
        edit_button = f"""
        <button
            type="button"
            class="icon-action"
            title="{action_label}"
            aria-label="{action_label}"
            onclick="showEditor(
                'application-{editor_key}-editor-{application_id}'
            )"
        >✎</button>
        """
        editor = f"""
        <div
            id="application-{editor_key}-editor-{application_id}"
            class="application-date-editor hidden"
        >
            <div class="application-date-editor-fields">
                <label>
                    <span>{input_label}</span>
                    <input
                        type="date"
                        class="{input_class}"
                        value="{date_value}"
                    >
                </label>
                <div class="application-date-editor-actions">
                    <button
                        type="button"
                        class="button button-primary button-small"
                        onclick="{save_function}({application_id})"
                    >
                        Сохранить
                    </button>
                    <button
                        type="button"
                        class="button button-secondary button-small"
                        onclick="hideEditor(
                            'application-{editor_key}-editor-{application_id}'
                        )"
                    >
                        Отмена
                    </button>
                </div>
            </div>
        </div>
        """

    return f"""
    <div class="info-row application-date-row">
        <span>{label}</span>
        <div class="application-date-value">
            <strong>
                {format_date(application.get(field_name))}
            </strong>
            {edit_button}
        </div>
    </div>
    {editor}
    <div
        id="application-date-message-{editor_key}-{application_id}"
        class="form-message application-date-message"
    ></div>
    """


def application_sent_at_html(
    application: dict[str, Any],
) -> str:
    return application_date_html(
        application,
        field_name="sent_at",
        label="Отправлено",
        action_label="Указать дату отправки",
        input_label="Дата отправки",
        editor_key="sent-at",
        input_class="application-sent-at",
        save_function="saveApplicationSentAt",
    )


def application_signed_at_html(
    application: dict[str, Any],
) -> str:
    return application_date_html(
        application,
        field_name="signed_at",
        label="Подписано",
        action_label="Указать дату подписания",
        input_label="Дата подписания",
        editor_key="signed-at",
        input_class="application-signed-at",
        save_function="saveApplicationSignedAt",
    )


def application_card_html(
    application: dict[str, Any],
    legal_entities: list[dict[str, Any]],
    entity_name: str,
) -> str:
    application_id = int(application["id"])
    application_periods = application.get("_periods")

    if not isinstance(application_periods, list):
        application_periods = []

    periods_block = periods_html(application_id, application_periods)
    edit_action = ""
    editor = ""

    if IS_EDIT_MODE:
        options = "".join(
            f"""
            <option
                value="{int(entity["id"])}"
                {
                    "selected"
                    if int(entity["id"])
                    == int(application["legal_entity_id"])
                    else ""
                }
            >{escape(entity.get("name") or "—")}</option>
            """
            for entity in legal_entities
        )
        edit_action = f"""
        <button
            type="button"
            class="button button-secondary button-small"
            onclick="showEditor('application-editor-{application_id}')"
        >
            Изменить
        </button>
        """
        editor = f"""
        <div
            id="application-editor-{application_id}"
            class="application-card-editor hidden"
        >
            <div class="application-editor-grid">
                <label>
                    <span>Номер</span>
                    <input
                        type="text"
                        class="application-number"
                        value="{escape(application.get("app_no") or "")}"
                    >
                </label>
                <label>
                    <span>Юридическое лицо</span>
                    <select class="application-entity">
                        {options}
                    </select>
                </label>
                <label class="application-content-field">
                    <span>Содержание</span>
                    <input
                        type="text"
                        class="application-content"
                        value="{escape(application.get("content") or "")}"
                        required
                    >
                </label>
                <label>
                    <span>Сумма приложения</span>
                    <input
                        type="text"
                        inputmode="decimal"
                        class="application-total"
                        value="{format(money(application.get("total_sum")), "f")}"
                        required
                    >
                </label>
                <label>
                    <span>Отправлено</span>
                    <input
                        type="date"
                        class="application-sent-at"
                        value="{escape(application.get("sent_at") or "")}"
                    >
                </label>
                <label>
                    <span>Подписано</span>
                    <input
                        type="date"
                        class="application-signed-at"
                        value="{escape(application.get("signed_at") or "")}"
                    >
                </label>
            </div>
            <div class="editor-actions">
                <button
                    type="button"
                    class="button button-primary"
                    onclick="saveApplicationCard({application_id})"
                >
                    Сохранить
                </button>
                <button
                    type="button"
                    class="button button-secondary"
                    onclick="hideEditor('application-editor-{application_id}')"
                >
                    Отмена
                </button>
            </div>
        </div>
        """

    return f"""
    <div class="panel-heading">
        <h3>Приложение</h3>
        {edit_action}
    </div>
    <div class="application-overview">
        <div class="overview-value">
            <span>Номер</span>
            <strong>{escape(application.get("app_no") or "—")}</strong>
        </div>
        <div class="overview-value">
            <span>Юридическое лицо</span>
            <strong>{escape(entity_name)}</strong>
        </div>
        <div class="overview-value overview-content">
            <span>Содержание</span>
            <strong>{escape(application.get("content") or "—")}</strong>
        </div>
        {periods_block}
        <div class="overview-value overview-money">
            <span>Сумма приложения</span>
            <strong>{format_money(application.get("total_sum"))} ₽</strong>
        </div>
        <div class="overview-value">
            <span>Отправлено</span>
            <strong>{format_date(application.get("sent_at"))}</strong>
        </div>
        <div class="overview-value">
            <span>Подписано</span>
            <strong>{format_date(application.get("signed_at"))}</strong>
        </div>
    </div>
    {editor}
    <div
        id="application-message-{application_id}"
        class="form-message"
    ></div>
    """


def invoice_form_html(
    application_id: int,
    invoice: dict[str, Any] | None,
    upd: dict[str, Any] | None = None,
    suggested_invoice_no: str = "",
) -> str:
    invoice_id = int(invoice["id"]) if invoice else None
    form_key = str(invoice_id) if invoice_id is not None else "new"
    invoice_no = escape(
        invoice.get("invoice_no") if invoice else suggested_invoice_no
    )
    amount = (
        format(money(invoice.get("amount")), "f")
        if invoice
        else ""
    )
    due_date = escape(invoice.get("due_date") if invoice else "")
    shipment_date = escape(
        invoice.get("shipment_date") if invoice else ""
    )
    checked = "checked" if invoice and invoice.get("is_paid") else ""
    current_upd_status = str(
        (upd or {}).get("status") or "waiting"
    )
    upd_options = "".join(
        f"""
        <option
            value="{value}"
            {"selected" if value == current_upd_status else ""}
        >{label}</option>
        """
        for value, label in (
            ("waiting", "Ожидает"),
            ("formed", "Сформирован"),
            ("sent", "Отправлен"),
            ("signed", "Подписан"),
        )
    )
    action_label = "Сохранить" if invoice else "Добавить счёт"
    invoice_argument = str(invoice_id) if invoice_id is not None else "null"

    return f"""
    <div
        id="invoice-form-{application_id}-{form_key}"
        class="finance-editor invoice-editor hidden"
    >
        <div class="finance-editor-fields invoice-editor-fields">
            <label>
                <span>Номер счёта</span>
                <input
                    type="text"
                    class="invoice-number"
                    value="{invoice_no}"
                    required
                >
            </label>
            <label>
                <span>Сумма</span>
                <input
                    type="text"
                    inputmode="decimal"
                    class="invoice-amount"
                    value="{amount}"
                    required
                >
            </label>
            <label>
                <span>Срок оплаты</span>
                <input
                    type="date"
                    class="invoice-due-date"
                    value="{due_date}"
                >
            </label>
            <label>
                <span>Дата отгрузки</span>
                <input
                    type="date"
                    class="invoice-shipment-date"
                    value="{shipment_date}"
                >
            </label>
            <label class="invoice-paid-field checkbox-label">
                <input
                    type="checkbox"
                    class="invoice-paid"
                    {checked}
                >
                <span>Оплачено</span>
            </label>
            <label>
                <span>УПД</span>
                <select class="invoice-upd-status">
                    {upd_options}
                </select>
            </label>
        </div>
        <div class="finance-editor-actions">
            <button
                type="button"
                class="button button-primary button-small"
                onclick="saveInvoice({application_id}, {invoice_argument})"
            >
                {action_label}
            </button>
            <button
                type="button"
                class="button button-secondary button-small"
                onclick="hideEditor(
                    'invoice-form-{application_id}-{form_key}'
                )"
            >
                Отмена
            </button>
        </div>
        <div
            id="invoice-message-{application_id}-{form_key}"
            class="form-message"
        ></div>
    </div>
    """


def payment_form_html(
    application_id: int,
    invoice_id: int,
    payment: dict[str, Any] | None,
) -> str:
    payment_id = int(payment["id"]) if payment else None
    form_key = str(payment_id) if payment_id is not None else "new"
    amount = (
        format(money(payment.get("amount")), "f")
        if payment
        else ""
    )
    payment_date = escape(
        payment.get("payment_date") if payment else ""
    )
    action_label = "Сохранить" if payment else "Добавить платёж"
    payment_argument = str(payment_id) if payment_id is not None else "null"

    return f"""
    <div
        id="payment-form-{invoice_id}-{form_key}"
        class="finance-editor payment-editor hidden"
    >
        <div class="finance-editor-fields payment-editor-fields">
            <label>
                <span>Сумма</span>
                <input
                    type="text"
                    inputmode="decimal"
                    class="payment-amount"
                    value="{amount}"
                    required
                >
            </label>
            <label>
                <span>Дата оплаты</span>
                <input
                    type="date"
                    class="payment-date"
                    value="{payment_date}"
                >
            </label>
        </div>
        <div class="finance-editor-actions">
            <button
                type="button"
                class="button button-primary button-small"
                onclick="savePayment(
                    {application_id},
                    {invoice_id},
                    {payment_argument}
                )"
            >
                {action_label}
            </button>
            <button
                type="button"
                class="button button-secondary button-small"
                onclick="hideEditor(
                    'payment-form-{invoice_id}-{form_key}'
                )"
            >
                Отмена
            </button>
        </div>
        <div
            id="payment-message-{invoice_id}-{form_key}"
            class="form-message"
        ></div>
    </div>
    """


def payment_lines_html(
    application_id: int,
    invoice_id: int,
    payments: list[dict[str, Any]],
) -> str:
    rows = ""

    for payment in payments:
        payment_id = int(payment["id"])
        actions = ""

        if IS_EDIT_MODE:
            actions = f"""
            <span class="inline-actions">
                <button
                    type="button"
                    class="icon-action"
                    title="Изменить платёж"
                    aria-label="Изменить платёж"
                    onclick="showEditor(
                        'payment-form-{invoice_id}-{payment_id}'
                    )"
                >✎</button>
                <button
                    type="button"
                    class="icon-action icon-action-danger"
                    title="Удалить платёж"
                    aria-label="Удалить платёж"
                    onclick="deletePayment(
                        {application_id},
                        {invoice_id},
                        {payment_id}
                    )"
                >×</button>
            </span>
            """

        rows += f"""
        <div class="payment-line">
            <div class="payment-line-view">
                <strong>{format_money(payment.get("amount"))} ₽</strong>
                <span>{format_date(payment.get("payment_date"))}</span>
                {actions}
            </div>
        </div>
        """

    if not rows:
        rows = '<div class="muted">Платежей нет</div>'

    if IS_EDIT_MODE:
        rows += f"""
        <button
            type="button"
            class="add-inline-button"
            onclick="showEditor('payment-form-{invoice_id}-new')"
        >
            + платёж
        </button>
        """

    return rows


def payment_editors_html(
    application_id: int,
    invoice_id: int,
    payments: list[dict[str, Any]],
) -> str:
    """Редакторы платежей на всю ширину строки счёта."""

    if not IS_EDIT_MODE:
        return ""

    editors = "".join(
        payment_form_html(
            application_id,
            invoice_id,
            payment,
        )
        for payment in payments
    )
    editors += payment_form_html(application_id, invoice_id, None)

    return f"""
    <div class="finance-payment-editors">
        {editors}
    </div>
    """


def finance_table_html(
    application: dict[str, Any],
    invoices: list[dict[str, Any]],
    upds_by_invoice: dict[int, dict[str, Any]],
    today: datetime.date,
) -> str:
    application_id = int(application["id"])
    suggested_invoice_no = suggest_next_invoice_number(invoices)
    rows = ""
    upd_labels = {
        "waiting": "Ожидает",
        "formed": "Сформирован",
        "sent": "Отправлен",
        "signed": "Подписан",
    }

    for invoice in invoices:
        invoice_id = int(invoice["id"])
        invoice_amount = money(invoice.get("amount"))
        due_date = parse_date(invoice.get("due_date"))
        is_overdue = (
            due_date is not None
            and due_date < today
            and not invoice.get("is_paid")
        )

        if invoice.get("is_paid"):
            payment_status = (
                '<span class="workflow-status workflow-paid">'
                '<span aria-hidden="true">✓</span> Оплачен</span>'
            )
        elif is_overdue:
            payment_status = (
                '<span class="workflow-status workflow-overdue">'
                "Просрочен</span>"
            )
        else:
            payment_status = (
                '<span class="workflow-status workflow-waiting">'
                "Ожидает оплаты</span>"
            )

        upd = upds_by_invoice.get(invoice_id)
        upd_status = str((upd or {}).get("status") or "waiting")
        shipment_date = parse_date(invoice.get("shipment_date"))
        upd_needs_attention = (
            upd_status == "waiting"
            and shipment_date is not None
            and shipment_date <= today
        )
        upd_status_html = (
            '<span class="workflow-status '
            + (
                "workflow-overdue"
                if upd_needs_attention
                else f"workflow-upd-{escape(upd_status)}"
            )
            + '">'
            + escape(upd_labels.get(upd_status, "Ожидает"))
            + "</span>"
        )

        if upd_needs_attention:
            upd_status_html += (
                '<span class="upd-reminder">Пора сформировать</span>'
            )

        invoice_edit_action = ""
        invoice_delete_action = ""

        if IS_EDIT_MODE:
            invoice_edit_action = f"""
            <button
                type="button"
                class="icon-action"
                title="Изменить счёт"
                aria-label="Изменить счёт"
                onclick="showEditor(
                    'invoice-form-{application_id}-{invoice_id}'
                )"
            >✎</button>
            """
            invoice_delete_action = f"""
            <button
                type="button"
                class="icon-action icon-action-danger invoice-delete-action"
                title="Удалить счёт"
                aria-label="Удалить счёт"
                onclick="deleteInvoice(
                    {application_id},
                    {invoice_id}
                )"
            >×</button>
            """

        rows += f"""
        <div class="finance-entry workflow-entry">
            <div class="finance-row workflow-row">
                <strong class="workflow-invoice-no">
                    {escape(invoice.get("invoice_no") or "Без номера")}
                </strong>
                <strong class="workflow-amount">
                    {format_money(invoice_amount)} ₽
                </strong>
                <span class="workflow-date">
                    {format_date(invoice.get("due_date"))}
                </span>
                <div class="workflow-payment">
                    {payment_status}
                </div>
                <span class="workflow-date">
                    {format_date(invoice.get("shipment_date"))}
                </span>
                <div class="workflow-upd">
                    {upd_status_html}
                </div>
                <div class="workflow-actions">
                    {invoice_edit_action}
                    {invoice_delete_action}
                </div>
            </div>
            {
                invoice_form_html(
                    application_id,
                    invoice,
                    upd,
                )
                if IS_EDIT_MODE
                else ""
            }
        </div>
        """

    if not rows:
        rows = '<div class="finance-empty muted">Счетов нет</div>'

    add_invoice = ""

    if IS_EDIT_MODE:
        add_invoice = f"""
        <div class="finance-add-row">
            <button
                type="button"
                class="button button-secondary button-small"
                onclick="showEditor(
                    'invoice-form-{application_id}-new'
                )"
            >
                + Добавить счёт
            </button>
        </div>
        {
            invoice_form_html(
                application_id,
                None,
                None,
                suggested_invoice_no,
            )
        }
        """

    return f"""
    <div class="finance-table workflow-table">
        <div class="finance-header workflow-header">
            <span>Счёт</span>
            <span>Сумма</span>
            <span>Срок оплаты</span>
            <span>Оплата</span>
            <span>Дата отгрузки</span>
            <span>УПД</span>
            <span></span>
        </div>
        {rows}
        {add_invoice}
        <div
            id="finance-message-{application_id}"
            class="form-message finance-panel-message"
        ></div>
    </div>
    """


def upd_status_html(upd: dict[str, Any] | None) -> str:
    if upd and upd.get("signed_at"):
        return '<span class="mini-status mini-paid">ПОДПИСАНО</span>'

    if upd and upd.get("sent_at"):
        return '<span class="mini-status mini-waiting">ОТПРАВЛЕНО</span>'

    if upd and (upd.get("upd_no") or upd.get("upd_date")):
        return '<span class="mini-status mini-ready">ОФОРМЛЕНО</span>'

    return '<span class="mini-status mini-empty">НЕ ЗАПОЛНЕНО</span>'


def upd_form_html(
    application_id: int,
    invoice: dict[str, Any],
    upd: dict[str, Any] | None,
) -> str:
    invoice_id = int(invoice["id"])
    upd_no = escape(upd.get("upd_no") if upd else "")
    shipment_date = escape(
        invoice.get("shipment_date")
        or (upd.get("upd_date") if upd else "")
    )
    sent_at = escape(upd.get("sent_at") if upd else "")
    signed_at = escape(upd.get("signed_at") if upd else "")

    return f"""
    <div
        id="upd-form-{invoice_id}"
        class="upd-editor hidden"
    >
        <div class="upd-editor-fields">
            <label>
                <span>Номер УПД</span>
                <input
                    type="text"
                    class="upd-number"
                    value="{upd_no}"
                >
            </label>
            <label>
                <span>Дата УПД</span>
                <input
                    type="date"
                    class="upd-shipment-date"
                    value="{shipment_date}"
                >
            </label>
            <label>
                <span>Отправлено</span>
                <input
                    type="date"
                    class="upd-sent-at"
                    value="{sent_at}"
                >
            </label>
            <label>
                <span>Подписано</span>
                <input
                    type="date"
                    class="upd-signed-at"
                    value="{signed_at}"
                >
            </label>
        </div>
        <div class="finance-editor-actions">
            <button
                type="button"
                class="button button-primary button-small"
                onclick="saveUpd({application_id}, {invoice_id})"
            >
                Сохранить
            </button>
            <button
                type="button"
                class="button button-secondary button-small"
                onclick="hideEditor('upd-form-{invoice_id}')"
            >
                Отмена
            </button>
        </div>
        <div
            id="upd-message-{invoice_id}"
            class="form-message"
        ></div>
    </div>
    """


def upds_html(
    application_id: int,
    invoices: list[dict[str, Any]],
    upds_by_invoice: dict[int, dict[str, Any]],
) -> str:
    if not invoices:
        return '<div class="muted">Сначала добавьте счёт</div>'

    rows = ""

    for invoice in invoices:
        invoice_id = int(invoice["id"])
        upd = upds_by_invoice.get(invoice_id)
        edit_action = ""

        if IS_EDIT_MODE:
            edit_action = f"""
            <button
                type="button"
                class="icon-action"
                title="Заполнить или изменить УПД"
                aria-label="Заполнить или изменить УПД"
                onclick="showEditor('upd-form-{invoice_id}')"
            >✎</button>
            """

        rows += f"""
        <div class="upd-record">
            <div class="upd-record-view">
                <div class="upd-value upd-invoice-value">
                    <span>Счёт</span>
                    <strong>{escape(invoice.get("invoice_no") or "—")}</strong>
                </div>
                <div class="upd-value">
                    <span>Номер УПД</span>
                    <strong>{escape(upd.get("upd_no") if upd else "—")}</strong>
                </div>
                <div class="upd-value upd-amount-value">
                    <span>Сумма</span>
                    <strong>{format_money(invoice.get("amount"))} ₽</strong>
                </div>
                <div class="upd-value">
                    <span>Дата УПД</span>
                    <strong>
                        {
                            format_date(
                                invoice.get("shipment_date")
                                or (upd.get("upd_date") if upd else None)
                            )
                        }
                    </strong>
                </div>
                <div class="upd-value">
                    <span>Отправлено</span>
                    <strong>
                        {format_date(upd.get("sent_at") if upd else None)}
                    </strong>
                </div>
                <div class="upd-value">
                    <span>Подписано</span>
                    <strong>
                        {format_date(upd.get("signed_at") if upd else None)}
                    </strong>
                </div>
                <div class="upd-state">
                    {upd_status_html(upd)}
                    {edit_action}
                </div>
            </div>
            {
                upd_form_html(
                    application_id,
                    invoice,
                    upd,
                )
                if IS_EDIT_MODE
                else ""
            }
        </div>
        """

    return rows


def periods_html(
    application_id: int,
    periods: list[dict[str, Any]],
) -> str:
    sorted_periods = sorted(
        periods,
        key=lambda row: (
            row.get("start_date") or "",
            row.get("end_date") or "",
        ),
    )

    period_value = "; ".join(
        (
            f"{format_date(period.get('start_date'))} — "
            f"{format_date(period.get('end_date'))}"
        )
        for period in sorted_periods
    )

    if not period_value:
        period_value = "—"

    edit_button = ""

    if IS_EDIT_MODE:
        edit_button = f"""
        <button
            type="button"
            class="icon-action"
            title="Редактировать период размещения"
            aria-label="Редактировать период размещения"
            onclick="showEditor('period-editor-{application_id}')"
        >✎</button>
        """

    period_overview = f"""
    <div class="overview-value overview-period">
        <div class="overview-period-heading">
            <span>Период размещения</span>
            {edit_button}
        </div>
        <strong class="period-view">{escape(period_value)}</strong>
    </div>
    """

    if not IS_EDIT_MODE:
        return period_overview

    editor_rows = ""

    for period in sorted_periods:
        editor_rows += f"""
        <div class="period-edit-row">
            <label>
                <span>Начало</span>
                <input
                    type="date"
                    class="period-start"
                    value="{escape(period.get("start_date"))}"
                    required
                >
            </label>

            <label>
                <span>Окончание</span>
                <input
                    type="date"
                    class="period-end"
                    value="{escape(period.get("end_date"))}"
                    required
                >
            </label>

            <button
                type="button"
                class="button button-danger button-icon"
                title="Удалить период"
                aria-label="Удалить период"
                onclick="removePeriodRow(this)"
            >
                ×
            </button>
        </div>
        """

    if not editor_rows:
        editor_rows = """
        <div class="period-edit-row">
            <label>
                <span>Начало</span>
                <input type="date" class="period-start" required>
            </label>
            <label>
                <span>Окончание</span>
                <input type="date" class="period-end" required>
            </label>
            <button
                type="button"
                class="button button-danger button-icon"
                title="Удалить период"
                aria-label="Удалить период"
                onclick="removePeriodRow(this)"
            >
                ×
            </button>
        </div>
        """

    return f"""
    {period_overview}
    <div
        id="period-editor-{application_id}"
        class="period-editor hidden"
        data-application-id="{application_id}"
    >
        <div class="period-rows">
            {editor_rows}
        </div>

        <div class="editor-actions">
            <button
                type="button"
                class="button button-secondary"
                onclick="addPeriodRow({application_id})"
            >
                Добавить
            </button>

            <button
                type="button"
                class="button button-primary"
                onclick="savePeriods({application_id})"
            >
                    Сохранить
                </button>
                <button
                    type="button"
                    class="button button-secondary"
                    onclick="hideEditor('period-editor-{application_id}')"
                >
                    Отмена
                </button>
            </div>

        <div
            id="period-message-{application_id}"
            class="form-message"
        ></div>
    </div>
    """


def photo_reports_html(
    application_id: int,
    reports: list[dict[str, Any]],
    today: datetime.date,
) -> str:
    if not reports:
        return (
            '<div class="muted">'
            "Фотоотчётов нет. Сохраните периоды, чтобы создать месяцы."
            "</div>"
        )

    result = ""

    for report in reports:
        month = MONTHS.get(
            int(report.get("report_month") or 0),
            str(report.get("report_month") or ""),
        )
        year = report.get("report_year") or ""
        report_id = int(report["id"])
        label = f"{month} {year}"
        safe_url = escape(report.get("url") or "")
        comment = escape(report.get("comment") or "")
        is_not_required = bool(report.get("not_required"))
        is_overdue = (
            not is_not_required
            and not report.get("url")
            and photo_report_is_overdue(report, today)
        )

        if report.get("url") and not is_not_required:
            period_title = (
                f'<a class="photo-period photo-complete" '
                f'href="{safe_url}" target="_blank" '
                f'rel="noopener noreferrer">{label}</a>'
            )
        elif is_not_required:
            period_title = (
                f'<span class="photo-period photo-not-required">'
                f"{label}</span>"
            )
        elif is_overdue:
            period_title = (
                f'<span class="photo-period photo-overdue photo-blink">'
                f"{label}</span>"
            )
        else:
            period_title = (
                f'<span class="photo-period">{label}</span>'
            )

        if IS_EDIT_MODE:
            checked = "checked" if is_not_required else ""
            disabled = "disabled" if is_not_required else ""

            result += f"""
            <div
                class="photo-record"
                id="photo-report-{report_id}"
            >
                <div class="photo-record-view">
                    {period_title}
                    <button
                        type="button"
                        class="icon-action"
                        title="Изменить фотоотчёт"
                        aria-label="Изменить фотоотчёт"
                        onclick="showEditor(
                            'photo-editor-{report_id}'
                        )"
                    >✎</button>
                </div>

                <div
                    id="photo-editor-{report_id}"
                    class="photo-editor hidden"
                >
                    <label class="photo-url-field">
                        <span>Ссылка</span>
                        <input
                            type="url"
                            class="photo-url"
                            value="{safe_url}"
                            placeholder="https://..."
                            {disabled}
                        >
                    </label>

                    <input
                        type="hidden"
                        class="photo-comment"
                        value="{comment}"
                    >

                    <label class="checkbox-label photo-checkbox">
                        <input
                            type="checkbox"
                            class="photo-not-required"
                            {checked}
                            onchange="togglePhotoUrl({report_id})"
                        >
                        <span>ФО не нужен</span>
                    </label>

                    <div class="photo-actions">
                        <button
                            type="button"
                            class="button button-primary button-small"
                            title="Сохранить фотоотчёт"
                            onclick="savePhotoReport(
                                {application_id},
                                {report_id}
                            )"
                        >
                            Сохранить
                        </button>

                        <button
                            type="button"
                            class="button button-secondary button-small"
                            onclick="hideEditor(
                                'photo-editor-{report_id}'
                            )"
                        >
                            Отмена
                        </button>

                        <button
                            type="button"
                            class="button button-danger button-small"
                            title="Удалить строку фотоотчёта"
                            onclick="deletePhotoReport(
                                {application_id},
                                {report_id},
                                '{label}'
                            )"
                        >
                            Удалить
                        </button>
                    </div>

                    <div
                        id="photo-message-{report_id}"
                        class="form-message"
                    ></div>
                </div>
            </div>
            """
            continue

        result += f"""
        <div class="photo-row">
            {period_title}
        </div>
        """

    return result


# --------------------------------------------------
# Страница
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(
    entity: str | None = Query(default=None),
    month: str | None = Query(default=None),
    status: str | None = Query(default=None),
    photo_state: str | None = Query(default=None),
) -> HTMLResponse:
    today = datetime.date.today()
    rows = load_dashboard_data()
    current_filters = {
        "entity": entity,
        "month": month,
        "status": status,
        "photo_state": photo_state,
    }

    prepared_rows: list[dict[str, Any]] = []
    all_entities: set[str] = set()

    for item in rows:
        application = item["application"]
        app_invoices = item["invoices"]

        app_status, status_class = get_application_status(
            application,
            app_invoices,
            today,
        )

        item["status"] = app_status
        item["status_class"] = status_class
        item["period_label"] = period_label(item["periods"])

        all_entities.add(item["entity_name"])

        if entity and item["entity_name"] != entity:
            continue

        if status and app_status != status:
            continue

        if month or photo_state:
            matching_reports = [
                report
                for report in item["photo_reports"]
                if (
                    not month
                    or MONTHS.get(
                        int(report.get("report_month") or 0),
                        "",
                    ) == month
                )
                and report_matches_state(report, photo_state)
            ]

            if not matching_reports:
                continue

        prepared_rows.append(item)

    entity_buttons = "".join(
        f"""
        <a class="filter-chip {'active' if entity == entity_name else ''}"
           href="{escape(build_filter_url(current_filters, entity=entity_name))}">
            {escape(entity_name)}
        </a>
        """
        for entity_name in sorted(all_entities)
    )

    month_buttons = "".join(
        f"""
        <a class="filter-chip {'active' if month == month_name else ''}"
           href="{escape(build_filter_url(current_filters, month=month_name))}">
            {month_name}
        </a>
        """
        for month_name in MONTH_ORDER
    )

    statuses = ["—", "ОТПРАВЛЕНО", "ПОДПИСАНО", "ОПЛАЧЕНО", "ПРОСРОЧКА"]

    status_buttons = "".join(
        f"""
        <a class="filter-chip {'active' if status == status_name else ''}"
           href="{escape(build_filter_url(current_filters, status=status_name))}">
            {escape(status_name)}
        </a>
        """
        for status_name in statuses
    )

    photo_states = [
        ("present", "ЕСТЬ"),
        ("missing", "НЕТ"),
        ("not_required", "НЕ ТРЕБУЕТСЯ"),
    ]

    photo_state_buttons = "".join(
        f"""
        <a class="filter-chip {'active' if photo_state == value else ''}"
           href="{escape(build_filter_url(current_filters, photo_state=value))}">
            {label}
        </a>
        """
        for value, label in photo_states
    )

    rows_html = ""

    for item in prepared_rows:
        application = item["application"]
        invoices = item["invoices"]

        invoice_total = sum(
            (money(row.get("amount")) for row in invoices),
            Decimal("0"),
        )
        invoice_numbers = ", ".join(
            str(invoice.get("invoice_no") or "")
            for invoice in invoices
        ) or "—"

        application_block = application_card_html(
            {**application, "_periods": item["periods"]},
            item["legal_entities"],
            item["entity_name"],
        )
        finance_block = finance_table_html(
            application,
            invoices,
            item["upds_by_invoice"],
            today,
        )
        photo_reports_block = photo_reports_html(
            int(application["id"]),
            item["photo_reports"],
            today,
        )

        rows_html += f"""
        <tbody class="application-row">
            <tr class="summary-row"
                onclick="toggleDetails({application['id']})">
                <td class="center">
                    <strong>№ {escape(application.get("app_no") or "—")}</strong>
                </td>
                <td class="center small">
                    {escape(invoice_numbers)}
                </td>
                <td>
                    <strong>{escape(application.get("content") or "—")}</strong>
                    <div class="muted uppercase">
                        {escape(item["entity_name"])}
                    </div>
                </td>
                <td class="center">
                    <span class="period-badge">
                        {escape(item["period_label"])}
                    </span>
                </td>
                <td class="center">
                    <span class="status {item['status_class']}">
                        {escape(item["status"])}
                    </span>
                </td>
                <td class="right mono">
                    {format_money(invoice_total)} ₽
                </td>
            </tr>

            <tr id="details-{application['id']}" class="details-row hidden">
                <td colspan="6">
                    <div class="details-grid">
                        <section class="panel panel-application">
                            {application_block}
                        </section>

                        <section class="panel panel-finance">
                            <div class="panel-heading">
                                <h3>Счета</h3>
                            </div>
                            {finance_block}
                        </section>

                        <section class="panel panel-photos">
                            <div class="panel-heading">
                                <h3>Фотоотчёты</h3>
                            </div>
                            <div class="photo-list">
                                {photo_reports_block}
                            </div>
                            <div
                                id="photo-panel-message-{application['id']}"
                                class="form-message"
                            ></div>
                        </section>
                    </div>
                </td>
            </tr>
        </tbody>
        """

    if not rows_html:
        rows_html = """
        <tbody>
            <tr>
                <td colspan="6" class="empty-state">
                    По выбранным фильтрам ничего не найдено
                </td>
            </tr>
        </tbody>
        """

    mode_badge_class = "mode-edit" if IS_EDIT_MODE else "mode-read"
    mode_badge_text = (
        "режим редактирования"
        if IS_EDIT_MODE
        else "только просмотр"
    )

    html_page = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">

        <title>Billing Control v2</title>

        <style>
            :root {{
                --ink: #1e1b4b;
                --indigo: #4f46e5;
                --indigo-soft: #eef2ff;
                --slate-50: #f8fafc;
                --slate-100: #f1f5f9;
                --slate-200: #e2e8f0;
                --slate-400: #94a3b8;
                --slate-500: #64748b;
                --slate-700: #334155;
                --emerald: #10b981;
                --amber: #f59e0b;
                --red: #ef4444;
                --white: #ffffff;
            }}

            * {{
                box-sizing: border-box;
            }}

            body {{
                margin: 0;
                padding: 24px;
                background: #fcfdff;
                color: var(--slate-700);
                font-family:
                    Inter,
                    ui-sans-serif,
                    system-ui,
                    -apple-system,
                    BlinkMacSystemFont,
                    "Segoe UI",
                    sans-serif;
            }}

            a {{
                color: inherit;
            }}

            .container {{
                max-width: 1900px;
                margin: 0 auto;
            }}

            .header {{
                display: flex;
                justify-content: space-between;
                align-items: end;
                gap: 20px;
                margin-bottom: 30px;
            }}

            h1 {{
                margin: 0;
                color: var(--ink);
                font-size: 38px;
                font-weight: 900;
                letter-spacing: -1.5px;
                text-transform: uppercase;
                font-style: italic;
            }}

            h1 span {{
                color: var(--indigo);
            }}

            .mode-badge {{
                padding: 10px 16px;
                border: 1px solid var(--slate-200);
                border-radius: 14px;
                background: var(--white);
                font-size: 11px;
                font-weight: 800;
                text-transform: uppercase;
            }}

            .mode-read {{
                color: var(--slate-500);
            }}

            .mode-edit {{
                border-color: #bbf7d0;
                color: #15803d;
                background: #f0fdf4;
            }}

            .filters {{
                margin-bottom: 30px;
                padding: 28px;
                border: 1px solid #e0e7ff;
                border-radius: 30px;
                background: var(--indigo-soft);
                display: grid;
                grid-template-columns: 1fr 1.4fr 1.1fr 1.1fr;
                gap: 24px;
            }}

            .filter-title {{
                margin-bottom: 12px;
                color: #818cf8;
                font-size: 10px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .filter-list {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
            }}

            .filter-chip {{
                padding: 9px 12px;
                border: 1px solid var(--slate-200);
                border-radius: 11px;
                background: var(--white);
                color: var(--slate-500);
                font-size: 10px;
                font-weight: 800;
                text-decoration: none;
            }}

            .filter-chip.active {{
                border-color: var(--indigo);
                background: var(--indigo);
                color: var(--white);
            }}

            .reset-link {{
                display: inline-block;
                margin-top: 12px;
                color: var(--indigo);
                font-size: 10px;
                font-weight: 800;
            }}

            .table-shell {{
                overflow: hidden;
                border: 1px solid var(--slate-100);
                border-radius: 30px;
                background: var(--white);
                box-shadow: 0 8px 30px rgba(15, 23, 42, 0.04);
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
            }}

            th {{
                padding: 20px 18px;
                border-bottom: 1px solid var(--slate-100);
                background: var(--slate-50);
                color: var(--slate-400);
                font-size: 10px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            td {{
                padding: 21px 18px;
                border-bottom: 1px solid var(--slate-100);
                vertical-align: middle;
                font-size: 12px;
            }}

            .summary-row {{
                cursor: pointer;
            }}

            .summary-row:hover {{
                background: #f8faff;
            }}

            .center {{
                text-align: center;
            }}

            .right {{
                text-align: right;
            }}

            .small {{
                max-width: 230px;
                color: var(--slate-500);
                line-height: 1.45;
            }}

            .mono {{
                font-family:
                    "SFMono-Regular",
                    Consolas,
                    "Liberation Mono",
                    monospace;
            }}

            .muted {{
                color: var(--slate-400);
                font-size: 10px;
            }}

            .uppercase {{
                margin-top: 4px;
                text-transform: uppercase;
                font-weight: 800;
            }}

            .period-badge {{
                display: inline-block;
                padding: 7px 10px;
                border: 1px solid var(--slate-200);
                border-radius: 10px;
                background: var(--white);
                color: var(--slate-500);
                font-size: 10px;
                font-weight: 900;
            }}

            .status {{
                font-size: 10px;
                font-weight: 900;
            }}

            .status-paid,
            .success {{
                color: var(--emerald);
            }}

            .status-overdue,
            .danger {{
                color: var(--red);
            }}

            .status-signed {{
                color: var(--amber);
            }}

            .status-sent {{
                color: var(--indigo);
            }}

            .status-empty {{
                color: var(--slate-400);
            }}

            .hidden {{
                display: none;
            }}

            .details-row td {{
                padding: 12px;
                background: var(--slate-50);
            }}

            .details-grid {{
                display: grid;
                grid-template-columns:
                    minmax(300px, 0.95fr)
                    minmax(700px, 2.55fr)
                    minmax(210px, 0.55fr);
                grid-template-areas:
                    "application finance photos";
                gap: 10px;
                align-items: start;
            }}

            .panel-application {{
                grid-area: application;
            }}

            .panel-finance {{
                grid-area: finance;
            }}

            .panel-photos {{
                grid-area: photos;
            }}

            .panel {{
                min-width: 0;
                padding: 12px;
                border: 1px solid var(--slate-200);
                border-radius: 14px;
                background: var(--white);
            }}

            .panel h3 {{
                margin: 0 0 10px;
                color: var(--indigo);
                font-size: 11px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .application-info {{
                display: grid;
                gap: 0;
            }}

            .panel-subsection {{
                margin-top: 12px;
                padding-top: 11px;
                border-top: 1px solid var(--slate-200);
            }}

            .panel-subsection h3 {{
                margin-bottom: 8px;
            }}

            label {{
                display: grid;
                gap: 6px;
                color: var(--slate-500);
                font-size: 9px;
                font-weight: 800;
                text-transform: uppercase;
            }}

            input,
            textarea {{
                width: 100%;
                min-width: 0;
                padding: 7px 8px;
                border: 1px solid var(--slate-200);
                border-radius: 9px;
                background: var(--white);
                color: var(--slate-700);
                font: inherit;
                font-size: 11px;
                text-transform: none;
            }}

            input:focus,
            textarea:focus {{
                outline: 2px solid #c7d2fe;
                border-color: var(--indigo);
            }}

            input:disabled {{
                background: var(--slate-100);
                color: var(--slate-400);
            }}

            textarea {{
                resize: vertical;
            }}

            .button {{
                padding: 9px 12px;
                border: 1px solid transparent;
                border-radius: 10px;
                cursor: pointer;
                font-size: 9px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .button:disabled {{
                cursor: wait;
                opacity: 0.6;
            }}

            .button-primary {{
                background: var(--indigo);
                color: var(--white);
            }}

            .button-secondary {{
                border-color: var(--slate-200);
                background: var(--white);
                color: var(--slate-700);
            }}

            .button-danger {{
                border-color: #fecaca;
                background: #fff;
                color: #dc2626;
            }}

            .button-small {{
                padding: 7px 8px;
            }}

            .button-icon {{
                width: 30px;
                height: 30px;
                padding: 0;
                font-size: 16px;
                line-height: 1;
            }}

            .editor-actions {{
                display: flex;
                flex-wrap: wrap;
                gap: 8px;
                margin-top: 8px;
            }}

            .period-editor > .editor-actions {{
                display: grid;
                grid-template-columns: 1fr 1fr;
            }}

            .form-message {{
                min-height: 0;
                margin-top: 5px;
                color: var(--slate-500);
                font-size: 10px;
            }}

            .form-message.error {{
                color: var(--red);
            }}

            .form-message.success {{
                color: var(--emerald);
            }}

            .info-row {{
                display: flex;
                justify-content: space-between;
                gap: 10px;
                padding: 5px 0;
                border-bottom: 1px solid var(--slate-100);
                font-size: 10px;
            }}

            .info-row span {{
                color: var(--slate-400);
            }}

            .info-row strong {{
                text-align: right;
            }}

            .application-date-value {{
                display: flex;
                gap: 6px;
                align-items: center;
                justify-content: flex-end;
            }}

            .application-date-editor {{
                padding: 7px 0;
                border-bottom: 1px solid var(--slate-100);
            }}

            .application-date-editor-fields {{
                display: grid;
                grid-template-columns: minmax(130px, 1fr) auto;
                gap: 6px;
                align-items: end;
            }}

            .application-date-editor-actions {{
                display: flex;
                gap: 5px;
            }}

            .application-date-message {{
                margin-top: 0;
            }}

            .mini-status {{
                padding: 3px 6px;
                border-radius: 6px;
                font-size: 8px;
                font-weight: 900;
                white-space: nowrap;
            }}

            .mini-paid {{
                color: #047857;
                background: #ecfdf5;
            }}

            .mini-overdue {{
                color: #b91c1c;
                background: #fef2f2;
            }}

            .mini-waiting {{
                color: var(--slate-500);
                background: var(--slate-100);
            }}

            .mini-ready {{
                color: var(--indigo);
                background: var(--indigo-soft);
            }}

            .mini-empty {{
                color: var(--slate-400);
                background: var(--slate-50);
            }}

            .label {{
                color: var(--slate-400);
                font-size: 9px;
                text-transform: uppercase;
                font-weight: 800;
            }}

            .finance-table {{
                display: grid;
                gap: 0;
                min-width: 0;
                border: 1px solid var(--slate-200);
                border-radius: 10px;
                overflow: hidden;
            }}

            .finance-header,
            .finance-row {{
                display: grid;
                grid-template-columns:
                    165px
                    110px
                    180px
                    minmax(220px, 1fr)
                    82px;
                gap: 10px;
                align-items: start;
            }}

            .finance-header {{
                padding: 7px 8px;
                background: var(--slate-50);
                color: var(--slate-400);
                font-size: 8px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .finance-header-number,
            .finance-header-date {{
                text-align: right;
            }}

            .finance-header-date-edit {{
                padding-right: 31px;
            }}

            .finance-entry {{
                border-top: 1px solid var(--slate-200);
            }}

            .finance-row {{
                padding: 8px;
                font-size: 9px;
                line-height: 1.35;
            }}

            .finance-invoice {{
                min-width: 0;
            }}

            .invoice-title-line {{
                display: flex;
                flex-wrap: wrap;
                gap: 5px;
                align-items: center;
            }}

            .invoice-content {{
                display: block;
                margin-top: 3px;
                overflow: hidden;
                color: var(--slate-500);
                font-size: 9px;
                line-height: 1.3;
                text-overflow: ellipsis;
            }}

            .finance-number,
            .finance-date {{
                padding-top: 2px;
                font-size: 9px;
                font-weight: 700;
                line-height: 1.35;
                text-align: right;
                white-space: nowrap;
            }}

            .finance-date-cell {{
                display: grid;
                grid-template-columns: minmax(0, 1fr) auto;
                gap: 6px;
                align-items: start;
            }}

            .finance-payment-stack {{
                display: grid;
                gap: 5px;
                min-width: 0;
            }}

            .payment-line {{
                min-width: 0;
            }}

            .payment-line + .payment-line {{
                padding-top: 5px;
                border-top: 1px dashed var(--slate-200);
            }}

            .payment-line-view {{
                display: grid;
                grid-template-columns:
                    minmax(72px, auto)
                    minmax(72px, auto)
                    auto;
                gap: 7px;
                align-items: center;
                color: var(--slate-500);
                font-size: 9px;
                line-height: 1.35;
            }}

            .payment-line-view strong {{
                color: var(--slate-700);
                white-space: nowrap;
            }}

            .inline-actions {{
                display: flex;
                gap: 3px;
                justify-content: flex-end;
            }}

            .icon-action {{
                display: inline-grid;
                width: 21px;
                height: 21px;
                padding: 0;
                border: 1px solid var(--slate-200);
                border-radius: 6px;
                place-items: center;
                background: var(--white);
                color: var(--indigo);
                cursor: pointer;
                font-size: 12px;
                font-weight: 900;
                line-height: 1;
            }}

            .icon-action-danger {{
                border-color: #fecaca;
                color: var(--red);
            }}

            .add-inline-button {{
                width: fit-content;
                padding: 0;
                border: 0;
                background: transparent;
                color: var(--indigo);
                cursor: pointer;
                font-size: 8px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .finance-add-row {{
                padding: 8px;
                border-top: 1px solid var(--slate-200);
            }}

            .finance-empty {{
                padding: 12px;
            }}

            .finance-editor {{
                padding: 9px;
                border-top: 1px solid var(--slate-200);
                background: var(--slate-50);
            }}

            .finance-editor-fields {{
                display: grid;
                gap: 6px;
            }}

            .finance-editor-fields > label {{
                min-width: 0;
            }}

            .invoice-editor-fields {{
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }}

            .invoice-editor-fields input[type="date"] {{
                width: 100%;
                min-width: 0;
            }}

            .payment-editor {{
                padding: 10px;
                border-top: 1px solid var(--slate-200);
                background: #f8fafc;
            }}

            .payment-editor-fields {{
                grid-template-columns:
                    minmax(180px, 0.7fr)
                    minmax(220px, 0.8fr);
            }}

            .finance-payment-editors {{
                min-width: 0;
            }}

            .finance-editor-actions {{
                display: flex;
                gap: 5px;
                margin-top: 6px;
            }}

            .upd-list {{
                display: grid;
                gap: 6px;
            }}

            .upd-record {{
                overflow: hidden;
                border: 1px solid var(--slate-200);
                border-radius: 10px;
            }}

            .upd-record-view {{
                display: grid;
                grid-template-columns:
                    minmax(110px, 1fr)
                    minmax(100px, 1fr)
                    minmax(90px, 0.8fr)
                    minmax(100px, 0.9fr)
                    minmax(100px, 0.9fr)
                    minmax(100px, 0.9fr)
                    auto;
                gap: 9px;
                align-items: center;
                padding: 8px;
                background: var(--white);
            }}

            .upd-value {{
                display: grid;
                gap: 3px;
                min-width: 0;
                font-size: 9px;
            }}

            .upd-value span {{
                color: var(--slate-400);
                font-size: 8px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .upd-value strong {{
                overflow: hidden;
                color: var(--slate-700);
                text-overflow: ellipsis;
                white-space: nowrap;
            }}

            .upd-amount-value {{
                text-align: right;
            }}

            .upd-state {{
                display: flex;
                gap: 5px;
                align-items: center;
                justify-content: flex-end;
            }}

            .upd-editor {{
                padding: 9px;
                border-top: 1px solid var(--slate-200);
                background: var(--slate-50);
            }}

            .upd-editor-fields {{
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 6px;
            }}

            .upd-editor-fields > label,
            .upd-editor-fields input[type="date"] {{
                min-width: 0;
            }}

            .photo-list {{
                display: grid;
                gap: 6px;
            }}

            .period-editor,
            .period-rows {{
                display: grid;
                gap: 6px;
            }}

            .period-edit-row {{
                display: grid;
                grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 30px;
                gap: 5px;
                align-items: end;
                padding-bottom: 6px;
                border-bottom: 1px solid var(--slate-100);
            }}

            .photo-row,
            .photo-record {{
                padding: 7px 0;
                border-bottom: 1px solid var(--slate-100);
            }}

            .photo-row,
            .photo-record-view {{
                display: grid;
                grid-template-columns: auto minmax(0, 1fr) auto;
                gap: 7px;
                align-items: center;
                min-height: 25px;
                font-size: 10px;
            }}

            .photo-period {{
                font-weight: 900;
                text-decoration: none;
                white-space: nowrap;
            }}

            .photo-complete {{
                color: var(--emerald);
            }}

            .photo-not-required {{
                color: var(--slate-400);
            }}

            .photo-pending {{
                color: var(--indigo);
            }}

            .photo-overdue {{
                color: var(--red);
            }}

            .photo-blink {{
                animation: photo-alert 1.1s ease-in-out infinite;
            }}

            @keyframes photo-alert {{
                0%,
                100% {{
                    opacity: 1;
                }}
                50% {{
                    opacity: 0.28;
                }}
            }}

            @media (prefers-reduced-motion: reduce) {{
                .photo-blink {{
                    animation: none;
                    text-decoration: underline;
                    text-decoration-thickness: 2px;
                }}
            }}

            .photo-comment-view {{
                min-width: 0;
                overflow: hidden;
                color: var(--slate-500);
                font-size: 8px;
                line-height: 1.3;
                text-overflow: ellipsis;
                white-space: nowrap;
            }}

            .photo-editor:not(.hidden) {{
                display: grid;
                grid-template-columns: minmax(0, 1fr) auto;
                grid-template-areas:
                    "url checkbox"
                    "comment comment"
                    "actions actions"
                    "message message";
                gap: 6px;
                align-items: end;
                margin-top: 6px;
                padding: 7px;
                border: 1px solid var(--slate-200);
                border-radius: 9px;
                background: var(--slate-50);
            }}

            .photo-url-field {{
                grid-area: url;
            }}

            .photo-comment-field {{
                grid-area: comment;
            }}

            .photo-checkbox {{
                grid-area: checkbox;
                align-self: center;
            }}

            .photo-actions {{
                grid-area: actions;
                display: flex;
                flex-wrap: wrap;
                gap: 5px;
            }}

            .photo-actions .button {{
                min-width: 62px;
                padding: 5px 6px;
            }}

            .photo-editor .form-message {{
                grid-area: message;
            }}

            .checkbox-label {{
                display: flex;
                gap: 8px;
                align-items: center;
                cursor: pointer;
                text-transform: none;
            }}

            .checkbox-label input {{
                width: auto;
            }}

            .empty-state {{
                padding: 70px;
                text-align: center;
                color: var(--slate-400);
            }}

            @media (max-width: 1279px) {{
                .details-grid {{
                    grid-template-columns:
                        minmax(300px, 0.9fr)
                        minmax(620px, 1.9fr);
                    grid-template-areas:
                        "application finance"
                        "photos photos";
                }}

            }}

            @media (max-width: 1100px) {{
                body {{
                    padding: 14px;
                }}

                .filters {{
                    grid-template-columns: 1fr;
                }}

                .table-shell {{
                    overflow-x: auto;
                }}

                table {{
                    min-width: 1000px;
                }}

                .payment-editor-fields {{
                    grid-template-columns: 1fr;
                }}

                .upd-record-view {{
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                }}

                .upd-state {{
                    justify-content: flex-start;
                }}

                .upd-editor-fields {{
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }}
            }}

            /* Этап 5: карточка приложения как рабочий экран CRM. */
            .details-row > td {{
                padding: 12px;
                background: #f7f8fc;
            }}

            .details-grid {{
                display: grid;
                grid-template-columns: minmax(0, 1fr);
                grid-template-areas: none;
                gap: 12px;
                min-width: 0;
            }}

            .panel,
            .panel-application,
            .panel-finance,
            .panel-photos {{
                grid-area: auto;
                min-width: 0;
                padding: 16px;
                border: 1px solid var(--slate-200);
                border-radius: 16px;
                background: var(--white);
                box-shadow: none;
            }}

            .panel-heading {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 12px;
            }}

            .panel-heading h3 {{
                margin: 0;
            }}

            .application-overview {{
                display: grid;
                grid-template-columns:
                    minmax(90px, 0.65fr)
                    minmax(150px, 1fr)
                    minmax(240px, 2fr)
                    minmax(220px, 1.5fr)
                    minmax(140px, 0.9fr)
                    minmax(120px, 0.8fr)
                    minmax(120px, 0.8fr);
                gap: 10px;
                align-items: stretch;
            }}

            .overview-value {{
                display: grid;
                align-content: center;
                gap: 5px;
                min-width: 0;
                padding: 10px 12px;
                border-radius: 10px;
                background: var(--slate-50);
            }}

            .overview-value span {{
                color: var(--slate-400);
                font-size: 8px;
                font-weight: 900;
                letter-spacing: 0.03em;
                text-transform: uppercase;
            }}

            .overview-value strong {{
                min-width: 0;
                overflow: hidden;
                color: var(--slate-700);
                font-size: 11px;
                line-height: 1.35;
                text-overflow: ellipsis;
            }}

            .overview-money strong {{
                white-space: nowrap;
            }}

            .overview-period-heading {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 6px;
            }}

            .overview-period-heading .icon-action {{
                flex: 0 0 auto;
            }}

            .overview-period .period-view {{
                white-space: normal;
            }}

            .application-overview > .overview-value {{
                order: 1;
            }}

            .application-overview > .period-editor {{
                grid-column: 1 / -1;
                order: 2;
            }}

            .period-editor.hidden {{
                display: none;
            }}

            .application-card-editor {{
                margin-top: 12px;
                padding: 12px;
                border: 1px solid #c7d2fe;
                border-radius: 12px;
                background: var(--indigo-soft);
            }}

            .application-editor-grid {{
                display: grid;
                grid-template-columns:
                    minmax(100px, 0.7fr)
                    minmax(160px, 1fr)
                    minmax(260px, 2fr)
                    minmax(140px, 0.9fr)
                    minmax(150px, 0.9fr)
                    minmax(150px, 0.9fr);
                gap: 8px;
                align-items: end;
            }}

            .application-editor-grid > label {{
                min-width: 0;
            }}

            .workflow-table {{
                overflow: visible;
                border-radius: 12px;
            }}

            .workflow-header,
            .workflow-row {{
                display: grid;
                grid-template-columns:
                    minmax(140px, 1.2fr)
                    minmax(100px, 0.8fr)
                    minmax(125px, 0.9fr)
                    minmax(140px, 1fr)
                    minmax(125px, 0.9fr)
                    minmax(170px, 1.2fr)
                    52px;
                gap: 10px;
                align-items: center;
            }}

            .workflow-header {{
                padding: 8px 10px;
                color: var(--slate-400);
                font-size: 8px;
                font-weight: 900;
                text-transform: uppercase;
            }}

            .workflow-entry {{
                border-top: 1px solid var(--slate-200);
            }}

            .workflow-row {{
                padding: 10px;
                font-size: 10px;
            }}

            .workflow-invoice-no,
            .workflow-amount,
            .workflow-date {{
                min-width: 0;
                color: var(--slate-700);
                font-size: 10px;
                white-space: nowrap;
            }}

            .workflow-amount {{
                font-variant-numeric: tabular-nums;
            }}

            .workflow-payment,
            .workflow-upd {{
                display: flex;
                flex-wrap: wrap;
                gap: 5px;
                align-items: center;
                min-width: 0;
            }}

            .workflow-actions {{
                display: flex;
                gap: 4px;
                justify-content: flex-end;
            }}

            .workflow-status {{
                display: inline-flex;
                gap: 4px;
                align-items: center;
                width: fit-content;
                padding: 5px 8px;
                border-radius: 999px;
                font-size: 8px;
                font-weight: 900;
                line-height: 1;
                white-space: nowrap;
            }}

            .workflow-paid,
            .workflow-upd-signed {{
                background: #dcfce7;
                color: #15803d;
            }}

            .workflow-overdue {{
                background: #fee2e2;
                color: #b91c1c;
            }}

            .workflow-waiting,
            .workflow-upd-waiting {{
                background: var(--slate-100);
                color: var(--slate-500);
            }}

            .workflow-upd-formed {{
                background: #fef3c7;
                color: #a16207;
            }}

            .workflow-upd-sent {{
                background: #dbeafe;
                color: #1d4ed8;
            }}

            .upd-reminder {{
                color: #dc2626;
                font-size: 8px;
                font-weight: 900;
                white-space: nowrap;
            }}

            .invoice-editor {{
                padding: 12px;
                border-top: 1px solid #c7d2fe;
                background: var(--indigo-soft);
            }}

            .invoice-editor-fields {{
                grid-template-columns:
                    minmax(150px, 1.2fr)
                    minmax(110px, 0.8fr)
                    minmax(150px, 0.9fr)
                    minmax(150px, 0.9fr)
                    minmax(110px, 0.7fr)
                    minmax(150px, 0.9fr);
                align-items: end;
            }}

            .invoice-paid-field {{
                min-height: 35px;
                padding: 8px 10px;
                border: 1px solid var(--slate-200);
                border-radius: 8px;
                background: var(--white);
            }}

            .invoice-paid-field span {{
                color: var(--slate-700);
            }}

            .period-editor {{
                margin-top: 10px;
                padding: 10px;
                border: 1px solid #c7d2fe;
                border-radius: 10px;
                background: var(--indigo-soft);
            }}

            .photo-list {{
                display: grid;
                grid-template-columns: repeat(
                    auto-fit,
                    minmax(150px, 1fr)
                );
                gap: 6px 12px;
            }}

            .photo-row,
            .photo-record {{
                min-width: 0;
                padding: 6px 8px;
                border: 1px solid var(--slate-100);
                border-radius: 9px;
                background: var(--slate-50);
            }}

            .photo-row,
            .photo-record-view {{
                display: grid;
                grid-template-columns: minmax(0, 1fr) auto;
                gap: 7px;
                align-items: center;
            }}

            .photo-period {{
                color: var(--slate-700);
                font-weight: 700;
            }}

            .photo-complete {{
                color: var(--emerald);
                font-weight: 900;
            }}

            .photo-not-required {{
                color: var(--slate-400);
            }}

            .photo-overdue {{
                color: var(--red);
                font-weight: 900;
            }}

            .photo-editor:not(.hidden) {{
                grid-template-areas:
                    "url checkbox"
                    "actions actions"
                    "message message";
            }}

            @media (max-width: 1279px) {{
                .details-grid {{
                    grid-template-columns: minmax(0, 1fr);
                    grid-template-areas: none;
                }}

                .application-overview,
                .application-editor-grid {{
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                }}

                .overview-content,
                .application-content-field {{
                    grid-column: span 2;
                }}
            }}

            @media (max-width: 1100px) {{
                .workflow-table {{
                    overflow-x: auto;
                }}

                .workflow-header,
                .workflow-row {{
                    min-width: 970px;
                }}

                .invoice-editor-fields {{
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                }}
            }}
        </style>
    </head>

    <body>
        <div class="container">
            <div class="header">
                <h1>Billing <span>Control</span></h1>
                <div class="mode-badge {mode_badge_class}">
                    v2 · Supabase · {mode_badge_text}
                </div>
            </div>

            <div class="filters">
                <div>
                    <div class="filter-title">Юридическое лицо</div>
                    <div class="filter-list">
                        {entity_buttons}
                    </div>
                </div>

                <div>
                    <div class="filter-title">Месяц фотоотчёта</div>
                    <div class="filter-list">
                        {month_buttons}
                    </div>
                </div>

                <div>
                    <div class="filter-title">Статус</div>
                    <div class="filter-list">
                        {status_buttons}
                    </div>
                </div>

                <div>
                    <div class="filter-title">Фотоотчёт</div>
                    <div class="filter-list">
                        {photo_state_buttons}
                    </div>

                    <a class="reset-link" href="/">
                        Сбросить фильтры
                    </a>
                </div>
            </div>

            <div class="table-shell">
                <table>
                    <thead>
                        <tr>
                            <th>APP</th>
                            <th>INV</th>
                            <th>Услуга</th>
                            <th>Период</th>
                            <th>Статус</th>
                            <th class="right">Сумма счетов</th>
                        </tr>
                    </thead>

                    {rows_html}
                </table>
            </div>
        </div>

        <script>
            const RELOAD_STATE_KEY = "billing-control-reload-state";

            function toggleDetails(id) {{
                const row = document.getElementById("details-" + id);

                if (row) {{
                    row.classList.toggle("hidden");
                }}
            }}

            function setFormMessage(id, text, kind) {{
                const element = document.getElementById(id);

                if (!element) {{
                    return;
                }}

                element.textContent = text || "";
                element.className = "form-message" + (
                    kind ? " " + kind : ""
                );
            }}

            function reloadWithOpenApplication(
                applicationId,
                messageId,
                messageText
            ) {{
                window.sessionStorage.setItem(
                    RELOAD_STATE_KEY,
                    JSON.stringify({{
                        applicationId: applicationId,
                        messageId: messageId || "",
                        messageText: messageText || ""
                    }})
                );
                window.location.reload();
            }}

            function restoreAfterReload() {{
                const rawState = window.sessionStorage.getItem(
                    RELOAD_STATE_KEY
                );

                if (!rawState) {{
                    return;
                }}

                window.sessionStorage.removeItem(RELOAD_STATE_KEY);

                let state;

                try {{
                    state = JSON.parse(rawState);
                }} catch (error) {{
                    return;
                }}

                const row = document.getElementById(
                    "details-" + state.applicationId
                );

                if (!row) {{
                    return;
                }}

                row.classList.remove("hidden");

                if (state.messageId && state.messageText) {{
                    setFormMessage(
                        state.messageId,
                        state.messageText,
                        "success"
                    );
                }}

                const summaryRow = row.previousElementSibling;

                if (summaryRow) {{
                    window.requestAnimationFrame(function() {{
                        summaryRow.scrollIntoView({{
                            block: "start"
                        }});
                    }});
                }}
            }}

            document.addEventListener(
                "DOMContentLoaded",
                restoreAfterReload
            );

            async function requestJson(url, options) {{
                const response = await fetch(url, options);
                let data = {{}};

                try {{
                    data = await response.json();
                }} catch (error) {{
                    data = {{}};
                }}

                if (!response.ok) {{
                    throw new Error(
                        data.detail || "Не удалось выполнить операцию."
                    );
                }}

                return data;
            }}

            function showEditor(id) {{
                const editor = document.getElementById(id);

                if (!editor) {{
                    return;
                }}

                editor.classList.remove("hidden");
                const firstInput = editor.querySelector("input");

                if (firstInput) {{
                    firstInput.focus();
                }}
            }}

            function hideEditor(id) {{
                const editor = document.getElementById(id);

                if (editor) {{
                    editor.classList.add("hidden");
                }}
            }}

            function showInvoiceDueDateEditor(id) {{
                showEditor(id);
                const editor = document.getElementById(id);
                const dueDateInput = editor
                    ? editor.querySelector(".invoice-due-date")
                    : null;

                if (dueDateInput) {{
                    dueDateInput.focus();
                }}
            }}

            async function saveApplicationSignedAt(applicationId) {{
                const editorId = (
                    "application-signed-at-editor-" + applicationId
                );
                const editor = document.getElementById(editorId);
                const input = editor
                    ? editor.querySelector(".application-signed-at")
                    : null;
                const messageId = (
                    "application-date-message-signed-at-" + applicationId
                );

                if (!input) {{
                    setFormMessage(
                        messageId,
                        "Поле даты подписания не найдено.",
                        "error"
                    );
                    return;
                }}

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(
                        "/applications/" + applicationId + "/signed-at",
                        {{
                            method: "PUT",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify({{
                                signed_at: input.value
                            }})
                        }}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        messageId,
                        "Дата подписания сохранена."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            async function saveApplicationSentAt(applicationId) {{
                const editorId = (
                    "application-sent-at-editor-" + applicationId
                );
                const editor = document.getElementById(editorId);
                const input = editor
                    ? editor.querySelector(".application-sent-at")
                    : null;
                const messageId = (
                    "application-date-message-sent-at-" + applicationId
                );

                if (!input) {{
                    setFormMessage(
                        messageId,
                        "Поле даты отправки не найдено.",
                        "error"
                    );
                    return;
                }}

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(
                        "/applications/" + applicationId + "/sent-at",
                        {{
                            method: "PUT",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify({{
                                sent_at: input.value
                            }})
                        }}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        messageId,
                        "Дата отправки сохранена."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function applicationCardPayload(editor) {{
                return {{
                    app_no: editor.querySelector(
                        ".application-number"
                    ).value,
                    legal_entity_id: editor.querySelector(
                        ".application-entity"
                    ).value,
                    content: editor.querySelector(
                        ".application-content"
                    ).value,
                    total_sum: editor.querySelector(
                        ".application-total"
                    ).value,
                    sent_at: editor.querySelector(
                        ".application-sent-at"
                    ).value,
                    signed_at: editor.querySelector(
                        ".application-signed-at"
                    ).value
                }};
            }}

            async function saveApplicationCard(applicationId) {{
                const editor = document.getElementById(
                    "application-editor-" + applicationId
                );
                const messageId = (
                    "application-message-" + applicationId
                );

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(
                        "/applications/" + applicationId,
                        {{
                            method: "PUT",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify(
                                applicationCardPayload(editor)
                            )
                        }}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        messageId,
                        "Приложение сохранено."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function invoicePayload(editor) {{
                return {{
                    invoice_no: editor.querySelector(
                        ".invoice-number"
                    ).value,
                    amount: editor.querySelector(
                        ".invoice-amount"
                    ).value,
                    due_date: editor.querySelector(
                        ".invoice-due-date"
                    ).value,
                    shipment_date: editor.querySelector(
                        ".invoice-shipment-date"
                    ).value,
                    is_paid: editor.querySelector(
                        ".invoice-paid"
                    ).checked,
                    upd_status: editor.querySelector(
                        ".invoice-upd-status"
                    ).value
                }};
            }}

            async function saveInvoice(applicationId, invoiceId) {{
                const formKey = invoiceId === null ? "new" : invoiceId;
                const editorId = (
                    "invoice-form-" + applicationId + "-" + formKey
                );
                const editor = document.getElementById(editorId);
                const messageId = (
                    "invoice-message-" + applicationId + "-" + formKey
                );
                const url = invoiceId === null
                    ? "/applications/" + applicationId + "/invoices"
                    : "/applications/" + applicationId +
                        "/invoices/" + invoiceId;

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(url, {{
                        method: invoiceId === null ? "POST" : "PUT",
                        headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify(invoicePayload(editor))
                    }});
                    reloadWithOpenApplication(
                        applicationId,
                        "finance-message-" + applicationId,
                        invoiceId === null
                            ? "Счёт добавлен."
                            : "Счёт сохранён."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            async function deleteInvoice(applicationId, invoiceId) {{
                const confirmed = window.confirm(
                    "Удалить этот счёт?\\n\\n" +
                    "Связанные внутренние данные также будут удалены."
                );

                if (!confirmed) {{
                    return;
                }}

                try {{
                    await requestJson(
                        "/applications/" + applicationId +
                        "/invoices/" + invoiceId +
                        "?confirmed=true",
                        {{method: "DELETE"}}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        "finance-message-" + applicationId,
                        "Счёт удалён."
                    );
                }} catch (error) {{
                    setFormMessage(
                        "finance-message-" + applicationId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function paymentPayload(editor) {{
                return {{
                    amount: editor.querySelector(
                        ".payment-amount"
                    ).value,
                    payment_date: editor.querySelector(
                        ".payment-date"
                    ).value
                }};
            }}

            async function savePayment(
                applicationId,
                invoiceId,
                paymentId
            ) {{
                const formKey = paymentId === null ? "new" : paymentId;
                const editor = document.getElementById(
                    "payment-form-" + invoiceId + "-" + formKey
                );
                const messageId = (
                    "payment-message-" + invoiceId + "-" + formKey
                );
                const baseUrl = (
                    "/applications/" + applicationId +
                    "/invoices/" + invoiceId + "/payments"
                );
                const url = paymentId === null
                    ? baseUrl
                    : baseUrl + "/" + paymentId;

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(url, {{
                        method: paymentId === null ? "POST" : "PUT",
                        headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify(paymentPayload(editor))
                    }});
                    reloadWithOpenApplication(
                        applicationId,
                        "finance-message-" + applicationId,
                        paymentId === null
                            ? "Платёж добавлен."
                            : "Платёж сохранён."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            async function deletePayment(
                applicationId,
                invoiceId,
                paymentId
            ) {{
                const confirmed = window.confirm(
                    "Удалить этот платёж? Отменить удаление будет нельзя."
                );

                if (!confirmed) {{
                    return;
                }}

                try {{
                    await requestJson(
                        "/applications/" + applicationId +
                        "/invoices/" + invoiceId +
                        "/payments/" + paymentId +
                        "?confirmed=true",
                        {{method: "DELETE"}}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        "finance-message-" + applicationId,
                        "Платёж удалён."
                    );
                }} catch (error) {{
                    setFormMessage(
                        "finance-message-" + applicationId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function updPayload(editor) {{
                return {{
                    upd_no: editor.querySelector(
                        ".upd-number"
                    ).value,
                    shipment_date: editor.querySelector(
                        ".upd-shipment-date"
                    ).value,
                    sent_at: editor.querySelector(
                        ".upd-sent-at"
                    ).value,
                    signed_at: editor.querySelector(
                        ".upd-signed-at"
                    ).value
                }};
            }}

            async function saveUpd(applicationId, invoiceId) {{
                const editor = document.getElementById(
                    "upd-form-" + invoiceId
                );
                const messageId = "upd-message-" + invoiceId;

                setFormMessage(messageId, "Сохраняю...", "");

                try {{
                    await requestJson(
                        "/applications/" + applicationId +
                        "/invoices/" + invoiceId + "/upd",
                        {{
                            method: "PUT",
                            headers: {{"Content-Type": "application/json"}},
                            body: JSON.stringify(updPayload(editor))
                        }}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        messageId,
                        "УПД сохранён."
                    );
                }} catch (error) {{
                    setFormMessage(
                        messageId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function addPeriodRow(applicationId) {{
                const editor = document.getElementById(
                    "period-editor-" + applicationId
                );
                const rows = editor.querySelector(".period-rows");
                const row = document.createElement("div");
                row.className = "period-edit-row";
                row.innerHTML =
                    '<label><span>Начало</span>' +
                    '<input type="date" class="period-start" required>' +
                    '</label>' +
                    '<label><span>Окончание</span>' +
                    '<input type="date" class="period-end" required>' +
                    '</label>' +
                    '<button type="button" ' +
                    'class="button button-danger button-icon" ' +
                    'title="Удалить период" aria-label="Удалить период" ' +
                    'onclick="removePeriodRow(this)">×</button>';
                rows.appendChild(row);
            }}

            function removePeriodRow(button) {{
                const rows = button
                    .closest(".period-rows")
                    .querySelectorAll(".period-edit-row");

                if (rows.length <= 1) {{
                    window.alert(
                        "У приложения должен остаться хотя бы один период."
                    );
                    return;
                }}

                button.closest(".period-edit-row").remove();
            }}

            async function sendPeriods(
                applicationId,
                periods,
                confirmRemoveFilled
            ) {{
                return requestJson(
                    "/applications/" + applicationId + "/periods",
                    {{
                        method: "PUT",
                        headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify({{
                            periods: periods,
                            confirm_remove_filled: confirmRemoveFilled
                        }})
                    }}
                );
            }}

            function filledReportLabel(report) {{
                const monthNames = [
                    "",
                    "ЯНВ",
                    "ФЕВ",
                    "МАР",
                    "АПР",
                    "МАЙ",
                    "ИЮН",
                    "ИЮЛ",
                    "АВГ",
                    "СЕН",
                    "ОКТ",
                    "НОЯ",
                    "ДЕК"
                ];

                return (
                    (monthNames[report.month] || report.month) +
                    " " +
                    report.year
                );
            }}

            async function savePeriods(applicationId) {{
                const editor = document.getElementById(
                    "period-editor-" + applicationId
                );
                const periods = Array.from(
                    editor.querySelectorAll(".period-edit-row")
                ).map(function(row) {{
                    return {{
                        start_date: row.querySelector(
                            ".period-start"
                        ).value,
                        end_date: row.querySelector(".period-end").value
                    }};
                }});

                setFormMessage(
                    "period-message-" + applicationId,
                    "Сохраняю...",
                    ""
                );

                try {{
                    let result = await sendPeriods(
                        applicationId,
                        periods,
                        false
                    );

                    if (result.status === "confirmation_required") {{
                        const labels = (result.filled_reports || [])
                            .map(filledReportLabel)
                            .join(", ");
                        const confirmed = window.confirm(
                            "Из новых периодов исключаются заполненные " +
                            "фотоотчёты: " + labels + ".\\n\\n" +
                            "Удалить эти строки вместе с данными?"
                        );

                        if (!confirmed) {{
                            setFormMessage(
                                "period-message-" + applicationId,
                                "Сохранение отменено. Данные не изменены.",
                                ""
                            );
                            return;
                        }}

                        result = await sendPeriods(
                            applicationId,
                            periods,
                            true
                        );
                    }}

                    setFormMessage(
                        "period-message-" + applicationId,
                        "Сохранено.",
                        "success"
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        "period-message-" + applicationId,
                        "Сохранено."
                    );
                }} catch (error) {{
                    setFormMessage(
                        "period-message-" + applicationId,
                        error.message,
                        "error"
                    );
                }}
            }}

            function togglePhotoUrl(reportId) {{
                const row = document.getElementById(
                    "photo-report-" + reportId
                );
                const checkbox = row.querySelector(
                    ".photo-not-required"
                );
                const url = row.querySelector(".photo-url");
                url.disabled = checkbox.checked;

                if (checkbox.checked) {{
                    url.value = "";
                }}
            }}

            async function savePhotoReport(applicationId, reportId) {{
                const row = document.getElementById(
                    "photo-report-" + reportId
                );
                const payload = {{
                    url: row.querySelector(".photo-url").value,
                    comment: row.querySelector(".photo-comment").value,
                    not_required: row.querySelector(
                        ".photo-not-required"
                    ).checked
                }};

                setFormMessage(
                    "photo-message-" + reportId,
                    "Сохраняю...",
                    ""
                );

                try {{
                    await requestJson(
                        "/applications/" + applicationId +
                        "/photo-reports/" + reportId,
                        {{
                            method: "PUT",
                            headers: {{
                                "Content-Type": "application/json"
                            }},
                            body: JSON.stringify(payload)
                        }}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        "photo-panel-message-" + applicationId,
                        "Сохранено."
                    );
                }} catch (error) {{
                    setFormMessage(
                        "photo-message-" + reportId,
                        error.message,
                        "error"
                    );
                }}
            }}

            async function deletePhotoReport(
                applicationId,
                reportId,
                label
            ) {{
                const confirmed = window.confirm(
                    "Удалить строку фотоотчёта за " + label + "?\\n\\n" +
                    "При следующем сохранении периодов этот месяц " +
                    "будет создан заново, если входит в период."
                );

                if (!confirmed) {{
                    return;
                }}

                try {{
                    await requestJson(
                        "/applications/" + applicationId +
                        "/photo-reports/" + reportId +
                        "?confirmed=true",
                        {{method: "DELETE"}}
                    );
                    reloadWithOpenApplication(
                        applicationId,
                        "photo-panel-message-" + applicationId,
                        "Строка фотоотчёта удалена."
                    );
                }} catch (error) {{
                    setFormMessage(
                        "photo-message-" + reportId,
                        error.message,
                        "error"
                    );
                }}
            }}
        </script>
    </body>
    </html>
    """

    return HTMLResponse(html_page)


@app.get("/health")
def health() -> dict[str, str]:
    """
    Простая проверка доступности приложения и Supabase.
    """

    supabase.table("legal_entities").select("id").limit(1).execute()

    return {
        "status": "ok",
        "database": "supabase",
        "app_mode": APP_MODE,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8002,
    )
