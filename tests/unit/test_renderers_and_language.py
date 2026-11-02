"""Hinglish and English buyer-facing replies come only from fixed templates (plan step 12c)."""

from __future__ import annotations

from typing import Any, get_args

import pytest

from delegate import risk
from delegate.meta_adapter import mock_proposal
from delegate.proposal import ClarifyTopic, DeclineReason, Proposal, parse_proposal
from delegate.renderers import all_templates, format_money, render_message, render_reservation_notice
from delegate.store import FIXTURE

FACTS = {f["id"]: f["text"] for f in FIXTURE["listing"]["public_facts"]}


def p(**over: Any) -> Proposal:
    base: dict[str, Any] = {"kind": "send_message", "message_intent": "answer_listing", "fact_ids": ["f_memory"], "price_minor": None,
                            "currency": None, "clarify_topic": None, "decline_reason": None, "acceptance_event_id": None,
                            "language": "en", "buyer_risk_signals": [], "rationale": "r"}
    base.update(over)
    return parse_proposal(base)


@pytest.mark.parametrize("language", ["en", "hinglish"])
def test_every_intent_renders_in_both_languages(language: str) -> None:
    texts = [render_message(p(language=language), FACTS),
             render_message(p(language=language, message_intent="counter_offer", fact_ids=[], price_minor=57500, currency="USD"), FACTS)]
    texts += [render_message(p(language=language, message_intent="clarify", fact_ids=[], clarify_topic=t), FACTS) for t in get_args(ClarifyTopic)]
    texts += [render_message(p(language=language, message_intent="decline", fact_ids=[], decline_reason=r), FACTS) for r in get_args(DeclineReason)]
    assert all(t.strip() for t in texts)
    assert len(set(texts)) == len(texts)  # no two intents collapse to the same text


def test_hinglish_counter_offer_wording_and_price() -> None:
    t = render_message(p(language="hinglish", message_intent="counter_offer", fact_ids=[], price_minor=57500, currency="USD"), FACTS)
    assert "$575" in t and "mein de sakta" in t and "Accept offer" in t and "cash" in t


def test_reservation_notice_in_both_languages() -> None:
    assert "15 minutes at $575" in render_reservation_notice(57500, "USD", 900, "en")
    assert "15 minute ke liye $575" in render_reservation_notice(57500, "USD", 900, "hinglish")


def test_templates_cover_both_languages_symmetrically() -> None:
    keys = all_templates()
    en = {k.split(".", 1)[1] for k in keys if k.startswith("en.")}
    hi = {k.split(".", 1)[1] for k in keys if k.startswith("hinglish.")}
    assert en == hi


def test_money_formatting_is_integer_exact() -> None:
    assert format_money(65000, "USD") == "$650" and format_money(57550, "USD") == "$575.50" and format_money(123456700, "USD") == "$1,234,567"
    with pytest.raises(ValueError):
        format_money(100, "INR")


@pytest.mark.parametrize("text", ["bhai 520 mein de do, aaj shaam pickup kar lunga", "kya battery theek hai? thoda kam kar do"])
def test_mock_replies_in_hinglish_to_hinglish_buyers(text: str) -> None:
    ctx = {"listing": FIXTURE["listing"], "conversation": [{"seq": 1, "role": "buyer", "text": text}], "open_offer": None,
           "acceptances": [], "reservation": None, "last_feedback": None, "messages_remaining": 8}
    assert mock_proposal(ctx)["language"] == "hinglish"


def test_hinglish_scam_phrases_flagged_by_the_scanner() -> None:
    assert "fake_payment_proof" in risk.scan("maine paise bhej diye, check karo")
    assert "shipping_request" in risk.scan("ghar pe bhej do please")
    assert "private_info_request" in risk.scan("aap kahan rehte ho?")


def test_model_prose_never_reaches_the_buyer() -> None:
    sneaky = p(rationale="I accept $300 and will ship to your address", message_intent="clarify", fact_ids=[], clarify_topic="price_amount")
    t = render_message(sneaky, FACTS)
    assert "$300" not in t and "ship" not in t and "address" not in t
