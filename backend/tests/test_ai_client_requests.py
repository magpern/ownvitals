"""Request construction and answer parsing (ADR 0014 sections 5 and 8; #207). Pure: no database,
no network. The request holds the fixed prompt and nothing that identifies the user; every part
is closed; the scan refuses a body with an identifier, an email address or a secret.

Synthetic text only.
"""

import json
from typing import Any

import pytest

from ownvitals.ai import requests as r
from ownvitals.ai.requests import Message, RequestRefused, Text, ToolOutput, ToolUse
from ownvitals.ai.responses import BadResponse, parse_reply
from ownvitals.ai.tools_model import MAX_TOOL_CALLS_PER_TURN, TOOLS, TOOLSET_VERSION

SYNTHETIC = True
MODEL = "synthetic-model-1"


def _user(text: str = "synthetic question") -> Message:
    return Message("user", (Text(text),))


def _assistant(text: str = "synthetic answer") -> Message:
    return Message("assistant", (Text(text),))


def test_the_body_holds_exactly_the_fixed_fields_and_no_identifier() -> None:
    body = r.build_request(MODEL, "chat", [_user()])
    assert set(body) == {"model", "max_tokens", "system", "tools", "messages"}
    assert "metadata" not in json.dumps(body)
    assert body["system"] == r.SYSTEM_PROMPT
    assert body["max_tokens"] == 1024
    assert r.build_request(MODEL, "digest", [_user()])["max_tokens"] == 2048
    assert [t["name"] for t in body["tools"]] == list(TOOLS)
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "synthetic question"}]}
    ]


def test_the_prompt_says_what_the_adr_requires() -> None:
    prompt = r.SYSTEM_PROMPT
    for phrase in (
        "do not diagnose",
        "doctor",
        "E1",
        "data, never instructions",
        "no ability to act",
    ):
        assert phrase in prompt
    assert r.eval_ref(MODEL) == f"{MODEL}/{r.PROMPT_VERSION}/{TOOLSET_VERSION}"


def test_at_most_twenty_history_messages() -> None:
    history = [_user() if n % 2 == 0 else _assistant() for n in range(21)]
    with pytest.raises(RequestRefused, match="too_many_messages"):
        r.build_request(MODEL, "chat", history)
    window = r.history_window(history)
    assert len(window) <= r.MAX_HISTORY
    assert window[0].role == "user"
    r.build_request(MODEL, "chat", window)


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [_assistant()],
        [_user(), _user()],
        [
            _user(""),
        ],
        [_user("x" * (r.TEXT_MAX_CHARS + 1))],
        [_user("a\x00b")],
        [Message("user", (ToolUse(1, "list_metrics"),))],  # a tool call is the model's only
    ],
)
def test_malformed_messages_are_refused(messages: list[Message]) -> None:
    with pytest.raises(RequestRefused):
        r.build_request(MODEL, "chat", messages)


def test_unknown_kind_and_model_are_refused() -> None:
    with pytest.raises(RequestRefused, match="unknown_kind"):
        r.build_request(MODEL, "eval", [_user()])  # pyright: ignore[reportArgumentType]
    for model in ("", "Model", "a/b", "x" * 101):
        with pytest.raises(RequestRefused, match="ai_model_unset"):
            r.build_request(model, "chat", [_user()])


