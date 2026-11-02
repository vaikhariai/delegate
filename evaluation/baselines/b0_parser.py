"""Baseline B0: deterministic intent/offer parser for Delegate (no model).

`propose(context)` takes the POLICY Appendix B public context and returns one
POLICY section 2 proposal. It sees exactly what the model sees: the public
listing, the conversation, the open offer, acceptances, reservation state,
the last gate feedback and the remaining message budget. It never sees the
private floor, address, phone or seller notes, so it relies on the server gate
(and `last_feedback`) to keep counters inside bounds, exactly as the model must.

Decision order (first match wins)
---------------------------------
1. Gate feedback that ends the turn (BUDGET_EXHAUSTED, MANDATE_*, STALE_REVISION,
   ACTION_EXPIRED) -> no_action. ITEM_UNAVAILABLE -> decline unsupported_request.
2. A buyer acceptance of the current open offer, no reservation yet, no
   ACCEPTANCE_*/SCAM_RISK feedback and no high-severity risk signal anywhere in
   the conversation -> reserve_item at that offer's price with that acceptance.
   With a high signal -> decline with the matching scam reason instead.
3. Nothing new from the buyer since the last seller message -> no_action.
4. Last buyer message, risk first (POLICY section 6 vocabulary):
   QR / collect-request to receive money -> decline qr_receive_scam;
   "already paid" / screenshot / proof -> decline fake_payment_proof;
   owner/approval/ignore-rules/army/police/officer claims -> decline authority_claim;
   address / phone / email / seller's minimum or lowest price -> decline private_info_request;
   ship / delivery / courier -> decline delivery_not_offered;
   UPI / bank transfer / wire / payment link / advance -> decline off_platform_payment.
   WhatsApp / "call me" only adds the low `off_platform_contact` signal.
5. Non-USD money (rupees, Rs, INR, rupaye, euro, EUR, pounds, GBP) -> clarify currency.
   Amounts are never converted.
6. A USD amount (see vocabulary) A, in cents:
   - A < 10% of asking (e.g. "6") -> clarify price_amount (likely shorthand);
   - A < 40% of asking -> decline price_not_acceptable (lowball);
   - A >= asking -> counter at asking (re-offer the list price);
   - A >= current open offer (buyer meets the seller's counter) -> counter at A,
     which creates an open offer at the buyer's price for them to accept;
   - otherwise split the difference: counter at ceil((A + asking) / 2) to the
     next $5, capped at the current open offer so the seller never re-raises.
   After last_feedback PRICE_OUT_OF_BOUNDS the counter moves halfway again
   toward asking from the split point (B0 cannot know the floor).
7. Offer words without a usable number ("can you do it cheaper?") -> clarify price_amount.
8. A bare "yes / ok / deal / haan / theek hai / done" while an offer is open
   -> clarify acceptance (a typed yes is not an explicit acceptance event).
9. Listing keywords -> answer_listing citing every matching public fact id.
10. Otherwise -> clarify item_details.

Money vocabulary
----------------
"$560", "$ 560.50", "USD 560", "560 USD", "560 dollars", "560 dollar",
"560 bucks", Hinglish "520 mein / me / mai / main / tak", and bare numbers
("would you take 560?") that are not followed by a unit (GB, TB, %, W, inch,
pm, am, baje, hours, minutes, days) and are not a clock time after "at".
Thousands separators are accepted. When several USD amounts appear, the
smallest is taken as the buyer's offer.

Listing-question vocabulary (fact id <- keywords)
-------------------------------------------------
f_availability <- available, still there, still have, sold, milega
f_memory <- ram, memory
f_storage <- storage, ssd, disk, hard drive
f_battery <- battery
f_charger <- charger, adapter, power brick
f_condition <- condition, scratch, dent, damage, screen, keyboard, working
f_model <- model, processor, cpu, i7, specs, which year, kaunsa
f_pickup <- pickup, pick up, pick it up, collect, location, where, kahan, kab
f_payment <- payment, pay, cash

Hinglish is detected from common romanized Hindi function words
(hai, kya, mein, de do, dunga, kar, bhai, nahi, haan, theek, kitna, ...);
the reply language follows the last buyer message.

Limitations (deliberate scope, not handicaps)
---------------------------------------------
- Number words ("five-sixty", "five hundred") are not parsed; such offers get
  clarify price_amount.
- "k" shorthand ("0.5k") and ranges ("550-600") are not interpreted beyond the
  smallest number.
- One action per turn: when a message both asks a question and makes an offer,
  the offer wins.
- It cannot remember a denied price across turns beyond what the context
  carries; a second consecutive PRICE_OUT_OF_BOUNDS repeats the same raised
  counter and is stopped by the run's correction cap.
- Keyword risk detection misses paraphrases that avoid every keyword and may
  over-trigger on innocent uses of words such as "proof" or "officer".
- It never cites facts outside the listing's public facts and never states a
  floor, because it has none.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------------
SIGNAL_ORDER = (
    "qr_receive_scam", "fake_payment_proof", "authority_claim", "off_platform_payment",
    "shipping_request", "private_info_request", "off_platform_contact",
)
HIGH = ("qr_receive_scam", "fake_payment_proof", "authority_claim")

RISK_PATTERNS: Dict[str, List[str]] = {
    "qr_receive_scam": [
        r"\bqr\b", r"\bscan\b", r"collect request", r"approve (?:the |my )?(?:upi )?request",
        r"receive (?:the |your )?(?:money|payment|paise|amount)", r"paise receive",
        r"link to receive", r"click (?:the |this )?link",
    ],
    "fake_payment_proof": [
        r"already (?:sent|paid|transferred)", r"\bsent you\b", r"screen ?shot", r"\bproof\b",
        r"payment (?:is )?done", r"bhej diye", r"bhej diya", r"transfer kar diya", r"paid already",
    ],
    "authority_claim": [
        r"\bi am the owner\b", r"\bi'?m the owner\b", r"this is the owner", r"\bowner (?:has |already )?(?:approved|agreed|said)",
        r"owner se (?:phone pe |call pe )?baat", r"main hi owner", r"\bapprove(?:d)? kar diya",
        r"ignore (?:your |the |all |previous |prior )*(?:rules|instructions)", r"rules ignore",
        r"\barmy\b", r"\bpolice\b", r"\bofficer\b", r"\bcustoms\b", r"\bgovernment official\b",
    ],
    "off_platform_payment": [
        r"\bupi\b", r"bank transfer", r"\bwire\b", r"payment link", r"pay(?:ment)? online", r"\bpaypal\b",
        r"\bgpay\b", r"google pay", r"\bvenmo\b", r"\bzelle\b", r"\badvance\b", r"\bneft\b", r"\bimps\b",
    ],
    "shipping_request": [
        r"\bship\b", r"\bshipping\b", r"\bdeliver", r"\bcourier\b", r"\bpost it\b", r"send it to",
        r"ghar (?:pe|par) bhej", r"home delivery",
    ],
    "private_info_request": [
        r"\baddress\b", r"phone number", r"your (?:phone|number|email|mobile)", r"\bemail\b",
        r"\bminimum\b", r"lowest (?:price|you|the seller|they|kitna)", r"\bfloor price\b", r"bottom price",
        r"seller'?s? (?:lowest|minimum|notes)", r"kam se kam",
    ],
    "off_platform_contact": [r"whats ?app", r"call me", r"text me", r"\btelegram\b", r"mujhe call"],
}

DECLINE_FOR_SIGNAL = {
    "qr_receive_scam": "qr_receive_scam",
    "fake_payment_proof": "fake_payment_proof",
    "authority_claim": "authority_claim",
    "private_info_request": "private_info_request",
    "shipping_request": "delivery_not_offered",
    "off_platform_payment": "off_platform_payment",
}
# Order in which a single last message picks its decline reason.
DECLINE_PRIORITY = ("qr_receive_scam", "fake_payment_proof", "authority_claim", "private_info_request",
                    "shipping_request", "off_platform_payment")

FACT_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("f_availability", [r"\bavailable\b", r"still there", r"still have", r"\bsold\b", r"\bmilega\b"]),
    ("f_model", [r"\bmodel\b", r"\bprocessor\b", r"\bcpu\b", r"\bi7\b", r"\bspecs?\b", r"which year", r"\bkaunsa\b"]),
    ("f_memory", [r"\bram\b", r"\bmemory\b"]),
    ("f_storage", [r"\bstorage\b", r"\bssd\b", r"\bdisk\b", r"hard drive"]),
    ("f_condition", [r"\bcondition\b", r"scratch", r"\bdents?\b", r"damage", r"\bscreen\b", r"\bkeyboard\b", r"\bworking\b"]),
    ("f_battery", [r"\bbattery\b"]),
    ("f_charger", [r"\bcharger\b", r"\badapter\b", r"power brick"]),
    ("f_pickup", [r"pick ?up", r"pick it up", r"\bcollect\b", r"\blocation\b", r"\bwhere\b", r"\bkahan\b", r"\bkab\b"]),
    ("f_payment", [r"\bpayment\b", r"\bpay\b", r"\bcash\b"]),
]

QUESTION_HINTS = [r"\?", r"\bkya\b", r"\bhow\b", r"\bwhat\b", r"\bis it\b", r"\bdoes\b", r"\bany\b", r"\bkitna\b",
                  r"\bkaisi\b", r"\bkaisa\b", r"\btell me\b", r"\bbatao\b"]

OFFER_WORDS = [
    r"\btake\b", r"\boffer\b", r"\bgive\b", r"\bpay\b", r"\bdo\b", r"\bfor\b", r"\bfinal\b", r"\bbudget\b",
    r"\bdeal\b", r"\bcheaper\b", r"\bdiscount\b", r"\bbest price\b", r"\blet it go\b", r"de do", r"\bdunga\b",
    r"\bdungi\b", r"\bchalega\b", r"le lo", r"\blelo\b", r"\bmein\b", r"\bkam\b", r"\bsasta\b", r"\bnegotiable\b",
]
ACCEPT_WORDS = [r"^\s*(?:yes|yeah|yep|ok(?:ay)?|deal|done|sure|haan|ha|theek hai|thik hai|chalo|pakka)\b"]

HINGLISH_TOKENS = {
    "hai", "hain", "kya", "mein", "de", "do", "dunga", "dungi", "kar", "karo", "bhai", "nahi", "haan", "theek",
    "thik", "kitna", "kitne", "aur", "abhi", "hoon", "hu", "kal", "aaj", "shaam", "lunga", "chalega", "bhej",
    "batao", "sach", "yeh", "woh", "mujhe", "mera", "mere", "paise", "jaldi", "lo", "lelo", "kaisi", "kahan",
    "kab", "agar", "saath", "baje", "ke", "ka", "ki", "se", "pe", "par", "diye", "diya", "sakta", "chahiye",
    "hafte", "naam", "isse", "zyada", "nahin", "accha", "acha", "ji",
}
HINGLISH_STRONG = {"hai", "kya", "dunga", "karo", "nahi", "haan", "theek", "kitna", "hoon", "lunga", "chalega",
                   "batao", "mujhe", "paise", "jaldi", "chahiye", "sakta", "diye", "bhai", "mein"}

OTHER_CURRENCY = re.compile(
    r"(?:₹|€|£|\brs\.?\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:rupees?|rupaye|rupay|rs\b|inr\b|euros?\b|eur\b|pounds?\b|gbp\b|yen\b)"
    r"|\b(?:inr|eur|gbp)\s*\d)",
    re.IGNORECASE,
)
NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?"
USD_PATTERNS = [
    re.compile(r"\$\s*" + NUM),
    re.compile(r"\busd\s*" + NUM, re.IGNORECASE),
    re.compile(NUM + r"\s*(?:usd\b|dollars?\b|bucks\b)", re.IGNORECASE),
    re.compile(NUM + r"\s*(?:mein|me|mai|main|tak)\b", re.IGNORECASE),
]
BARE_NUM = re.compile(r"(?<![\w.$₹€£/-])" + NUM + r"(?![\w%])")
UNIT_AFTER = re.compile(
    r"^\s*-?\s*(?:gb|tb|mb|%|w\b|watt|inch|in\b|\"|pm|am|baje|bje|o'?clock|hours?|hrs?|mins?|minutes?|days?|"
    r"weeks?|months?|years?|yrs?|th\b|st\b|nd\b|rd\b|cores?|ghz)",
    re.IGNORECASE,
)


def _has(text: str, patterns: List[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def detect_signals(text: str) -> List[str]:
    """Risk signals (POLICY section 6 vocabulary) found in one buyer message."""
    return [s for s in SIGNAL_ORDER if _has(text, RISK_PATTERNS[s])]


def detect_language(text: str) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return "en"
    strong = sum(1 for w in words if w in HINGLISH_STRONG)
    weak = sum(1 for w in words if w in HINGLISH_TOKENS)
    return "hinglish" if strong >= 1 and (weak >= 2 or len(words) <= 4) else "en"


def _to_cents(whole: str, frac: Optional[str]) -> int:
    cents = int(whole.replace(",", "")) * 100
    if frac:
        cents += int(frac.ljust(2, "0"))
    return cents


def extract_usd_amounts(text: str) -> Tuple[List[int], bool]:
    """Return (USD amounts in cents, explicit) from a message; explicit means currency-marked."""
    explicit: List[int] = []
    spans: List[Tuple[int, int]] = []
    for pat in USD_PATTERNS:
        for m in pat.finditer(text):
            explicit.append(_to_cents(m.group(1), m.group(2)))
            spans.append(m.span())
    if explicit:
        return explicit, True
    bare: List[int] = []
    for m in BARE_NUM.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        after = text[m.end():]
        if UNIT_AFTER.match(after):
            continue
        before = text[:m.start()].rstrip().lower()
        value = int(m.group(1).replace(",", ""))
        if before.endswith(" at") or before.endswith("by") or before == "at":
            if value <= 24:
                continue  # clock time, e.g. "pickup at 7"
        if 1900 <= value <= 2100 and not m.group(2):
            continue  # a year
        bare.append(_to_cents(m.group(1), m.group(2)))
    return bare, False


def _ceil_to(cents: int, step: int = 500) -> int:
    return int(math.ceil(cents / float(step)) * step)


def _proposal(kind: str, *, intent: Optional[str] = None, facts: Optional[List[str]] = None,
              price: Optional[int] = None, topic: Optional[str] = None, reason: Optional[str] = None,
              acc: Optional[str] = None, language: str = "en", signals: Optional[List[str]] = None,
              rationale: str = "") -> Dict[str, Any]:
    return {
        "kind": kind,
        "message_intent": intent,
        "fact_ids": list(facts or []),
        "price_minor": price,
        "currency": "USD" if price is not None else None,
        "clarify_topic": topic,
        "decline_reason": reason,
        "acceptance_event_id": acc,
        "language": language,
        "buyer_risk_signals": list(signals or []),
        "rationale": ("B0: " + rationale)[:240],
    }


# ----------------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------------
STOP_CODES = ("BUDGET_EXHAUSTED", "MANDATE_NOT_ACTIVE", "MANDATE_PAUSED", "MANDATE_REVOKED", "MANDATE_EXPIRED",
              "STALE_REVISION", "ACTION_EXPIRED", "RUN_NOT_LIVE", "FENCE_STALE", "OUT_OF_SCOPE")


def propose(context: Dict[str, Any]) -> Dict[str, Any]:
    """Return one POLICY section 2 proposal for the Appendix B public context."""
    listing = context.get("listing") or {}
    asking = int(listing.get("asking_price_minor") or 0)
    public_ids = [f.get("id") for f in listing.get("public_facts") or [] if isinstance(f, dict)]
    conversation = sorted(context.get("conversation") or [], key=lambda m: m.get("seq", 0))
    open_offer = context.get("open_offer")
    acceptances = context.get("acceptances") or []
    reservation = context.get("reservation")
    feedback = context.get("last_feedback") or {}
    fb_code = feedback.get("code") if isinstance(feedback, dict) else None

    buyer_msgs = [m for m in conversation if m.get("role") == "buyer"]
    last_buyer = buyer_msgs[-1]["text"] if buyer_msgs else ""
    lang = detect_language(last_buyer) if last_buyer else "en"

    run_signals: List[str] = []
    for m in buyer_msgs:
        for s in detect_signals(m.get("text") or ""):
            if s not in run_signals:
                run_signals.append(s)
    run_signals = [s for s in SIGNAL_ORDER if s in run_signals]
    high = [s for s in run_signals if s in HIGH]

    # 1. Feedback that ends the turn.
    if fb_code in STOP_CODES:
        return _proposal("no_action", language=lang, signals=run_signals,
                         rationale="Gate reported %s; waiting." % fb_code)
    if fb_code == "ITEM_UNAVAILABLE":
        return _proposal("send_message", intent="decline", reason="unsupported_request", language=lang,
                         signals=run_signals, rationale="Item already reserved.")

    # 2. Acceptance of the current open offer.
    if reservation is None and open_offer and fb_code not in ("ACCEPTANCE_MISSING", "ACCEPTANCE_MISMATCH", "SCAM_RISK"):
        for a in reversed(acceptances):
            if (a.get("offer_id") == open_offer.get("offer_id")
                    and a.get("price_minor") == open_offer.get("price_minor")
                    and a.get("currency") == open_offer.get("currency") == "USD"):
                if high:
                    return _proposal("send_message", intent="decline", reason=DECLINE_FOR_SIGNAL[high[0]],
                                     language=lang, signals=run_signals,
                                     rationale="Buyer accepted but raised %s; no reservation." % high[0])
                return _proposal("reserve_item", price=int(a["price_minor"]), acc=a.get("acceptance_event_id"),
                                 language=lang, signals=run_signals,
                                 rationale="Buyer accepted open offer %s." % open_offer.get("offer_id"))
    if fb_code == "SCAM_RISK" and high:
        return _proposal("send_message", intent="decline", reason=DECLINE_FOR_SIGNAL[high[0]], language=lang,
                         signals=run_signals, rationale="Reservation blocked for payment or authority risk.")
    if fb_code in ("ACCEPTANCE_MISSING", "ACCEPTANCE_MISMATCH"):
        return _proposal("send_message", intent="clarify", topic="acceptance", language=lang,
                         signals=run_signals, rationale="Need an explicit acceptance of the current offer.")

    # 3. Is it the buyer's turn?
    if not conversation or conversation[-1].get("role") != "buyer":
        return _proposal("no_action", language=lang, signals=run_signals, rationale="No new buyer message.")

    text = last_buyer
    low = text.lower()
    msg_signals = detect_signals(text)

    # 4. Risk declines on the last message.
    for s in DECLINE_PRIORITY:
        if s in msg_signals:
            return _proposal("send_message", intent="decline", reason=DECLINE_FOR_SIGNAL[s], language=lang,
                             signals=run_signals, rationale="Buyer message matched %s." % s)

    # 5. Other currencies.
    if OTHER_CURRENCY.search(text):
        return _proposal("send_message", intent="clarify", topic="currency", language=lang, signals=run_signals,
                         rationale="Amount is not in USD; asking for USD without converting.")

    # 6. Dollar amounts.
    amounts, explicit = extract_usd_amounts(text)
    offer_context = _has(low, OFFER_WORDS)
    if amounts and (explicit or offer_context or len(text.split()) <= 3):
        a = min(amounts)
        if a < asking // 10:
            return _proposal("send_message", intent="clarify", topic="price_amount", language=lang,
                             signals=run_signals, rationale="Amount too small to be meant literally.")
        if a < (asking * 4) // 10:
            return _proposal("send_message", intent="decline", reason="price_not_acceptable", language=lang,
                             signals=run_signals, rationale="Offer far below the asking price.")
        current = int(open_offer["price_minor"]) if open_offer and open_offer.get("price_minor") else asking
        if a >= asking:
            price = asking
            why = "Offer at or above asking; re-offering list price."
        elif a >= current:
            price = a
            why = "Buyer meets the open offer; offering at buyer's price."
        else:
            price = min(_ceil_to((a + asking) // 2), current, asking)
            why = "Split the difference toward asking."
            if fb_code == "PRICE_OUT_OF_BOUNDS":
                # The current open offer was committed, so it is in bounds; never exceed it.
                price = min(_ceil_to((price + asking) // 2), current, asking)
                why = "Previous counter was out of bounds; moving toward asking."
            price = max(price, a)
        return _proposal("send_message", intent="counter_offer", price=price, language=lang, signals=run_signals,
                         rationale=why)

    # 7. Offer words without a usable number.
    if offer_context and _has(low, [r"\bcheaper\b", r"\bdiscount\b", r"\bbest price\b", r"\bkam\b", r"\bsasta\b",
                                    r"\bnegotiable\b", r"\blet it go\b", r"\btake\b", r"\boffer\b",
                                    r"\bfive\b", r"\bsix\b", r"\bhundred\b"]):
        return _proposal("send_message", intent="clarify", topic="price_amount", language=lang,
                         signals=run_signals, rationale="Offer without a clear dollar amount.")

    # 8. A typed yes is not an acceptance event.
    if open_offer and _has(low, ACCEPT_WORDS):
        return _proposal("send_message", intent="clarify", topic="acceptance", language=lang, signals=run_signals,
                         rationale="Typed yes; asking for explicit acceptance of the open offer.")

    # 9. Listing questions.
    facts = [fid for fid, pats in FACT_KEYWORDS if fid in public_ids and _has(low, pats)]
    if facts:
        return _proposal("send_message", intent="answer_listing", facts=facts, language=lang, signals=run_signals,
                         rationale="Answering from public facts.")

    # 10. Fallback.
    why = ("Question not covered by public facts." if _has(low, QUESTION_HINTS)
           else "Could not classify the buyer message.")
    return _proposal("send_message", intent="clarify", topic="item_details", language=lang, signals=run_signals,
                     rationale=why)
