"""One-shot flash messages for POST-redirect-GET.

Kept in their own short-lived signed cookie rather than the session, so the
session holds nothing but ``user``, ``csrf`` and ``login_at``. The cookie is
signed (a third party cannot plant messages) and read at most once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

FLASH_COOKIE = "dsw_flash"
FLASH_MAX_AGE = 120
MAX_MESSAGES = 5
MAX_MESSAGE_LEN = 400
Level = Literal["info", "success", "error"]
_LEVELS = frozenset({"info", "success", "error"})


@dataclass(frozen=True)
class Flash:
    level: str
    text: str


class FlashStore:
    def __init__(self, secret: str, *, secure: bool) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="dsw-flash")
        self._secure = secure

    def set(self, response: Response, messages: list[Flash]) -> None:
        payload = [[m.level, m.text[:MAX_MESSAGE_LEN]] for m in messages[:MAX_MESSAGES] if m.level in _LEVELS]
        if not payload:
            return
        response.set_cookie(
            FLASH_COOKIE,
            self._serializer.dumps(payload),
            max_age=FLASH_MAX_AGE,
            httponly=True,
            secure=self._secure,
            samesite="lax",
            path="/",
        )

    def read(self, request: Request) -> list[Flash]:
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw or len(raw) > 8192:
            return []
        try:
            data = self._serializer.loads(raw, max_age=FLASH_MAX_AGE)
        except BadSignature:
            return []
        if not isinstance(data, list):
            return []
        out: list[Flash] = []
        for item in data[:MAX_MESSAGES]:
            if isinstance(item, list) and len(item) == 2 and item[0] in _LEVELS and isinstance(item[1], str):
                out.append(Flash(level=item[0], text=item[1][:MAX_MESSAGE_LEN]))
        return out

    def clear(self, response: Response) -> None:
        response.delete_cookie(FLASH_COOKIE, path="/", secure=self._secure, httponly=True, samesite="lax")
