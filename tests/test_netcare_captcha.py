"""Unit tests for the multi-key Gemini CAPTCHA solver with failover rotation."""

from __future__ import annotations

import base64

import pytest

from mrtg_cmp.netcare.captcha import (
    CAPTCHA_PROMPT,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    GeminiCaptchaSolver,
    normalize_captcha_text,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n fake captcha bytes"

Response = tuple[int, dict]


def _text_response(text: str) -> Response:
    return 200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class RecordingTransport:
    """Transport double capturing every request and replaying queued responses."""

    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> Response:
        self.calls.append((url, payload))
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError("transport called more times than responses were queued")
        outcome = self.responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _solver(
    transport: RecordingTransport, keys: list[str], models: list[str]
) -> GeminiCaptchaSolver:
    return GeminiCaptchaSolver(api_keys=keys, models=models, transport=transport)


# --- Response normalisation ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc", "abc"),
        ("  AbC  ", "AbC"),
        ("`a1b`", "a1b"),
        ("The code is **x9z**", "x9z"),
        ("answer: Q7w\n", "Q7w"),
        ('"K2m"', "K2m"),
    ],
)
def test_normalize_extracts_three_character_code(raw: str, expected: str) -> None:
    assert normalize_captcha_text(raw) == expected


@pytest.mark.parametrize("raw", ["", "ab", "abcd", "12", "toolongvalue", "no code here"])
def test_normalize_rejects_non_three_character_output(raw: str) -> None:
    assert normalize_captcha_text(raw) == ""


# --- Happy path ------------------------------------------------------------


