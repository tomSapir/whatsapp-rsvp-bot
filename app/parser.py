"""Reply parsing — OpenAI structured extraction behind an injectable seam (PLAN §6 · Q5).

A free-text guest reply ("yes!! we're 3, one vegan", "מצטערים, לא נוכל") is turned into a
:class:`ParsedReply` via OpenAI **tool calling**: the model is forced to call one function
whose JSON-Schema arguments are the structure we want, instead of free-associating prose.
``intent`` is a **closed enum** — the conversation layer keys its control flow off it, never
off ``confidence`` (self-reported confidence is poorly calibrated, so it is logged only).

The seam mirrors :mod:`app.whatsapp`: :class:`ReplyParser` is the contract, the real
:class:`OpenAIReplyParser` is injected in production, and :class:`StubReplyParser` (canned
replies, no network) in tests. ``null`` fields mean "the guest didn't say" — the
conversation layer never lets a ``null`` overwrite a known value.

Anything the model returns that doesn't validate (unknown intent, malformed JSON, wrong
types) raises :class:`ParseError`; the conversation layer reacts by touching nothing and
notifying the Host (PLAN §6 · Q5/Q7).
"""

from __future__ import annotations

import enum
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class Intent(enum.Enum):
    """The closed set of things a guest message can mean (PLAN §6)."""

    rsvp_yes = "rsvp_yes"
    rsvp_no = "rsvp_no"
    provide_details = "provide_details"
    change = "change"
    question = "question"
    other = "other"


class ParseError(ValueError):
    """The model's output could not be validated into a :class:`ParsedReply`."""


@dataclass(frozen=True)
class ParsedReply:
    """Structured extraction of one guest message; ``None`` = "the guest didn't say"."""

    intent: Intent
    attending: bool | None = None
    party_size: int | None = None
    dietary: str | None = None
    note: str | None = None
    confidence: float | None = None


class ReplyParser(ABC):
    """The parsing contract; the conversation layer depends on this, never a concrete class."""

    @abstractmethod
    def parse(self, text: str) -> ParsedReply:
        """Extract structure from one inbound free-text message (may raise ParseError)."""


_SYSTEM_PROMPT = (
    "You extract RSVP information from a single WhatsApp message sent by a wedding guest. "
    "Messages may be in Hebrew or English. Call the record_rsvp_reply function exactly once. "
    "Rules: pick the single best intent; set a field ONLY when the guest stated it "
    "explicitly — otherwise leave it null; never guess. Set attending only on an "
    "unambiguous yes/no statement about coming. party_size is the total number of people "
    "coming, including the guest; if the guest does not state a number, party_size MUST be "
    "null — never assume 1 or infer a count from a bare yes. confidence is your own 0-1 "
    "estimate of the extraction."
)

_TOOL_NAME = "record_rsvp_reply"

_TOOL = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": "Record the structured RSVP information extracted from the message.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [member.value for member in Intent],
                    "description": (
                        "rsvp_yes: clear yes · rsvp_no: clear no · provide_details: party "
                        "size/dietary/note without a clear yes-no · change: changes a "
                        "previous answer · question: asks something · other: anything else"
                    ),
                },
                "attending": {"type": ["boolean", "null"]},
                "party_size": {"type": ["integer", "null"]},
                "dietary": {"type": ["string", "null"]},
                "note": {"type": ["string", "null"]},
                "confidence": {"type": ["number", "null"]},
            },
            "required": ["intent"],
        },
    },
}


def parsed_reply_from_args(args: dict[str, Any]) -> ParsedReply:
    """Validate raw tool-call arguments into a :class:`ParsedReply` (raises ParseError).

    Strict on types: ``intent`` must be one of the closed enum values; ``party_size`` must
    be an actual int (``bool`` is explicitly excluded — it subclasses ``int`` in Python).
    """
    try:
        intent = Intent(args["intent"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ParseError(f"invalid intent: {args.get('intent')!r}") from exc

    attending = args.get("attending")
    if attending is not None and not isinstance(attending, bool):
        raise ParseError(f"attending must be a boolean or null, got {attending!r}")

    party_size = args.get("party_size")
    if party_size is not None and (isinstance(party_size, bool) or not isinstance(party_size, int)):
        raise ParseError(f"party_size must be an integer or null, got {party_size!r}")

    dietary = args.get("dietary")
    if dietary is not None and not isinstance(dietary, str):
        raise ParseError(f"dietary must be a string or null, got {dietary!r}")

    note = args.get("note")
    if note is not None and not isinstance(note, str):
        raise ParseError(f"note must be a string or null, got {note!r}")

    confidence = args.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ParseError(f"confidence must be a number or null, got {confidence!r}")
        confidence = float(confidence)

    return ParsedReply(
        intent=intent,
        attending=attending,
        party_size=party_size,
        dietary=dietary,
        note=note,
        confidence=confidence,
    )


class OpenAIReplyParser(ReplyParser):
    """Real parser — one forced tool call to OpenAI per guest message.

    ``client`` is injectable (anything exposing ``chat.completions.create``) so tests can
    pass a canned fake; by default the official SDK client is built from the api key.
    """

    def __init__(self, api_key: str | None = None, model: str = "gpt-4o-mini", *, client: Any = None) -> None:
        if client is None:
            from openai import OpenAI  # lazy: only the real path needs the SDK client

            client = OpenAI(api_key=api_key)
        self._client = client
        self._model = model

    def parse(self, text: str) -> ParsedReply:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            tools=[_TOOL],
            # Force the tool call: the model may not answer in prose.
            tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
            # Extraction, not generation: pin to 0 so the same reply parses the same way
            # and the model doesn't "fill in" fields (e.g. a party_size) the guest never gave.
            temperature=0,
        )
        message = response.choices[0].message
        if not message.tool_calls:
            raise ParseError("model returned no tool call")
        try:
            args = json.loads(message.tool_calls[0].function.arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ParseError(f"tool arguments are not valid JSON: {exc}") from exc
        if not isinstance(args, dict):
            raise ParseError(f"tool arguments must be an object, got {args!r}")
        return parsed_reply_from_args(args)


class StubReplyParser(ReplyParser):
    """Test double — canned :class:`ParsedReply` per exact text, no network.

    Unmapped text raises :class:`ParseError` (unless a ``default`` is given), which doubles
    as a way to exercise the conversation layer's parse-failure path.
    """

    def __init__(
        self,
        replies: dict[str, ParsedReply] | None = None,
        default: ParsedReply | None = None,
    ) -> None:
        self._replies = replies or {}
        self._default = default

    def parse(self, text: str) -> ParsedReply:
        reply = self._replies.get(text, self._default)
        if reply is None:
            raise ParseError(f"no stubbed reply for {text!r}")
        return reply


def build_reply_parser(settings: Any = None) -> ReplyParser:
    """Construct the real :class:`OpenAIReplyParser` from app settings (M9 wiring)."""
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return OpenAIReplyParser(api_key=settings.openai_api_key, model=settings.openai_model)
