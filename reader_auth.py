import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit


SESSION_COOKIE_NAME = "billing_reader_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
_TOKEN_VERSION = "v1"


def reader_auth_is_configured(
    password: str | None,
    session_secret: str | None,
) -> bool:
    """Проверяет, заданы ли оба секрета читательского доступа."""

    return bool(str(password or "")) and bool(str(session_secret or ""))


def verify_reader_password(
    submitted_password: str,
    expected_password: str,
) -> bool:
    """Сравнивает пароль без раннего выхода по несовпавшему символу."""

    submitted_digest = hashlib.sha256(
        str(submitted_password).encode("utf-8")
    ).digest()
    expected_digest = hashlib.sha256(
        str(expected_password).encode("utf-8")
    ).digest()
    return hmac.compare_digest(submitted_digest, expected_digest)


def create_reader_session(
    session_secret: str,
    *,
    issued_at: int | None = None,
) -> str:
    """Создаёт подписанный токен без пароля и данных пользователя внутри."""

    timestamp = int(time.time() if issued_at is None else issued_at)
    payload = f"{timestamp}:{secrets.token_urlsafe(16)}".encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(
        str(session_secret).encode("utf-8"),
        encoded_payload,
        hashlib.sha256,
    ).hexdigest()
    return f"{_TOKEN_VERSION}.{encoded_payload.decode('ascii')}.{signature}"


def reader_session_is_valid(
    token: str | None,
    session_secret: str,
    *,
    now: int | None = None,
    max_age_seconds: int = SESSION_TTL_SECONDS,
) -> bool:
    """Проверяет подпись и срок действия читательской сессии."""

    try:
        version, encoded_payload, supplied_signature = str(token or "").split(
            ".",
            2,
        )
        if version != _TOKEN_VERSION:
            return False

        expected_signature = hmac.new(
            str(session_secret).encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            supplied_signature,
            expected_signature,
        ):
            return False

        padding = "=" * (-len(encoded_payload) % 4)
        payload = base64.urlsafe_b64decode(
            f"{encoded_payload}{padding}".encode("ascii")
        ).decode("utf-8")
        issued_at_text, nonce = payload.split(":", 1)
        issued_at = int(issued_at_text)
        current_time = int(time.time() if now is None else now)

        if not nonce or issued_at > current_time + 300:
            return False

        return current_time - issued_at <= max_age_seconds
    except (TypeError, ValueError, UnicodeError):
        return False


def safe_next_path(raw_value: str | None) -> str:
    """Разрешает после входа только внутренний путь этого приложения."""

    candidate = str(raw_value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"

    return candidate