def test_solve_returns_code_from_first_key() -> None:
    transport = RecordingTransport([_text_response("aBc")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == "aBc"
    assert len(transport.calls) == 1
    assert "key=key-1" in transport.calls[0][0]
    assert "gemini-2.5-flash" in transport.calls[0][0]


def test_solve_sends_image_as_base64_inline_data() -> None:
    transport = RecordingTransport([_text_response("aBc")])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash"])
    solver.solve(PNG_BYTES)
    payload = transport.calls[0][1]
    parts = payload["contents"][0]["parts"]
    assert parts[0]["text"] == CAPTCHA_PROMPT
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == PNG_BYTES


def test_solve_without_api_keys_returns_empty() -> None:
    transport = RecordingTransport([])
    solver = _solver(transport, [], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == ""
    assert transport.calls == []


# --- Key rotation on 429 ---------------------------------------------------


def test_solve_rotates_to_next_key_on_quota_exhaustion() -> None:
    transport = RecordingTransport([(429, {"error": "quota"}), _text_response("z9x")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == "z9x"
    assert "key=key-1" in transport.calls[0][0]
    assert "key=key-2" in transport.calls[1][0]


def test_solve_rotates_across_all_keys_when_all_are_exhausted() -> None:
    transport = RecordingTransport(
        [(429, {}), (429, {}), _text_response("q1w")]
    )
    solver = _solver(transport, ["key-1", "key-2", "key-3"], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == "q1w"
    assert "key=key-3" in transport.calls[2][0]


def test_solve_returns_empty_when_every_key_is_exhausted() -> None:
    transport = RecordingTransport([(429, {}), (429, {})])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == ""


def test_next_key_index_advances_after_rotation() -> None:
    transport = RecordingTransport([(429, {}), _text_response("aaa")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])
    solver.solve(PNG_BYTES)
    assert solver.next_key_index == 1


def test_next_key_index_stays_on_success() -> None:
    transport = RecordingTransport([_text_response("aaa")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])
    solver.solve(PNG_BYTES)
    assert solver.next_key_index == 0


# --- Model fallback --------------------------------------------------------


def test_solve_falls_back_to_next_model_on_bad_output() -> None:
    transport = RecordingTransport([_text_response("not-three"), _text_response("d4f")])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-2.0-flash"])
    assert solver.solve(PNG_BYTES) == "d4f"
    assert "gemini-2.5-flash" in transport.calls[0][0]
    assert "gemini-2.0-flash" in transport.calls[1][0]


def test_solve_falls_back_to_next_model_on_server_error() -> None:
    transport = RecordingTransport([(503, {}), _text_response("f6y")])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-1.5-flash"])
    assert solver.solve(PNG_BYTES) == "f6y"
    assert len(transport.calls) == 2


def test_solve_survives_transport_error() -> None:
    transport = RecordingTransport([OSError("connection reset"), _text_response("g7h")])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-1.5-flash"])
    assert solver.solve(PNG_BYTES) == "g7h"


def test_solve_returns_empty_when_all_models_fail() -> None:
    transport = RecordingTransport([OSError("down"), (500, {}), OSError("down again")])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-1.5-flash"])
    assert solver.solve(PNG_BYTES) == ""


def test_solve_handles_malformed_success_payload() -> None:
    transport = RecordingTransport([(200, {"candidates": []})])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash"])
    assert solver.solve(PNG_BYTES) == ""


def test_solve_rejects_empty_image_bytes() -> None:
    transport = RecordingTransport([])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash"])
    assert solver.solve(b"") == ""
    assert transport.calls == []


def test_solve_visits_key_then_model_grid_in_order() -> None:
    transport = RecordingTransport(
        [(429, {}), (429, {}), (429, {}), _text_response("h8i")]
    )
    solver = _solver(
        transport,
        ["key-1", "key-2"],
        ["gemini-2.5-flash", "gemini-1.5-flash"],
    )
    assert solver.solve(PNG_BYTES) == "h8i"
    visited = [
        url.split("models/")[1].replace(":generateContent", "")
        for url, _ in transport.calls
    ]
    assert visited == [
        "gemini-2.5-flash?key=key-1",
        "gemini-2.5-flash?key=key-2",
        "gemini-1.5-flash?key=key-1",
        "gemini-1.5-flash?key=key-2",
    ]


# --- Fast failover: short timeout and session model blacklisting -----------


def test_default_timeout_is_short_enough_to_fail_fast() -> None:
    """A dead model must cost seconds, not the old half-minute stall."""

    assert DEFAULT_TIMEOUT_SECONDS == 6
    assert MAX_TIMEOUT_SECONDS == 6
    assert GeminiCaptchaSolver(api_keys=["k"], models=["m"]).timeout_seconds == 6


def test_a_misconfigured_timeout_cannot_reintroduce_the_long_stall() -> None:
    solver = GeminiCaptchaSolver(api_keys=["k"], models=["m"], timeout_seconds=120)
    assert solver.timeout_seconds == MAX_TIMEOUT_SECONDS


def test_a_nonsense_timeout_degrades_to_one_second() -> None:
    solver = GeminiCaptchaSolver(api_keys=["k"], models=["m"], timeout_seconds=0)
    assert solver.timeout_seconds == 1


@pytest.mark.parametrize("status", [404, 503])
def test_a_503_or_404_model_is_blacklisted_for_the_session(status: int) -> None:
    """An unavailable model is dropped outright: no key is even attempted."""

    transport = RecordingTransport([(status, {}), _text_response("m3q")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash", "gemini-3.6-flash"])

    assert solver.solve(PNG_BYTES) == "m3q"
    # The dead model is tried once, not once per key.
    assert len(transport.calls) == 2
    assert "gemini-2.5-flash" in transport.calls[0][0]
    assert "key-2" not in transport.calls[0][0]
    assert status in (404, 503)
    assert "gemini-2.5-flash" in solver.blacklisted_models


def test_a_blacklisted_model_is_skipped_on_the_next_solve() -> None:
    """The point of the breaker: the second CAPTCHA never touches the dead model."""

    transport = RecordingTransport(
        [(503, {}), _text_response("abc"), _text_response("xyz")]
    )
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-3.6-flash"])

    assert solver.solve(PNG_BYTES) == "abc"
    calls_after_first = len(transport.calls)

    assert solver.solve(PNG_BYTES) == "xyz"
    assert len(transport.calls) == calls_after_first + 1
    assert "gemini-3.6-flash" in transport.calls[-1][0]


def test_a_quota_error_does_not_blacklist_the_model() -> None:
    """429 is a key problem, not a model problem: the model stays in rotation."""

    transport = RecordingTransport([(429, {}), _text_response("p4r")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])

    assert solver.solve(PNG_BYTES) == "p4r"
    assert solver.blacklisted_models == frozenset()


def test_solving_returns_immediately_when_every_model_is_blacklisted() -> None:
    """No transport call at all: there is nothing left worth trying."""

    transport = RecordingTransport([(503, {}), (404, {})])
    solver = _solver(transport, ["key-1"], ["gemini-2.5-flash", "gemini-2.0-flash"])

    assert solver.solve(PNG_BYTES) == ""
    assert len(transport.calls) == 2
    assert solver.solve(PNG_BYTES) == ""
    assert len(transport.calls) == 2
    assert solver.blacklisted_models == frozenset({"gemini-2.5-flash", "gemini-2.0-flash"})


def test_a_server_error_without_a_blacklist_signal_still_rotates_the_key() -> None:
    """500 stays a transient key-level fault, so both keys are tried."""

    transport = RecordingTransport([(500, {}), _text_response("n2b")])
    solver = _solver(transport, ["key-1", "key-2"], ["gemini-2.5-flash"])

    assert solver.solve(PNG_BYTES) == "n2b"
    assert "key-2" in transport.calls[1][0]
    assert solver.blacklisted_models == frozenset()
