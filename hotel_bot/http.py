"""Небольшой JSON-клиент без повторов изменяющих запросов."""

import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class ServiceError(Exception):
    def __init__(self, message, *, status=None, description=""):
        super().__init__(message)
        self.status = status
        self.description = description


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload=None, *, headers=None, timeout=30, photo_bytes=None):
    content_type = "application/json"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if photo_bytes is not None:
        boundary = "sosny-" + uuid.uuid4().hex
        parts = []
        for key, value in (payload or {}).items():
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="room.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode(),
                  photo_bytes, f'\r\n--{boundary}--\r\n'.encode()]
        body = b"".join(parts)
        content_type = "multipart/form-data; boundary=" + boundary
    request = Request(url, data=body, headers={"Content-Type": content_type, **(headers or {})})
    try:
        # Redirects must not forward credentials to another host.
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ServiceError("Сервис вернул слишком большой ответ")
            return json.loads(raw)
    except HTTPError as exc:
        description = ""
        try:
            error = json.loads(exc.read(10_000))
            if isinstance(error, dict):
                description = str(error.get("description", ""))
        except (ValueError, OSError):
            pass
        # Keep server details for classification, never put them in logs (URLs may contain tokens).
        raise ServiceError(f"Сервис вернул HTTP {exc.code}", status=exc.code, description=description) from None
    except (URLError, TimeoutError, OSError, ValueError):
        # URLs may contain the Telegram token; never print the underlying exception.
        raise ServiceError("Сервис недоступен или вернул некорректный JSON") from None
