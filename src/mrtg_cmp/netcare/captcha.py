"""Multi-key Gemini Vision CAPTCHA solver with quota failover.

The TelkomCare login form guards access with a 3-character alphanumeric
CAPTCHA. Solving it consumes Gemini Vision quota, so the solver walks a
``(model, key)`` grid: on HTTP 429 (quota exhaustion) or a transient 5xx it
immediately moves to the next key/model instead of burning the remaining
quota of a dead key.

Failover is kept fast by two session-level rules. The request timeout is short
({@link DEFAULT_TIMEOUT_SECONDS}) so a hung model costs seconds, not a
half-minute of operator staring at a spinner. And a model that answers 503 or
404 is *unavailable* rather than *busy* — no key will fix it — so it is
blacklisted for the life of the session and skipped instantly on the next
CAPTCHA instead of paying the timeout again.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("mrtg_cmp.netcare.captcha")

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

CAPTCHA_PROMPT = (
    "Solve this text CAPTCHA. It is a 3-character alphanumeric string and may be "
    "case-sensitive. Output only the 3-character string; no explanation, markdown, "
    "punctuation, or newline."
)

#: Status codes that justify rotating to the next key/model immediately.
ROTATE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Status codes that mean the model itself is gone rather than momentarily
#: unavailable. No API key can rescue it, so the model is blacklisted for the
#: rest of the session and later solves skip it without paying a timeout.
MODEL_BLACKLIST_STATUS = frozenset({404, 503})

#: Per-request timeout. Deliberately short: the CAPTCHA is 3 characters the
#: portal will re-issue anyway, so waiting a long time for one model is worse
#: than falling back to the next one.
DEFAULT_TIMEOUT_SECONDS = 6

#: Upper bound on a caller-supplied timeout, so a misconfigured environment
#: cannot silently restore the old half-minute stall.
MAX_TIMEOUT_SECONDS = 6

#: Vision models that actually serve ``generateContent`` today. A name outside
#: this set is retired by the first 404, which costs a request per CAPTCHA, so
#: the shipped defaults and the documented examples stay inside it (FR-16.3).
#:
#: Order is failover priority, not alphabet. The two ``-flash-lite`` models
#: carry the 500 RPD / 15 RPM free-tier quota, so the solver reaches them first
#: and a routine login never escalates to a metered model. The full-size Flash
#: models follow as quality fallbacks; the 2.x names are last-resort retries
#: only.
VALID_GEMINI_VISION_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

_CAPTCHA_RE = re.compile(r"^[A-Za-z0-9]{3}$")
_EDGE_PUNCTUATION = "`*_'\"| \t\r\n.,:;!?"

#: Transport signature: (url, payload) -> (status_code, response_json)
Transport = Callable[[str, dict[str, Any]], "tuple[int, dict[str, Any]]"]


def normalize_captcha_text(raw: str) -> str:
    """Extract a 3-character alphanumeric CAPTCHA from free-form model output.

    The final whitespace-delimited segment is the only accepted answer, so
    chatty model replies ("The code is x9z") are read correctly while prose
    that merely happens to contain a 3-letter word is rejected.

    Returns an empty string when the model response cannot be interpreted.
    """

    if not raw:
        return ""
    segments = raw.split()
    if not segments:
        return ""
    candidate = segments[-1].strip(_EDGE_PUNCTUATION)
    return candidate if _CAPTCHA_RE.match(candidate) else ""


def extract_response_text(payload: dict[str, Any]) -> str:
    """Pull the first candidate text out of a Gemini response body."""

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, dict):
        return ""
    content = first.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        return ""
    head = parts[0]
    if not isinstance(head, dict):
        return ""
    text = head.get("text")
    return text.strip() if isinstance(text, str) else ""


def _urllib_transport(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Default transport: POST JSON to the Gemini endpoint via urllib."""

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=timeout_seconds
        ) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = b""
        with contextlib.suppress(Exception):  # body may already be consumed
            raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {}
        return exc.code, parsed
    try:
        parsed_body = json.loads(body)
    except ValueError:
        return 200, {}
    return 200, parsed_body if isinstance(parsed_body, dict) else {}


