import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import create_client


TABLE_NAMES = (
    "legal_entities",
    "applications",
    "application_periods",
    "invoices",
    "payments",
    "photo_reports",
    "upds",
)

LEGACY_TABLE_NAMES = (
    "legal_entities",
    "applications",
    "application_periods",
    "invoices",
    "payments",
    "photo_reports",
)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_FILE = SCRIPT_DIR / "database.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR
DEFAULT_SNAPSHOT_ROOT = SCRIPT_DIR / "backups"


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def money_text(value: Any) -> str:
    return f"{money(value):.2f}"


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: Any) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def load_legacy_database(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл {path.resolve()}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("В database.json ожидался список записей")

    return data


def load_supabase_client():
    load_dotenv(SCRIPT_DIR / ".env")
    load_dotenv()

    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    )

    if not url or not key:
        raise RuntimeError(
            "В .env не найдены SUPABASE_URL и "
            "SUPABASE_SECRET_KEY (или SUPABASE_SERVICE_ROLE_KEY)"
        )

    return create_client(url, key)


def fetch_all(supabase, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_size = 1000
    start = 0

    while True:
        response = (
            supabase
            .table(table)
            .select("*")
            .range(start, start + page_size - 1)
            .execute()
        )

        batch = response.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def fetch_database(supabase) -> dict[str, list[dict[str, Any]]]:
    return {
        table_name: fetch_all(supabase, table_name)
        for table_name in TABLE_NAMES
    }


def unique_snapshot_directory(root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    candidate = root / f"supabase_snapshot_{timestamp}"
    suffix = 1

    while candidate.exists():
        candidate = root / f"supabase_snapshot_{timestamp}_{suffix}"
        suffix += 1

    return candidate


def write_snapshot(
    tables: dict[str, list[dict[str, Any]]],
    root: Path,
) -> Path:
    snapshot_dir = unique_snapshot_directory(root)
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Supabase",
        "read_only_export": True,
        "tables": {},
        "totals": {
            "invoice_amount": money_text(
                sum(
                    (
                        money(row.get("amount"))
                        for row in tables["invoices"]
                    ),
                    Decimal("0"),
                )
            ),
            "payment_amount": money_text(
                sum(
                    (
                        money(row.get("amount"))
                        for row in tables["payments"]
                    ),
                    Decimal("0"),
                )
            ),
        },
    }

    for table_name, rows in tables.items():
        content = json.dumps(
            rows,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        file_name = f"{table_name}.json"
        (snapshot_dir / file_name).write_bytes(content)
        manifest["tables"][table_name] = {
            "file": file_name,
            "rows": len(rows),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    manifest_content = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    (snapshot_dir / "manifest.json").write_bytes(manifest_content)

    return snapshot_dir


def add_result(
    results: list[dict[str, str]],
    severity: str,
    title: str,
    details: str,
) -> None:
    results.append(
        {
            "severity": severity,
            "title": title,
            "details": details,
        }
    )


def duplicate_nonblank(
    values: list[Any],
) -> list[str]:
    normalized = [clean_text(value) for value in values]
    return sorted(
        value
        for value, count in Counter(normalized).items()
        if value and count > 1
    )


def months_in_period(start_value: Any, end_value: Any):
    start = parse_date(start_value)
    end = parse_date(end_value)

    if start is None or end is None or start > end:
        return

    year = start.year
    month = start.month

    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1

        if month == 13:
            year += 1
            month = 1


def audit_integrity(
    tables: dict[str, list[dict[str, Any]]],
) -> list[dict[str, str]]:
    legal_entities = tables["legal_entities"]
    applications = tables["applications"]
    periods = tables["application_periods"]
    invoices = tables["invoices"]
    payments = tables["payments"]
    photo_reports = tables["photo_reports"]
    upds = tables["upds"]

    results: list[dict[str, str]] = []

    counts = "; ".join(
        f"{table_name}: {len(tables[table_name])}"
        for table_name in TABLE_NAMES
    )
    add_result(results, "INFO", "Количество строк", counts)

    duplicate_entities = duplicate_nonblank(
        [row.get("name") for row in legal_entities]
    )
    blank_entities = [
        row.get("id")
        for row in legal_entities
        if not clean_text(row.get("name"))
    ]
    add_result(
        results,
        "ERROR" if duplicate_entities or blank_entities else "PASS",
        "Юридические лица",
        (
            f"Дубли: {duplicate_entities or 'нет'}; "
            f"пустые названия, id: {blank_entities or 'нет'}"
        ),
    )

    entity_ids = {row.get("id") for row in legal_entities}
    application_ids = {row.get("id") for row in applications}
    invoice_ids = {row.get("id") for row in invoices}

    relation_checks = (
        (
            "Applications → legal_entities",
            [
                row.get("id")
                for row in applications
                if row.get("legal_entity_id") not in entity_ids
            ],
        ),
        (
            "Application periods → applications",
            [
                row.get("id")
                for row in periods
                if row.get("application_id") not in application_ids
            ],
        ),
        (
            "Invoices → legal_entities",
            [
                row.get("id")
                for row in invoices
                if row.get("legal_entity_id") not in entity_ids
            ],
        ),
        (
            "Invoices → applications",
            [
                row.get("id")
                for row in invoices
                if row.get("application_id") not in application_ids
            ],
        ),
        (
            "Payments → invoices",
            [
                row.get("id")
                for row in payments
                if row.get("invoice_id") not in invoice_ids
            ],
        ),
        (
            "Photo reports → applications",
            [
                row.get("id")
                for row in photo_reports
                if row.get("application_id") not in application_ids
            ],
        ),
        (
            "UPDs → invoices",
            [
                row.get("id")
                for row in upds
                if row.get("invoice_id") not in invoice_ids
            ],
        ),
    )

    for title, bad_ids in relation_checks:
        add_result(
            results,
            "ERROR" if bad_ids else "PASS",
            title,
            f"Некорректные id: {bad_ids or 'нет'}",
        )

    duplicate_invoice_numbers = duplicate_nonblank(
        [row.get("invoice_no") for row in invoices]
    )
    add_result(
        results,
        "ERROR" if duplicate_invoice_numbers else "PASS",
        "Уникальность номеров счетов",
        f"Дубли: {duplicate_invoice_numbers or 'нет'}",
    )

    period_keys = [
        (
            row.get("application_id"),
            row.get("start_date"),
            row.get("end_date"),
        )
        for row in periods
    ]
    duplicate_periods = [
        key
        for key, count in Counter(period_keys).items()
        if count > 1
    ]
    add_result(
        results,
        "ERROR" if duplicate_periods else "PASS",
        "Уникальность периодов",
        f"Дубли: {duplicate_periods or 'нет'}",
    )

    photo_keys = [
        (
            row.get("application_id"),
            row.get("report_year"),
            row.get("report_month"),
        )
        for row in photo_reports
    ]
    duplicate_photos = [
        key
        for key, count in Counter(photo_keys).items()
        if count > 1
    ]
    add_result(
        results,
        "ERROR" if duplicate_photos else "PASS",
        "Уникальность месяцев фотоотчётов",
        f"Дубли: {duplicate_photos or 'нет'}",
    )

    duplicate_upds = [
        invoice_id
        for invoice_id, count in Counter(
            row.get("invoice_id") for row in upds
        ).items()
        if invoice_id is not None and count > 1
    ]
    add_result(
        results,
        "ERROR" if duplicate_upds else "PASS",
        "Один УПД на счёт",
        f"Счета с несколькими УПД: {duplicate_upds or 'нет'}",
    )

    invalid_periods = [
        row.get("id")
        for row in periods
        if (
            parse_date(row.get("start_date")) is None
            or parse_date(row.get("end_date")) is None
            or parse_date(row.get("start_date"))
            > parse_date(row.get("end_date"))
        )
    ]
    add_result(
        results,
        "ERROR" if invalid_periods else "PASS",
        "Корректность периодов",
        f"Некорректные period id: {invalid_periods or 'нет'}",
    )

    invalid_service_periods = [
        row.get("id")
        for row in invoices
        if (
            row.get("service_start_date")
            and row.get("service_end_date")
            and (
                parse_date(row.get("service_start_date")) is None
                or parse_date(row.get("service_end_date")) is None
                or parse_date(row.get("service_start_date"))
                > parse_date(row.get("service_end_date"))
            )
        )
    ]
    add_result(
        results,
        "ERROR" if invalid_service_periods else "PASS",
        "Корректность периодов услуг",
        f"Некорректные invoice id: {invalid_service_periods or 'нет'}",
    )

    invalid_invoice_amounts = [
        row.get("id")
        for row in invoices
        if money(row.get("amount")) <= 0
    ]
    invalid_payment_amounts = [
        row.get("id")
        for row in payments
        if money(row.get("amount")) <= 0
    ]
    add_result(
        results,
        "ERROR" if invalid_invoice_amounts else "PASS",
        "Положительные суммы счетов",
        f"Некорректные invoice id: {invalid_invoice_amounts or 'нет'}",
    )
    add_result(
        results,
        "ERROR" if invalid_payment_amounts else "PASS",
        "Положительные суммы платежей",
        f"Некорректные payment id: {invalid_payment_amounts or 'нет'}",
    )

    invalid_photo_months = [
        row.get("id")
        for row in photo_reports
        if not 1 <= integer(row.get("report_month")) <= 12
    ]
    contradictory_photos = [
        row.get("id")
        for row in photo_reports
        if row.get("not_required") and clean_text(row.get("url"))
    ]
    add_result(
        results,
        "ERROR" if invalid_photo_months else "PASS",
        "Месяцы фотоотчётов 1–12",
        f"Некорректные photo_report id: {invalid_photo_months or 'нет'}",
    )
    add_result(
        results,
        "ERROR" if contradictory_photos else "PASS",
        "Фотоотчёт: ссылка или «не требуется»",
        (
            "Одновременно заполнена ссылка и not_required=true, id: "
            f"{contradictory_photos or 'нет'}"
        ),
    )

    invoices_by_application: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for invoice in invoices:
        invoices_by_application[invoice.get("application_id")].append(invoice)

    application_invoice_mismatches = []
    for application in applications:
        application_id = application.get("id")
        invoice_total = sum(
            (
                money(row.get("amount"))
                for row in invoices_by_application.get(application_id, [])
            ),
            Decimal("0"),
        )
        application_total = money(application.get("total_sum"))

        if invoice_total != application_total:
            application_invoice_mismatches.append(
                {
                    "application_id": application_id,
                    "app_no": application.get("app_no"),
                    "application_total": money_text(application_total),
                    "invoice_total": money_text(invoice_total),
                    "difference": money_text(
                        application_total - invoice_total
                    ),
                }
            )

    add_result(
        results,
        "WARN" if application_invoice_mismatches else "PASS",
        "Сумма приложения равна сумме счетов",
        (
            f"Расхождения: "
            f"{application_invoice_mismatches or 'нет'}"
        ),
    )

    paid_by_invoice: dict[Any, Decimal] = defaultdict(Decimal)
    for payment in payments:
        paid_by_invoice[payment.get("invoice_id")] += money(
            payment.get("amount")
        )

    overpaid = []
    for invoice in invoices:
        invoice_id = invoice.get("id")
        paid = paid_by_invoice[invoice_id]
        amount = money(invoice.get("amount"))

        if paid > amount:
            overpaid.append(
                {
                    "invoice_id": invoice_id,
                    "invoice_no": invoice.get("invoice_no"),
                    "invoice_amount": money_text(amount),
                    "paid_amount": money_text(paid),
                }
            )

    add_result(
        results,
        "ERROR" if overpaid else "PASS",
        "Платежи не превышают сумму счёта",
        f"Переплаты: {overpaid or 'нет'}",
    )

    required_photo_keys = set()
    for period in periods:
        for year, month in months_in_period(
            period.get("start_date"),
            period.get("end_date"),
        ) or ():
            required_photo_keys.add(
                (period.get("application_id"), year, month)
            )

    actual_photo_keys = {
        (
            row.get("application_id"),
            integer(row.get("report_year")),
            integer(row.get("report_month")),
        )
        for row in photo_reports
    }
    missing_photo_months = sorted(
        required_photo_keys - actual_photo_keys,
        key=lambda item: (str(item[0]), item[1], item[2]),
    )
    add_result(
        results,
        "WARN" if missing_photo_months else "PASS",
        "Месяцы периодов покрыты фотоотчётами",
        (
            "Отсутствующие (application_id, год, месяц): "
            f"{missing_photo_months or 'нет'}"
        ),
    )

    upd_invoice_ids = {
        row.get("invoice_id")
        for row in upds
        if row.get("invoice_id") is not None
    }
    invoice_ids_without_upd = sorted(
        invoice_id
        for invoice_id in invoice_ids
        if invoice_id not in upd_invoice_ids
    )
    add_result(
        results,
        "ERROR" if invoice_ids_without_upd else "PASS",
        "Для каждого счёта существует УПД",
        (
            f"Счетов без УПД: {len(invoice_ids_without_upd)}; "
            f"id: {invoice_ids_without_upd or 'нет'}"
        ),
    )

    invoices_by_id = {
        row.get("id"): row
        for row in invoices
    }
    upd_date_mismatches = []

    for upd in upds:
        invoice = invoices_by_id.get(upd.get("invoice_id"))

        if invoice is None:
            continue

        if (
            parse_date(upd.get("upd_date"))
            != parse_date(invoice.get("shipment_date"))
        ):
            upd_date_mismatches.append(
                {
                    "upd_id": upd.get("id"),
                    "invoice_id": invoice.get("id"),
                    "invoice_no": invoice.get("invoice_no"),
                    "upd_date": upd.get("upd_date"),
                    "shipment_date": invoice.get("shipment_date"),
                }
            )

    add_result(
        results,
        "ERROR" if upd_date_mismatches else "PASS",
        "Дата УПД равна дате отгрузки счёта",
        f"Расхождения: {upd_date_mismatches or 'нет'}",
    )

    invoice_total = sum(
        (money(row.get("amount")) for row in invoices),
        Decimal("0"),
    )
    payment_total = sum(
        (money(row.get("amount")) for row in payments),
        Decimal("0"),
    )
    add_result(
        results,
        "INFO",
        "Контрольные суммы",
        (
            f"Счета: {money_text(invoice_total)}; "
            f"платежи: {money_text(payment_total)}"
        ),
    )

    return results


def calculate_legacy_expected(
    data: list[dict[str, Any]],
) -> dict[str, Any]:
    legal_entities = {
        clean_text(item.get("entity"))
        for item in data
        if clean_text(item.get("entity"))
    }
    invoices = 0
    payments = 0
    photo_reports = 0
    invoice_amount_total = Decimal("0")
    payment_amount_total = Decimal("0")

    for item in data:
        total_sum = money(item.get("total_sum"))

        if total_sum > 0 and clean_text(item.get("bill_no")):
            schedule = item.get("payments") or []

            if schedule:
                invoices += len(schedule)
                invoice_amount_total += sum(
                    (money(row.get("amount")) for row in schedule),
                    Decimal("0"),
                )
            else:
                invoices += 1
                invoice_amount_total += total_sum

        for row in item.get("payments") or []:
            if clean_text(row.get("status")).lower() == "paid":
                payments += 1
                payment_amount_total += money(row.get("amount"))

        photo_reports += len(item.get("photo_reports") or {})

    return {
        "legal_entities": len(legal_entities),
        "applications": len(data),
        "application_periods": sum(
            1 for item in data if clean_text(item.get("period"))
        ),
        "invoices": invoices,
        "payments": payments,
        "photo_reports": photo_reports,
        "invoice_amount_total": invoice_amount_total,
        "payment_amount_total": payment_amount_total,
    }


def expected_legacy_payments(
    data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected = []

    for item in data:
        schedule = item.get("payments") or []
        base_invoice_no = clean_text(item.get("bill_no"))
        multiple = len(schedule) > 1

        for payment_index, payment in enumerate(schedule):
            if clean_text(payment.get("status")).lower() != "paid":
                continue

            invoice_no = (
                f"{base_invoice_no}/{payment_index + 1}"
                if multiple
                else base_invoice_no
            )
            expected.append(
                {
                    "legacy_application_id": item.get("id"),
                    "application_no": item.get("app_no"),
                    "payment_line": payment_index + 1,
                    "invoice_no": invoice_no,
                    "amount": money(payment.get("amount")),
                    "comment": clean_text(payment.get("comment")),
                    "payment_date": None,
                }
            )

    return expected


def payment_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        clean_text(row.get("invoice_no")),
        money(row.get("amount")),
        clean_text(row.get("comment")),
        row.get("payment_date") or None,
    )


def public_payment_details(row: dict[str, Any]) -> dict[str, Any]:
    details = {
        "invoice_no": row.get("invoice_no"),
        "amount": money_text(row.get("amount")),
        "comment": row.get("comment") or "",
        "payment_date": row.get("payment_date"),
    }

    if "legacy_application_id" in row:
        details.update(
            {
                "legacy_application_id": row.get(
                    "legacy_application_id"
                ),
                "application_no": row.get("application_no"),
                "payment_line": row.get("payment_line"),
            }
        )
    else:
        details["payment_id"] = row.get("payment_id")

    return details


def compare_legacy_import(
    data: list[dict[str, Any]],
    tables: dict[str, list[dict[str, Any]]],
) -> list[dict[str, str]]:
    expected = calculate_legacy_expected(data)
    results: list[dict[str, str]] = []

    for table_name in LEGACY_TABLE_NAMES:
        wanted = expected[table_name]
        actual = len(tables[table_name])
        add_result(
            results,
            "PASS" if actual == wanted else "ERROR",
            f"Количество строк: {table_name}",
            f"Ожидалось при импорте: {wanted}; фактически: {actual}",
        )

    actual_invoice_total = sum(
        (
            money(row.get("amount"))
            for row in tables["invoices"]
        ),
        Decimal("0"),
    )
    actual_payment_total = sum(
        (
            money(row.get("amount"))
            for row in tables["payments"]
        ),
        Decimal("0"),
    )

    add_result(
        results,
        (
            "PASS"
            if actual_invoice_total == expected["invoice_amount_total"]
            else "ERROR"
        ),
        "Общая сумма счетов после импорта",
        (
            f"Ожидалось: {money_text(expected['invoice_amount_total'])}; "
            f"фактически: {money_text(actual_invoice_total)}"
        ),
    )
    add_result(
        results,
        (
            "PASS"
            if actual_payment_total == expected["payment_amount_total"]
            else "ERROR"
        ),
        "Общая сумма платежей после импорта",
        (
            f"Ожидалось: {money_text(expected['payment_amount_total'])}; "
            f"фактически: {money_text(actual_payment_total)}; "
            f"разница: "
            f"{money_text(expected['payment_amount_total'] - actual_payment_total)}"
        ),
    )

    invoice_number_by_id = {
        row.get("id"): clean_text(row.get("invoice_no"))
        for row in tables["invoices"]
    }
    actual_payments = [
        {
            "payment_id": row.get("id"),
            "invoice_no": invoice_number_by_id.get(row.get("invoice_id")),
            "amount": money(row.get("amount")),
            "comment": clean_text(row.get("comment")),
            "payment_date": row.get("payment_date") or None,
        }
        for row in tables["payments"]
    ]
    expected_payments = expected_legacy_payments(data)

    remaining_actual = Counter(
        payment_key(row) for row in actual_payments
    )
    missing = []

    for row in expected_payments:
        key = payment_key(row)

        if remaining_actual[key] > 0:
            remaining_actual[key] -= 1
        else:
            missing.append(public_payment_details(row))

    remaining_expected = Counter(
        payment_key(row) for row in expected_payments
    )
    extra = []

    for row in actual_payments:
        key = payment_key(row)

        if remaining_expected[key] > 0:
            remaining_expected[key] -= 1
        else:
            extra.append(public_payment_details(row))

    add_result(
        results,
        "ERROR" if missing else "PASS",
        "Платежи, отсутствующие в Supabase",
        f"Найдено: {len(missing)}; строки: {missing or 'нет'}",
    )
    add_result(
        results,
        "WARN" if extra else "PASS",
        "Дополнительные или изменённые платежи в Supabase",
        f"Найдено: {len(extra)}; строки: {extra or 'нет'}",
    )

    return results


def render_report(
    title: str,
    introduction: list[str],
    results: list[dict[str, str]],
) -> str:
    severity_labels = {
        "PASS": "✅",
        "INFO": "ℹ️",
        "WARN": "⚠️",
        "ERROR": "❌",
    }
    counts = Counter(result["severity"] for result in results)
    lines = [
        f"# {title}",
        "",
        *introduction,
        "",
        "## Итог",
        "",
        f"- Успешно: **{counts['PASS']}**",
        f"- Информация: **{counts['INFO']}**",
        f"- Предупреждения: **{counts['WARN']}**",
        f"- Ошибки: **{counts['ERROR']}**",
        "",
    ]

    for result in results:
        mark = severity_labels[result["severity"]]
        lines.extend(
            [
                f"## {mark} {result['title']}",
                "",
                result["details"],
                "",
            ]
        )

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only аудит Supabase и одноразовая сверка "
            "с исходным database.json."
        )
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Сохранить локальный JSON-снимок всех таблиц.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_FILE,
        help="Путь к legacy-файлу database.json.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Каталог для отчётов.",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help="Каталог для локальных снимков.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    supabase = load_supabase_client()
    tables = fetch_database(supabase)

    snapshot_dir = None
    if args.snapshot:
        snapshot_dir = write_snapshot(
            tables,
            args.snapshot_root.resolve(),
        )

    integrity_results = audit_integrity(tables)
    integrity_report = render_report(
        "Billing Control v2 — целостность Supabase",
        [
            (
                "Этот постоянный аудит проверяет связи, дубли, "
                "значения и контрольные суммы внутри Supabase."
            ),
            (
                "`database.json` не используется как эталон "
                "для этого отчёта."
            ),
        ],
        integrity_results,
    )
    integrity_path = output_dir / "database_integrity_report.md"
    integrity_path.write_text(integrity_report, encoding="utf-8")

    legacy_data = load_legacy_database(args.database.resolve())
    legacy_results = compare_legacy_import(legacy_data, tables)
    legacy_report = render_report(
        "Billing Control v2 — одноразовая сверка импорта",
        [
            (
                "Этот отчёт сравнивает первоначальный перенос данных "
                "из `database.json` с текущими строками Supabase."
            ),
            (
                "После начала ручного редактирования он остаётся "
                "исторической проверкой импорта, а не постоянным "
                "аудитом рабочей базы."
            ),
            (
                "Производные строки УПД не сравниваются с legacy-файлом: "
                "их целостность проверяется в постоянном аудите Supabase."
            ),
        ],
        legacy_results,
    )
    legacy_path = output_dir / "legacy_import_comparison_report.md"
    legacy_path.write_text(legacy_report, encoding="utf-8")

    integrity_counts = Counter(
        result["severity"] for result in integrity_results
    )
    legacy_counts = Counter(
        result["severity"] for result in legacy_results
    )

    print("АУДИТ BILLING CONTROL V2 ЗАВЕРШЁН")
    print("Supabase использовалась только для чтения.")
    if snapshot_dir:
        print(f"Локальный снимок: {snapshot_dir}")
    print(f"Отчёт о целостности: {integrity_path}")
    print(f"Сверка импорта: {legacy_path}")
    print(
        "Целостность — "
        f"ошибок: {integrity_counts['ERROR']}, "
        f"предупреждений: {integrity_counts['WARN']}"
    )
    print(
        "Импорт — "
        f"ошибок: {legacy_counts['ERROR']}, "
        f"предупреждений: {legacy_counts['WARN']}"
    )


if __name__ == "__main__":
    main()
