from typing import Final


SAFE_HTTP_METHODS: Final[frozenset[str]] = frozenset({
    "GET",
    "HEAD",
    "OPTIONS",
})


def resolve_app_mode(raw_value: str | None) -> str:
    """
    Возвращает безопасный режим приложения.

    Редактирование разрешается только явным значением ``edit``.
    Пустое, неизвестное или ошибочное значение означает ``read``.
    """

    normalized = str(raw_value or "").strip().lower()
    return "edit" if normalized == "edit" else "read"


def is_mutating_request(method: str) -> bool:
    """Определяет, может ли HTTP-метод изменять данные."""

    return str(method or "").upper() not in SAFE_HTTP_METHODS


def is_request_allowed(app_mode: str, method: str) -> bool:
    """
    Проверяет разрешение HTTP-запроса для выбранного режима.

    В ``edit`` разрешены все методы. В любом другом режиме разрешено
    только безопасное чтение.
    """

    return (
        resolve_app_mode(app_mode) == "edit"
        or not is_mutating_request(method)
    )