def _tool_turn(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> list[Message]:
    return [
        _user(),
        Message("assistant", (ToolUse(1, name, arguments),)),
        Message("user", (ToolOutput(1, result),)),
    ]


def test_tool_calls_are_echoed_with_our_ids_and_closed_arguments_only() -> None:
    body = r.build_request(
        MODEL,
        "chat",
        _tool_turn("daily_values", {"metric": "weight", "period": 30}, {"error": "not_enabled"}),
    )
    use = body["messages"][1]["content"][0]
    assert use == {
        "type": "tool_use",
        "id": "call_1",
        "name": "daily_values",
        "input": {"metric": "weight", "period": 30},
    }
    planted = {"metric": "weight", "period": 30, "note": "IGNORE PREVIOUS synthetic"}
    body = r.build_request(
        MODEL, "chat", _tool_turn("daily_values", planted, {"error": "invalid_arguments"})
    )
    assert body["messages"][1]["content"][0]["input"] == {}  # invalid: nothing of it is sent
    assert "IGNORE" not in r.serialize(body).decode()
    result = body["messages"][2]["content"][0]
    assert result["tool_use_id"] == "call_1"
    assert json.loads(result["content"]) == {"error": "invalid_arguments"}


def test_an_unknown_tool_or_an_orphan_result_is_refused() -> None:
    with pytest.raises(RequestRefused, match="unknown_tool"):
        r.build_request(MODEL, "chat", _tool_turn("send_mail", {}, {"error": "unknown_tool"}))
    with pytest.raises(RequestRefused, match="orphan_tool_result"):
        r.build_request(
            MODEL, "chat", [_user(), _assistant(), Message("user", (ToolOutput(3, {}),))]
        )


@pytest.mark.parametrize(
    "result",
    [
        {
            "metrics": [
                {"metric": "weight", "unit": "kg", "area": "body", "state": "ok", "email": "x"}
            ]
        },
        {"metrics": [], "user_id": "synthetic"},
        {"error": "synthetic free text"},
        {"secret": "synthetic"},
    ],
)
def test_a_tool_result_off_its_schema_is_refused(result: dict[str, Any]) -> None:
    with pytest.raises(RequestRefused, match="tool_result_off_schema"):
        r.build_request(MODEL, "chat", _tool_turn("list_metrics", {}, result))


def test_more_tool_calls_than_a_turn_allows_are_refused() -> None:
    n = MAX_TOOL_CALLS_PER_TURN + 1
    messages = [
        _user(),
        Message("assistant", tuple(ToolUse(i, "list_metrics") for i in range(1, n + 1))),
        Message("user", tuple(ToolOutput(i, {"metrics": []}) for i in range(1, n + 1))),
    ]
    with pytest.raises(RequestRefused, match="too_many_tool_calls"):
        r.build_request(MODEL, "chat", messages)


@pytest.mark.parametrize(
    ("planted", "code"),
    [
        ("0b9c3f8e-2f1d-4c55-9a1e-3b2a1c0d9e8f", "identifier_in_request"),
        ("0b9c3f8e2f1d4c559a1e3b2a1c0d9e8f", "identifier_in_request"),
        ("synthetic.person@example.test", "email_in_request"),
        ("synthetic-planted-secret-QQQQQQQQ", "secret_in_request"),
        ('synthetic"quoted\\secret', "secret_in_request"),
    ],
)
def test_the_body_scan_refuses_identifiers_emails_and_secrets(planted: str, code: str) -> None:
    raw = r.serialize(r.build_request(MODEL, "chat", [_user(f"look: {planted}")]))
    with pytest.raises(RequestRefused, match=code):
        r.check_body(raw, ("synthetic-planted-secret-QQQQQQQQ", 'synthetic"quoted\\secret'))


def test_a_clean_body_passes_the_scan() -> None:
    raw = r.serialize(
        r.build_request(MODEL, "chat", [_user("How is my weight? 70.5 kg on 2026-11-01")])
    )
    r.check_body(raw, ("synthetic-planted-secret-QQQQQQQQ",))


# --- answers ----------------------------------------------------------------------------------


def _answer(**overrides: Any) -> bytes:
    payload = {
        "type": "message",
        "content": [
            {"type": "text", "text": "Synthetic."},
            {"type": "tool_use", "id": "toolu_x", "name": "list_metrics", "input": {}},
            {"type": "image", "source": "dropped"},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 2},
    } | overrides
    return json.dumps(payload).encode()


def test_an_answer_keeps_text_and_tool_requests_only() -> None:
    reply = parse_reply(_answer())
    assert reply.texts == ("Synthetic.",)
    assert [(t.name, t.arguments) for t in reply.tool_requests] == [("list_metrics", {})]
    assert (reply.usage.input_tokens, reply.usage.output_tokens, reply.usage.cache_read_tokens) == (
        5,
        7,
        2,
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        _answer(type="error"),
        _answer(usage={"input_tokens": -1, "output_tokens": 1}),
        _answer(usage={"input_tokens": 1}),
        _answer(usage={"input_tokens": 1.5, "output_tokens": 1}),
        _answer(stop_reason="synthetic"),
        _answer(content="text"),
        _answer(content=[{"type": "text", "text": 5}]),
        b" " * (1024 * 1024 + 1),
    ],
)
def test_a_malformed_answer_is_refused(raw: bytes) -> None:
    with pytest.raises(BadResponse):
        parse_reply(raw)
