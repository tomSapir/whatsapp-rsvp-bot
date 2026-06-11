"""Opt-in LLM evaluation — real OpenAI calls on ~10 Hebrew/English phrases (M9, optional).

Skipped by default (the suite must stay offline and deterministic). Run explicitly with::

    RUN_LLM_EVAL=1 OPENAI_API_KEY=sk-... pytest tests/test_llm_eval.py -v

Assertions are deliberately *loose*: the closed ``intent`` and the explicit fields the
phrase states. We're checking the prompt + schema steer the model correctly, not pinning
exact strings — dietary/note wording is the model's choice.
"""

from __future__ import annotations

import os

import pytest

from app.parser import Intent, OpenAIReplyParser

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RUN_LLM_EVAL") and os.environ.get("OPENAI_API_KEY")),
    reason="opt-in: set RUN_LLM_EVAL=1 and OPENAI_API_KEY to run against real OpenAI",
)


@pytest.fixture(scope="module")
def parser():
    return OpenAIReplyParser(api_key=os.environ.get("OPENAI_API_KEY"))


# (phrase, expected intent, expected attending, expected party_size)
PHRASES = [
    ("yes! we'll be there", Intent.rsvp_yes, True, None),
    ("Yes, 3 of us, one vegetarian", Intent.rsvp_yes, True, 3),
    ("sorry, we can't make it", Intent.rsvp_no, False, None),
    ("we'll be 4 people", Intent.provide_details, None, 4),
    ("actually make that 5, not 4", Intent.change, None, 5),
    ("is there parking at the venue?", Intent.question, None, None),
    ("כן, נשמח לבוא! נהיה 2", Intent.rsvp_yes, True, 2),
    ("מצטערים, לא נוכל להגיע", Intent.rsvp_no, False, None),
    ("נהיה 6, שניים צמחוניים", Intent.provide_details, None, 6),
    ("באיזו שעה זה מתחיל?", Intent.question, None, None),
]


@pytest.mark.parametrize("phrase, intent, attending, party_size", PHRASES)
def test_phrase_extraction(parser, phrase, intent, attending, party_size):
    parsed = parser.parse(phrase)

    assert parsed.intent is intent
    if attending is not None:
        assert parsed.attending is attending
    if party_size is not None:
        assert parsed.party_size == party_size
