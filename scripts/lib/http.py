"""HTTP client with retries and exponential backoff on 429 / 5xx / network errors.

Error messages never contain query strings or API keys (they end up in logs, status.json and Discord).
"""

import re
import time
from typing import Any

import requests

USER_AGENT = "onchain-dashboard-fetcher/1.0"
RETRY_STATUSES = {429, 500, 502, 503, 504}

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})


class HttpError(RuntimeError):
    pass


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: Any = None,
    timeout: float = 30,
    max_attempts: int = 5,
    base_delay: float = 2.0,
    allow_404: bool = False,
) -> Any:
    """Return the decoded JSON body. With allow_404=True, a 404 returns None instead of raising."""
    last_error: str = ""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _session.request(method, url, params=params, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            last_error = redact(f"{type(exc).__name__}: {exc}")
        else:
            if resp.status_code in RETRY_STATUSES:
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(int(retry_after), 60))
                    continue
            elif resp.status_code == 404 and allow_404:
                return None
            elif resp.status_code >= 400:
                raise HttpError(redact(f"{method} {_safe(url)} -> HTTP {resp.status_code}: {resp.text[:300]}"))
            else:
                return resp.json()
        if attempt < max_attempts:
            time.sleep(base_delay * (2 ** (attempt - 1)))
    raise HttpError(f"{method} {_safe(url)} failed after {max_attempts} attempts ({last_error})")


def get_json(url: str, **kwargs: Any) -> Any:
    return request_json("GET", url, **kwargs)


def post_json(url: str, body: Any, **kwargs: Any) -> Any:
    return request_json("POST", url, json_body=body, **kwargs)


def _safe(url: str) -> str:
    """Drop the query string from error messages so API keys never end up in logs."""
    return url.split("?", 1)[0]


_SECRET_RE = re.compile(r"(api[-_]?key=)[^&\s'\")]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Mask any api key / api-key query parameter left in a message."""
    return _SECRET_RE.sub(r"\1***", text)
