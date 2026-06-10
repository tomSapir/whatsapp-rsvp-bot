"""M5 parser tests: argument validation, the stub, and the OpenAI path with a fake client.

No network anywhere — the "OpenAI" in these tests is a tiny canned object satisfying the
``chat.completions.create`` shape the real SDK client exposes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.parser import (
    Intent,
    OpenAIReplyParser,
    ParsedReply,
    ParseError,
    StubReplyParser,
    parsed_reply_from_args,
)


# --- parsed_reply_from_args — validation of raw tool-call arguments ------------------------


def test_full_valid_args():
    parsed = parsed_reply_from_args(
        {
            "intent": "rsvp_yes",
            "attending": True,
            "party_size": 3,
            "dietary": "one vegan",
            "note": "so excited!",
            "confidence": 0.93,
        }
    )
    assert parsed == ParsedReply(
        intent=Intent.rsvp_yes,
        attending=True,
        party_size=3,
        dietary="one vegan",
        note="so excited!",
        confidence=0.93,
    )


def test_minimal_args_default_to_none():
    parsed = parsed_reply_from_args({"intent": "question"})
    assert parsed.intent is Intent.question
    assert parsed.attending is None
    assert parsed.party_size is None


@pytest.mark.parametrize(
    "args",
    [
        {},  # intent missing
        {"intent": "maybe"},  # not in the closed enum
        {"intent": None},
        {"intent": "rsvp_yes", "attending": "yes"},  # wrong type
        {"intent": "rsvp_yes", "party_size": "three"},
        {"intent": "rsvp_yes", "party_size": True},  # bool is not a head-count
        {"intent": "rsvp_yes", "dietary": 5},
        {"intent": "rsvp_yes", "confidence": "high"},
    ],
)
def test_invalid_args_rejected(args):
    with pytest.raises(ParseError):
        parsed_reply_from_args(args)


# --- OpenAIReplyParser — driven by a fake SDK client ----------------------------------------


def _fake_client(arguments: str | None, tool_calls: bool = True):
    """A stand-in exposing ``chat.completions.create`` and recording its kwargs."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=arguments))]
            if tool_calls
            else None
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, calls


def test_openai_parser_happy_path():
    client, calls = _fake_client(json.dumps({"intent": "rsvp_no", "attending": False}))
    parser = OpenAIReplyParser(client=client, model="test-model")

    parsed = parser.parse("מצטערים, לא נוכל להגיע")

    assert parsed.intent is Intent.rsvp_no
    assert parsed.attending is False
    (call,) = calls
    assert call["model"] == "test-model"
    assert call["tool_choice"]["function"]["name"] == "record_rsvp_reply"  # forced call
    assert call["messages"][-1]["content"] == "מצטערים, לא נוכל להגיע"


def test_openai_parser_rejects_malformed_json():
    client, _ = _fake_client("{not json")
    with pytest.raises(ParseError):
        OpenAIReplyParser(client=client).parse("yes")


def test_openai_parser_rejects_missing_tool_call():
    client, _ = _fake_client(None, tool_calls=False)
    with pytest.raises(ParseError):
        OpenAIReplyParser(client=client).parse("yes")


def test_openai_parser_rejects_non_object_arguments():
    client, _ = _fake_client(json.dumps(["rsvp_yes"]))
    with pytest.raises(ParseError):
        OpenAIReplyParser(client=client).parse("yes")


# --- StubReplyParser -------------------------------------------------------------------------


def test_stub_returns_mapped_reply():
    stub = StubReplyParser({"yes": ParsedReply(intent=Intent.rsvp_yes, attending=True)})
    assert stub.parse("yes").intent is Intent.rsvp_yes


def test_stub_raises_for_unmapped_text():
    with pytest.raises(ParseError):
        StubReplyParser({}).parse("???")


def test_stub_default_used_for_unmapped_text():
    stub = StubReplyParser({}, default=ParsedReply(intent=Intent.other))
    assert stub.parse("anything").intent is Intent.other