class GeminiCaptchaSolver:
    """Solve 3-character TelkomCare CAPTCHAs with rotating Gemini API keys."""

    def __init__(
        self,
        api_keys: list[str],
        models: list[str],
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        self.api_keys = [k.strip() for k in api_keys if k.strip()]
        self.models = [m.strip() for m in models if m.strip()]
        self.timeout_seconds = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
        self._transport: Transport = transport or self._default_transport
        self._next_key_index = 0
        self._blacklisted_models: set[str] = set()

    @property
    def blacklisted_models(self) -> frozenset[str]:
        """Models removed from this session's rotation by 404/503 responses."""

        return frozenset(self._blacklisted_models)

    @property
    def next_key_index(self) -> int:
        """Index of the API key the next solve round starts with."""

        return self._next_key_index

    @next_key_index.setter
    def next_key_index(self, value: int) -> None:
        if not self.api_keys:
            self._next_key_index = 0
            return
        self._next_key_index = int(value) % len(self.api_keys)

    def _default_transport(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return _urllib_transport(url, payload, self.timeout_seconds)

    def _key_order(self) -> list[int]:
        """Return key indices rotated to start at the current position."""

        total = len(self.api_keys)
        start = self._next_key_index % total
        return [(start + offset) % total for offset in range(total)]

    def _available_models(self) -> list[str]:
        """Return the configured models minus this session's blacklisted ones."""

        return [m for m in self.models if m not in self._blacklisted_models]

    def solve(self, image_bytes: bytes) -> str:
        """Return the solved 3-character CAPTCHA, or an empty string on failure."""

        if not image_bytes or not self.api_keys or not self.models:
            return ""

        encoded = base64.b64encode(image_bytes).decode("utf-8")
        payload: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": CAPTCHA_PROMPT},
                        {"inline_data": {"mime_type": "image/png", "data": encoded}},
                    ]
                }
            ]
        }
        key_order = self._key_order()
        models = self._available_models()
        if not models:
            logger.warning("Every Gemini model is blacklisted; skipping the CAPTCHA")
            return ""

        for model in models:
            for key_index in key_order:
                key = self.api_keys[key_index]
                status, body = self._request(model, key, payload)
                if status == 200:
                    code = normalize_captcha_text(extract_response_text(body))
                    if code:
                        return code
                    logger.warning("Gemini model %s returned unparseable CAPTCHA", model)
                elif status in MODEL_BLACKLIST_STATUS:
                    # The model is unavailable, not busy: no key can rescue it, so
                    # drop it for the session instead of walking the whole grid.
                    self._blacklisted_models.add(model)
                    logger.warning(
                        "Gemini model %s returned HTTP %s; blacklisted for this session",
                        model,
                        status,
                    )
                    break
                elif status in ROTATE_STATUS:
                    logger.warning("Gemini key %s failed with HTTP %s; rotating", key[:6], status)
                    self.next_key_index = key_index + 1
                else:
                    logger.warning("Gemini model %s returned HTTP %s", model, status)

        return ""

    def _request(
        self, model: str, key: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        url = GEMINI_ENDPOINT.format(model=model, key=key)
        try:
            return self._transport(url, payload)
        except (OSError, ValueError, TimeoutError) as exc:
            logger.warning("Gemini request failed: %s", exc)
            return 599, {}


__all__ = [
    "CAPTCHA_PROMPT",
    "DEFAULT_TIMEOUT_SECONDS",
    "GEMINI_ENDPOINT",
    "MAX_TIMEOUT_SECONDS",
    "MODEL_BLACKLIST_STATUS",
    "ROTATE_STATUS",
    "VALID_GEMINI_VISION_MODELS",
    "GeminiCaptchaSolver",
    "Transport",
    "extract_response_text",
    "normalize_captcha_text",
]
