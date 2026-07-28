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
    normalize_invoice_payload,
    normalize_payment_payload,
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
    payments_by_invoice: dict[int, list[dict[str, Any]]],
    today: datetime.date,
) -> tuple[str, str]:
    """
    Возвращает:
    - текст статуса;
    - CSS-класс статуса.
    """

    if invoices:
        invoice_total = sum(
            (money(invoice.get("amount")) for invoice in invoices),
            Decimal("0"),
        )

        paid_total = Decimal("0")
        has_overdue = False

        for invoice in invoices:
            invoice_id = invoice["id"]
            paid = sum(
                (
                    money(payment.get("amount"))
                    for payment in payments_by_invoice.get(
                        invoice_id,
                        [],
                    )
                ),
                Decimal("0"),
            )
            paid_total += paid

            due_date = parse_date(invoice.get("due_date"))

            if (
                due_date is not None
                and due_date < today
                and paid < money(invoice.get("amount"))
            ):
                has_overdue = True

        if invoice_total > 0 and paid_total >= invoice_total:
            return "ОПЛАЧЕНО", "status-paid"

        if has_overdue:
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
        .select("id,legal_entity_id,content")
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
        payload = normalize_invoice_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        application = fetch_application_for_mutation(application_id)

        if application is None:
            return mutation_error("Приложение не найдено.", 404)

        payload.update({
            "application_id": application_id,
            "legal_entity_id": application["legal_entity_id"],
            "base_invoice_no": payload["invoice_no"],
        })

        inserted = (
            supabase
            .table("invoices")
            .insert(payload)
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
        payload = normalize_invoice_payload(raw_payload)
    except FinanceValidationError as error:
        return mutation_error(str(error), 422)

    try:
        existing = fetch_invoice_for_mutation(
            application_id,
            invoice_id,
        )

        if existing is None:
            return mutation_error("Счёт не найден.", 404)

        if existing.get("base_invoice_no") == existing.get("invoice_no"):
            payload["base_invoice_no"] = payload["invoice_no"]

        (
            supabase
            .table("invoices")
            .update(payload)
            .eq("id", invoice_id)
            .eq("application_id", application_id)
            .execute()
        )

        verified = (
            supabase
            .table("invoices")
            .select(
                "id,invoice_no,content,amount,service_start_date,"
                "service_end_date,due_date"
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

    return JSONResponse(
        content={"status": "saved", "invoice": verified.data[0]},
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
        "content,amount,service_start_date,service_end_date,due_date",
    )
    payments = fetch_all(
        "payments",
        "id,invoice_id,amount,payment_date",
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
    payments_by_invoice: dict[int, list[dict[str, Any]]] = defaultdict(list)
    reports_by_application: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in periods:
        periods_by_application[row["application_id"]].append(row)

    for row in invoices:
        application_id = row.get("application_id")

        if application_id is not None:
            invoices_by_application[application_id].append(row)

    for row in payments:
        payments_by_invoice[row["invoice_id"]].append(row)

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
            "periods": periods_by_application.get(application_id, []),
            "invoices": app_invoices,
            "payments_by_invoice": payments_by_invoice,
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

def invoice_form_html(
    application_id: int,
    invoice: dict[str, Any] | None,
    default_content: str,
    suggested_invoice_no: str = "",
) -> str:
    invoice_id = int(invoice["id"]) if invoice else None
    form_key = str(invoice_id) if invoice_id is not None else "new"
    invoice_no = escape(
        invoice.get("invoice_no") if invoice else suggested_invoice_no
    )
    content = escape(
        invoice.get("content") if invoice else default_content
    )
    amount = (
        format(money(invoice.get("amount")), "f")
        if invoice
        else ""
    )
    service_start = escape(
        invoice.get("service_start_date") if invoice else ""
    )
    service_end = escape(
        invoice.get("service_end_date") if invoice else ""
    )
    due_date = escape(invoice.get("due_date") if invoice else "")
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
            <label class="invoice-content-field">
                <span>Содержание</span>
                <input
                    type="text"
                    class="invoice-content"
                    value="{content}"
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
                <span>Начало услуг</span>
                <input
                    type="date"
                    class="invoice-service-start"
                    value="{service_start}"
                >
            </label>
            <label>
                <span>Окончание услуг</span>
                <input
                    type="date"
                    class="invoice-service-end"
                    value="{service_end}"
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
    payments_by_invoice: dict[int, list[dict[str, Any]]],
    today: datetime.date,
) -> str:
    application_id = int(application["id"])
    suggested_invoice_no = suggest_next_invoice_number(
        application.get("app_no"),
        invoices,
    )
    rows = ""

    for invoice in invoices:
        invoice_id = int(invoice["id"])
        payments = payments_by_invoice.get(invoice_id, [])
        invoice_amount = money(invoice.get("amount"))
        paid_amount = sum(
            (money(payment.get("amount")) for payment in payments),
            Decimal("0"),
        )
        debt = max(invoice_amount - paid_amount, Decimal("0"))
        due_date = parse_date(invoice.get("due_date"))
        is_overdue = (
            due_date is not None
            and due_date < today
            and debt > 0
        )

        if paid_amount >= invoice_amount and invoice_amount > 0:
            payment_status = (
                '<span class="mini-status mini-paid">ОПЛАЧЕНО</span>'
            )
        elif is_overdue:
            payment_status = (
                '<span class="mini-status mini-overdue">ПРОСРОЧЕНО</span>'
            )
        else:
            payment_status = (
                '<span class="mini-status mini-waiting">ОЖИДАЕТ</span>'
            )

        invoice_actions = ""

        if IS_EDIT_MODE:
            invoice_actions = f"""
            <div class="invoice-row-actions">
                <button
                    type="button"
                    class="icon-action"
                    title="Изменить счёт"
                    aria-label="Изменить счёт"
                    onclick="showEditor(
                        'invoice-form-{application_id}-{invoice_id}'
                    )"
                >✎</button>
                <button
                    type="button"
                    class="icon-action icon-action-danger"
                    title="Удалить счёт"
                    aria-label="Удалить счёт"
                    onclick="deleteInvoice(
                        {application_id},
                        {invoice_id}
                    )"
                >×</button>
            </div>
            """

        rows += f"""
        <div class="finance-entry">
            <div class="finance-row">
                <div class="finance-invoice">
                    <div class="invoice-title-line">
                        <strong>
                            {escape(invoice.get("invoice_no") or "Без номера")}
                        </strong>
                        {payment_status}
                    </div>
                    <span class="invoice-content">
                        {escape(invoice.get("content") or "")}
                    </span>
                </div>
                <strong class="finance-number">
                    {format_money(invoice_amount)} ₽
                </strong>
                <strong class="finance-date">
                    {format_date(invoice.get("due_date"))}
                </strong>
                <div class="finance-payment-stack">
                    {
                        payment_lines_html(
                            application_id,
                            invoice_id,
                            payments,
                        )
                    }
                </div>
                <strong class="finance-number {'danger' if debt > 0 else 'success'}">
                    {format_money(debt)} ₽
                </strong>
                {invoice_actions}
            </div>
            {
                invoice_form_html(
                    application_id,
                    invoice,
                    str(application.get("content") or ""),
                )
                if IS_EDIT_MODE
                else ""
            }
            {
                payment_editors_html(
                    application_id,
                    invoice_id,
                    payments,
                )
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
                str(application.get("content") or ""),
                suggested_invoice_no,
            )
        }
        """

    action_header = "<span></span>" if IS_EDIT_MODE else ""

    return f"""
    <div class="finance-table">
        <div class="finance-header">
            <span class="finance-header-invoice">Счёт</span>
            <span class="finance-header-number">Сумма счёта</span>
            <span class="finance-header-date">Срок оплаты</span>
            <span class="finance-header-payments">Фактические оплаты</span>
            <span class="finance-header-number">Остаток</span>
            {action_header}
        </div>
        {rows}
        {add_invoice}
        <div
            id="finance-message-{application_id}"
            class="form-message finance-panel-message"
        ></div>
    </div>
    """


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

    if not IS_EDIT_MODE:
        if not sorted_periods:
            return '<div class="muted">Периоды не заданы</div>'

        return "".join(
            f"""
            <div class="period-view-row">
                <span>{format_date(period.get("start_date"))}</span>
                <span>—</span>
                <strong>{format_date(period.get("end_date"))}</strong>
            </div>
            """
            for period in sorted_periods
        )

    rows = ""

    for period in sorted_periods:
        rows += f"""
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

    if not rows:
        rows = """
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
    <div
        id="period-editor-{application_id}"
        class="period-editor"
        data-application-id="{application_id}"
    >
        <div class="period-rows">
            {rows}
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
                f'<span class="photo-period photo-pending">{label}</span>'
            )

        if IS_EDIT_MODE:
            checked = "checked" if is_not_required else ""
            disabled = "disabled" if is_not_required else ""
            comment_view = (
                f'<span class="photo-comment-view">{comment}</span>'
                if comment
                else ""
            )

            result += f"""
            <div
                class="photo-record"
                id="photo-report-{report_id}"
            >
                <div class="photo-record-view">
                    {period_title}
                    {comment_view}
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

                    <label class="photo-comment-field">
                        <span>Комментарий</span>
                        <input
                            type="text"
                            class="photo-comment"
                            value="{comment}"
                        >
                    </label>

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

        comment_html = ""

        if comment:
            comment_html = (
                '<span class="photo-comment-view">'
                + comment
                + "</span>"
            )

        result += f"""
        <div class="photo-row">
            {period_title}
            {comment_html}
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
            item["payments_by_invoice"],
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
        paid_total = sum(
            (
                money(payment.get("amount"))
                for invoice in invoices
                for payment in item["payments_by_invoice"].get(
                    invoice["id"],
                    [],
                )
            ),
            Decimal("0"),
        )
        debt = max(invoice_total - paid_total, Decimal("0"))

        invoice_numbers = ", ".join(
            str(invoice.get("invoice_no") or "")
            for invoice in invoices
        ) or "—"

        finance_block = finance_table_html(
            application,
            invoices,
            item["payments_by_invoice"],
            today,
        )
        periods_block = periods_html(
            int(application["id"]),
            item["periods"],
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
                <td class="right mono {'danger' if debt > 0 else 'success'}">
                    {format_money(debt)} ₽
                </td>
            </tr>

            <tr id="details-{application['id']}" class="details-row hidden">
                <td colspan="7">
                    <div class="details-grid">
                        <section class="panel panel-application">
                            <h3>Приложение</h3>

                            <div class="application-info">
                                <div class="info-row">
                                    <span>Номер</span>
                                    <strong>
                                        {escape(application.get("app_no") or "—")}
                                    </strong>
                                </div>

                                <div class="info-row">
                                    <span>Юридическое лицо</span>
                                    <strong>{escape(item["entity_name"])}</strong>
                                </div>

                                <div class="info-row">
                                    <span>Содержание</span>
                                    <strong>
                                        {escape(application.get("content") or "—")}
                                    </strong>
                                </div>

                                <div class="info-row">
                                    <span>Сумма приложения</span>
                                    <strong>
                                        {format_money(application.get("total_sum"))} ₽
                                    </strong>
                                </div>

                                <div class="info-row">
                                    <span>Отправлено</span>
                                    <strong>
                                        {format_date(application.get("sent_at"))}
                                    </strong>
                                </div>

                                <div class="info-row">
                                    <span>Подписано</span>
                                    <strong>
                                        {format_date(application.get("signed_at"))}
                                    </strong>
                                </div>
                            </div>

                            <div class="panel-subsection panel-periods">
                                <h3>Периоды размещения</h3>
                                {periods_block}
                            </div>
                        </section>

                        <section class="panel panel-finance">
                            <h3>Счета и оплаты</h3>
                            {finance_block}
                        </section>

                        <section class="panel panel-photos">
                            <h3>Фотоотчёты</h3>
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
                <td colspan="7" class="empty-state">
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
                    minmax(700px, 2.25fr)
                    minmax(270px, 0.85fr);
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
                    82px
                    48px;
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

            .finance-header-date {{
                padding-right: 16px;
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

            .finance-date {{
                padding-right: 16px;
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

            .inline-actions,
            .invoice-row-actions {{
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

            .invoice-editor-fields {{
                grid-template-columns:
                    minmax(100px, 0.8fr)
                    minmax(180px, 1.5fr)
                    minmax(80px, 0.65fr)
                    repeat(3, minmax(105px, 0.8fr));
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

            .period-view-row {{
                display: grid;
                grid-template-columns: 1fr auto 1fr;
                gap: 9px;
                padding: 8px 0;
                border-bottom: 1px solid var(--slate-100);
                font-size: 10px;
            }}

            .period-view-row strong {{
                text-align: right;
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
                            <th class="right">Долг</th>
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

            function invoicePayload(editor) {{
                return {{
                    invoice_no: editor.querySelector(
                        ".invoice-number"
                    ).value,
                    content: editor.querySelector(
                        ".invoice-content"
                    ).value,
                    amount: editor.querySelector(
                        ".invoice-amount"
                    ).value,
                    service_start_date: editor.querySelector(
                        ".invoice-service-start"
                    ).value,
                    service_end_date: editor.querySelector(
                        ".invoice-service-end"
                    ).value,
                    due_date: editor.querySelector(
                        ".invoice-due-date"
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
                    "Вместе со счётом будут безвозвратно удалены " +
                    "все связанные с ним платежи и УПД."
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
