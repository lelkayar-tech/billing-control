import datetime
from typing import Any
from urllib.parse import urlencode, urlparse


class InputValidationError(ValueError):
    """Ошибка пользовательского ввода, которую можно безопасно показать."""


def _parse_iso_date(value: Any, field_name: str) -> datetime.date:
    text = str(value or "").strip()

    if not text:
        raise InputValidationError(f"Поле «{field_name}» обязательно.")

    try:
        return datetime.date.fromisoformat(text)
    except ValueError as error:
        raise InputValidationError(
            f"В поле «{field_name}» указана некорректная дата."
        ) from error


def _next_month(value: datetime.date) -> datetime.date:
    if value.month == 12:
        return datetime.date(value.year + 1, 1, 1)

    return datetime.date(value.year, value.month + 1, 1)


def normalize_periods(raw_periods: Any) -> list[dict[str, str]]:
    """
    Проверяет и нормализует весь набор периодов одного приложения.

    Периоды сохраняются одним атомарным запросом, поэтому дубли и
    некорректные интервалы отбрасываются до обращения к базе.
    """

    if not isinstance(raw_periods, list):
        raise InputValidationError("Список периодов передан неверно.")

    if not raw_periods:
        raise InputValidationError(
            "У приложения должен остаться хотя бы один период."
        )

    if len(raw_periods) > 50:
        raise InputValidationError(
            "Нельзя сохранить больше 50 периодов за один раз."
        )

    normalized: list[dict[str, str]] = []
    seen: set[tuple[datetime.date, datetime.date]] = set()

    for index, raw_period in enumerate(raw_periods, start=1):
        if not isinstance(raw_period, dict):
            raise InputValidationError(
                f"Период {index} передан в неверном формате."
            )

        start = _parse_iso_date(
            raw_period.get("start_date"),
            f"Начало периода {index}",
        )
        end = _parse_iso_date(
            raw_period.get("end_date"),
            f"Окончание периода {index}",
        )

        if end < start:
            raise InputValidationError(
                f"В периоде {index} дата окончания раньше даты начала."
            )

        key = (start, end)

        if key in seen:
            raise InputValidationError(
                f"Период {index} полностью дублирует другой период."
            )

        seen.add(key)
        normalized.append({
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        })

    return sorted(
        normalized,
        key=lambda period: (
            period["start_date"],
            period["end_date"],
        ),
    )


def calendar_months(
    periods: list[dict[str, str]],
) -> list[tuple[int, int]]:
    """Возвращает уникальные календарные месяцы заданных периодов."""

    months: set[tuple[int, int]] = set()

    for period in periods:
        start = datetime.date.fromisoformat(period["start_date"])
        end = datetime.date.fromisoformat(period["end_date"])
        current = start.replace(day=1)
        last = end.replace(day=1)

        while current <= last:
            months.add((current.year, current.month))

            if current.month == 12:
                current = datetime.date(current.year + 1, 1, 1)
            else:
                current = datetime.date(
                    current.year,
                    current.month + 1,
                    1,
                )

    return sorted(months)


def normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def normalize_photo_url(value: Any) -> str | None:
    """Разрешает только полноценные HTTP(S)-ссылки."""

    text = normalize_optional_text(value)

    if text is None:
        return None

    parsed = urlparse(text)

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or any(character.isspace() for character in text)
    ):
        raise InputValidationError(
            "Ссылка фотоотчёта должна начинаться с http:// или https://."
        )

    return text


def normalize_photo_report_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise InputValidationError(
            "Данные фотоотчёта переданы неверно."
        )

    not_required = raw_payload.get("not_required") is True
    url = normalize_photo_url(raw_payload.get("url"))
    comment = normalize_optional_text(raw_payload.get("comment"))

    if not_required:
        url = None

    return {
        "url": url,
        "not_required": not_required,
        "comment": comment,
    }


def photo_report_month_end(
    report: dict[str, Any],
) -> datetime.date:
    """
    Возвращает последний день месяца конкретного фотоотчёта.

    Просрочка определяется отдельно для каждого месячного фотоотчёта,
    даже если общий период размещения продолжается в следующих месяцах.
    """

    try:
        report_year = int(report.get("report_year") or 0)
        report_month = int(report.get("report_month") or 0)
        month_start = datetime.date(report_year, report_month, 1)
    except (TypeError, ValueError):
        return datetime.date.min

    next_month = _next_month(month_start)
    return next_month - datetime.timedelta(days=1)


def photo_report_is_overdue(
    report: dict[str, Any],
    today: datetime.date,
) -> bool:
    """Проверяет, закончился ли месяц конкретного фотоотчёта."""

    return photo_report_month_end(report) < today


def build_filter_url(
    current: dict[str, str | None],
    **updates: str | None,
) -> str:
    """Строит ссылку фильтра, не сбрасывая остальные выбранные значения."""

    allowed_keys = ("entity", "month", "status")
    merged = {
        key: updates.get(key, current.get(key))
        for key in allowed_keys
    }
    query = {
        key: value
        for key, value in merged.items()
        if value not in (None, "")
    }

    return f"/?{urlencode(query)}" if query else "/"
