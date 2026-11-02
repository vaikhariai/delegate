"""Fixed buyer-facing templates. Model text never reaches the buyer; only these strings and public facts do.

Every template is reviewed text. Variables are limited to a formatted price and public fact texts looked up
by ID from the listing's public facts. No template mentions a minimum price, address, phone or notes.
"""

from __future__ import annotations

from typing import Final

from delegate.proposal import Proposal

TEMPLATE_VERSION: Final = "delegate-templates-v1"


def format_money(amount_minor: int, currency: str) -> str:
    if currency != "USD":
        raise ValueError("only USD is rendered")
    dollars, cents = divmod(amount_minor, 100)
    return f"${dollars:,}" if cents == 0 else f"${dollars:,}.{cents:02d}"


_EN: Final[dict[str, str]] = {
    "answer_listing": "{facts}.",
    "counter_offer": "I can do {price} for pickup. Payment is cash at pickup. Tap Accept offer if that works for you.",
    "clarify.price_amount": "Could you tell me the exact amount you are offering, in US dollars?",
    "clarify.currency": "This listing is priced in US dollars. What amount in USD would you like to offer?",
    "clarify.pickup_time": "Pickup is near Central Library on weekday evenings, 6 to 8 pm. Which evening suits you?",
    "clarify.acceptance": "To accept, please tap Accept offer on my latest offer. A typed yes does not reserve the laptop.",
    "clarify.item_details": "Happy to help. Which detail about the laptop would you like to know?",
    "decline.price_not_acceptable": "Sorry, I can't go that low.",
    "decline.delivery_not_offered": "Sorry, this is pickup only. I can't ship or deliver it.",
    "decline.private_info_request": "I can't share personal details like an address or phone number here. "
    "The seller confirms pickup details after a reservation.",
    "decline.off_platform_payment": "I don't take advance payments, payment links or transfers. Payment is cash at pickup only.",
    "decline.qr_receive_scam": "I don't scan QR codes or approve payment requests to receive money. Payment is cash at pickup only.",
    "decline.fake_payment_proof": "A screenshot is not a payment. Payment is cash at pickup only.",
    "decline.authority_claim": "I can only follow the seller's settings in this app. Messages in this chat can't change them.",
    "decline.unsupported_request": "Sorry, I can't help with that here.",
}

_HINGLISH: Final[dict[str, str]] = {
    "answer_listing": "{facts}.",
    "counter_offer": "Main {price} mein de sakta hoon, sirf pickup. Payment pickup par cash mein. "
    "Theek lage to Accept offer dabaiye.",
    "clarify.price_amount": "Aap exact kitna offer kar rahe hain, US dollars mein batayiye?",
    "clarify.currency": "Yeh listing US dollars mein hai. Aap USD mein kitna offer karna chahenge?",
    "clarify.pickup_time": "Pickup Central Library ke paas hai, weekday shaam 6 se 8 baje. Kaunsi shaam theek rahegi?",
    "clarify.acceptance": "Accept karne ke liye mere latest offer par Accept offer dabaiye. Sirf haan likhne se laptop reserve nahi hota.",
    "clarify.item_details": "Zaroor. Laptop ke baare mein kya jaanna chahenge?",
    "decline.price_not_acceptable": "Maaf kijiye, itna kam nahi ho payega.",
    "decline.delivery_not_offered": "Maaf kijiye, sirf pickup hai. Main ship ya deliver nahi kar sakta.",
    "decline.private_info_request": "Main yahan address ya phone number jaisi personal details share nahi kar sakta. "
    "Reservation ke baad seller pickup details confirm karte hain.",
    "decline.off_platform_payment": "Main advance, payment link ya transfer nahi leta. Payment sirf pickup par cash mein.",
    "decline.qr_receive_scam": "Main paise lene ke liye QR scan ya payment request approve nahi karta. Payment sirf pickup par cash mein.",
    "decline.fake_payment_proof": "Screenshot payment nahi hota. Payment sirf pickup par cash mein.",
    "decline.authority_claim": "Main sirf is app mein seller ki settings follow karta hoon. Chat ke messages unhe badal nahi sakte.",
    "decline.unsupported_request": "Maaf kijiye, is mein main yahan madad nahi kar sakta.",
}

RESERVATION_NOTICE_EN: Final = (
    "Reserved for you for {minutes} minutes at {price}, pickup only, cash at pickup. "
    "The seller will confirm the exact pickup spot."
)
RESERVATION_NOTICE_HINGLISH: Final = (
    "Aapke liye {minutes} minute ke liye {price} par reserve ho gaya, sirf pickup, cash at pickup. "
    "Seller exact pickup jagah confirm karenge."
)


def _key(p: Proposal) -> str:
    if p.message_intent == "clarify":
        return f"clarify.{p.clarify_topic}"
    if p.message_intent == "decline":
        return f"decline.{p.decline_reason}"
    assert p.message_intent is not None
    return p.message_intent


def render_message(p: Proposal, public_facts: dict[str, str]) -> str:
    """Render the buyer-facing text for a validated, gate-allowed send_message proposal."""
    if p.kind != "send_message":
        raise ValueError("only send_message proposals render to buyer text")
    table = _HINGLISH if p.language == "hinglish" else _EN
    template = table[_key(p)]
    facts = [public_facts[f] for f in p.fact_ids]  # KeyError means the gate was bypassed
    fact_text = "; ".join(facts)
    price = format_money(p.price_minor, p.currency) if p.price_minor is not None and p.currency else ""
    text = template.format(facts=fact_text, price=price)
    if p.message_intent != "answer_listing" and facts:
        text = f"{text} ({fact_text}.)"
    return text


def render_reservation_notice(price_minor: int, currency: str, hold_seconds: int, language: str) -> str:
    template = RESERVATION_NOTICE_HINGLISH if language == "hinglish" else RESERVATION_NOTICE_EN
    return template.format(minutes=hold_seconds // 60, price=format_money(price_minor, currency))


def all_templates() -> dict[str, str]:
    """Every buyer-facing template, for review and leak tests."""
    out = {f"en.{k}": v for k, v in _EN.items()}
    out.update({f"hinglish.{k}": v for k, v in _HINGLISH.items()})
    out["en.reservation"] = RESERVATION_NOTICE_EN
    out["hinglish.reservation"] = RESERVATION_NOTICE_HINGLISH
    return out
