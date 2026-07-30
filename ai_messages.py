"""
MapLead — Per-lead unique message engine
=========================================

Generates a UNIQUE outreach message for every lead based on its actual
data. No two leads with the same message (unless they have identical
fields, in which case angles differ deterministically).

Three layers:

1. Templates (always work, zero cost)
   - 12 angles (reputation, growth, curiosity, authority, ...)
   - Per-lead angle selection via stable hash (deterministic but unique)
   - 3-4 specific details pulled from business fields

2. Multi-channel output
   - Initial outreach (email body)
   - Email subject (3 variants to A/B test)
   - WhatsApp short
   - Call script (voice outreach)
   - 3-step follow-up sequence (day 1, 3, 7)

3. AI enhancement (when key works)
   - Asks LLM to write a custom message using the SAME angle + details
   - Falls back to template if AI fails or returns nothing useful

Result: each lead gets a unique message that mentions their actual data
(name, city, rating, reviews, category). Vastly outperforms generic
mass-mail blasts.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from scraper import Business
from ai_core import (
    AICore,
    ScoreResult,
    detect_provider,
    mask_key,
    heuristic_score,
    _guess_category,
    _extract_city,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message angles
# ---------------------------------------------------------------------------
# Each angle has:
#   - subject_styles: 2-3 subject line templates
#   - opener: opening sentence template
#   - bridge: middle paragraph that ties to a specific observation
#   - offer: concrete value proposition
#   - close: call to action
# Placeholders: {name} {city} {cat} {rating} {reviews} {phone} {website} {area}

ANGLE_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "reputation",
        "subject_styles": [
            "Your {rating}-star reputation caught my eye, {name}",
            "Quick question about {name}'s customer experience",
        ],
        "opener": "Hi {name} team,",
        "bridge": (
            "Your {rating}-star rating from {reviews} reviews in {city} shows "
            "you clearly know how to keep customers happy — that's the hardest part of {cat}."
        ),
        "offer": (
            "I help other top-rated {cat} businesses multiply that goodwill into "
            "repeat visits and referrals, with response automation that pays for itself in weeks."
        ),
        "close": "Worth a 10-minute chat this week?",
    },
    {
        "id": "growth",
        "subject_styles": [
            "An idea for {name}",
            "Quick growth note for {area}",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "Came across your listing while looking at {cat} businesses in {city}. "
            "You have {reviews} reviews and {rating} stars — solid foundation, "
            "but I noticed you may be missing some easy wins in {area}."
        ),
        "offer": (
            "I work with {cat} businesses on three specific improvements that "
            "typically lift footfall within a month: faster response times, "
            "better follow-ups, and reviews that pull their weight."
        ),
        "close": "Want me to send a 1-pager with the specifics?",
    },
    {
        "id": "volume",
        "subject_styles": [
            "{reviews}+ reviews — small thing {name} might be missing",
            "Your customers are talking, {area}",
        ],
        "opener": "Hello {name} team,",
        "bridge": (
            "{reviews} people took the time to review {name} in {city} — "
            "that's serious word-of-mouth momentum. Most {cat} businesses "
            "stop there and let it ride."
        ),
        "offer": (
            "I help businesses turn that momentum into 15-25% more repeat customers "
            "with simple automation: review replies, follow-ups, and gentle reminders "
            "that nudge customers back at the right moment."
        ),
        "close": "Open to a quick look at what's possible?",
    },
    {
        "id": "local_expert",
        "subject_styles": [
            "Quick note from a neighbor in {area}",
            "Helping {cat} businesses in {city}",
        ],
        "opener": "Hi {name} team,",
        "bridge": (
            "I'm based in {city} and work with several {cat} businesses nearby. "
            "{name} on the radar — your {rating}-star average is hard to miss."
        ),
        "offer": (
            "I help local {cat} operations plug simple leaks: missed calls, slow replies, "
            "lost leads. One client in {area} recovered 14 hours/week just by automating their "
            "inbound."
        ),
        "close": "Curious if there's a fit — worth a 10-minute call?",
    },
    {
        "id": "pain_point",
        "subject_styles": [
            "The 3 leaks I see in most {cat} businesses",
            "Quick observation, {name}",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "Across {cat} businesses I work with, three leaks show up constantly: "
            "missed calls after hours, slow response, and reviews that go unanswered. "
            "I'd bet at least one applies to {name} in {area}."
        ),
        "offer": (
            "I help plug all three in a single weekend setup — "
            "no new tools for your team to learn, just better defaults."
        ),
        "close": "Want me to do a 5-minute audit?",
    },
    {
        "id": "authority",
        "subject_styles": [
            "Working with {cat} across {area} — quick idea",
            "One thing {name} might find useful",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "I've spent the last few years specifically on {cat} growth in {city}. "
            "Patterns repeat: the businesses that handle the boring operational stuff well "
            "win on the customer-experience side. {name} already has the product right."
        ),
        "offer": (
            "I bring a 90-minute playbook that other {cat} businesses have used to "
            "automate 60-70% of their inbound replies without losing the personal touch."
        ),
        "close": "Want a copy of the playbook?",
    },
    {
        "id": "time_sensitive",
        "subject_styles": [
            "Two-minute idea for {name}",
            "Saw your listing, {area}",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "Spotted {name} on Google Maps while looking at {cat} in {city} this week. "
            "{rating} stars is solid, but your listing photo and reply-time look like they're "
            "leaving some easy leads on the table."
        ),
        "offer": (
            "I help {cat} businesses fix exactly this in under an hour — "
            "better photos, faster replies, and a follow-up sequence that runs while you sleep."
        ),
        "close": "Want me to send before/after examples?",
    },
    {
        "id": "curiosity",
        "subject_styles": [
            "Saw something interesting about {name}",
            "Quick question, {area}",
        ],
        "opener": "Hi {name} team,",
        "bridge": (
            "I was helping a {cat} business in {area} last week, and ran a report on similar "
            "businesses in {city}. {name} showed up as an interesting outlier — "
            "strong reputation, but with what looks like untapped potential."
        ),
        "offer": (
            "I'd like to share the report — it's a 6-page comparison with concrete "
            "patterns from {area}-based {cat} leaders."
        ),
        "close": "May I send it over?",
    },
    {
        "id": "mutual_benefit",
        "subject_styles": [
            "Looking to partner with {area}'s best — got 30 seconds?",
            "Possible collaboration, {name}",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "We work with around a dozen {cat} operations in {city} and we're always "
            "looking for one more great fit. {name}'s {rating}-star reputation "
            "and {reviews} reviews put you on the shortlist."
        ),
        "offer": (
            "No cost, no commitment — we'd just like to understand what tools and processes "
            "you're already using for {cat} customer engagement, and share what's working elsewhere."
        ),
        "close": "Worth a 15-minute call to compare notes?",
    },
    {
        "id": "direct_value",
        "subject_styles": [
            "3 things I'd improve on {name}'s {cat} flow (in 24 hours)",
            "Quick wins for {name}",
        ],
        "opener": "Hi {name},",
        "bridge": (
            "I reviewed your Google presence and your {cat} flow as a customer would. "
            "Three specific things would lift conversions noticeably, and they're all doable "
            "this week for {name} in {city}."
        ),
        "offer": (
            "I'd send a 1-page note with the three changes, expected impact, and a "
            "do-it-yourself checklist. No pitch, no sales call."
        ),
        "close": "Want me to send it?",
    },
    {
        "id": "missed_call",
        "subject_styles": [
            "If {phone} rings at 9pm, who picks up?",
            "Why {area} {cat} owners lose 30% of leads",
        ],
        "opener": "Hi {name} team,",
        "bridge": (
            "I noticed {name} lists {phone}. Most {cat} businesses in {city} "
            "miss 25-40% of inbound calls because they happen outside business hours."
        ),
        "offer": (
            "I work with {cat} teams on a 24/7 AI receptionist that costs "
            "a fraction of a part-time hire — takes 48 hours to set up, no new contracts."
        ),
        "close": "Want a 2-minute demo?",
    },
    {
        "id": "compliment_close",
        "subject_styles": [
            "Loved your reviews, {name}",
            "{city}'s {cat} highlight",
        ],
        "opener": "Hi {name} team,",
        "bridge": (
            "Just read through some of {name}'s recent reviews — "
            "the customer stories are genuinely lovely. Especially the ones about {cat} service."
        ),
        "offer": (
            "I help {cat} businesses amplify that kind of social proof through "
            "better review-responds and gentle re-engagement. Pure word-of-mouth."
        ),
        "close": "Open to a quick conversation about how to multiply this?",
    },
]


# ---------------------------------------------------------------------------
# Per-lead message generation
# ---------------------------------------------------------------------------


def _stable_hash(parts: list[str]) -> int:
    """Stable integer hash from a list of strings."""
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(h[:8], 16)


def _pick_angle_for(biz: Business) -> dict[str, Any]:
    """Pick a message angle for this lead (deterministic per business)."""
    h = _stable_hash([biz.name or "", biz.address or "", biz.phone_number or ""])
    return ANGLE_TEMPLATES[h % len(ANGLE_TEMPLATES)]


def _local_obs(biz: Business) -> str:
    """Extract a lead-specific concrete observation (human-readable).

    Things like 'your last 5 reviews mention "parking"', 'reviews mention "late nights"'.
    """
    return ""  # Could pull from reviews_text if available


def _fill(template: str, biz: Business, cat: str, city: str, area: str) -> str:
    """Replace placeholders in a template with safe string fallbacks."""
    replacements = {
        "name": biz.name or "your team",
        "city": city,
        "area": area or city or "your area",
        "cat": cat,
        "rating": f"{biz.reviews_average:.1f}" if biz.reviews_average else "your",
        "reviews": str(biz.reviews_count) if biz.reviews_count else "your",
        "phone": biz.phone_number or "your number",
        "website": biz.website or "your site",
    }
    out = template
    for k, v in replacements.items():
        out = out.replace("{" + k + "}", str(v))
    # Collapse any unfilled placeholders
    out = re.sub(r"\{[a-z_]+\}", "", out).strip()
    # Tidy double spaces
    out = re.sub(r"  +", " ", out)
    return out


@dataclass
class LeadMessages:
    """All message variants for a single lead."""

    name: str = ""
    category: str = ""
    city: str = ""
    angle_id: str = ""

    # Primary outreach
    subject: str = ""
    subject_b: str = ""  # alternative subject line for A/B
    subject_c: str = ""
    body_email: str = ""

    # Short-form
    whatsapp_short: str = ""
    sms: str = ""

    # Voice
    call_script: str = ""

    # Follow-up sequence
    followup_day3: str = ""
    followup_day7: str = ""
    followup_day14: str = ""

    source: str = "template"  # "template" | "ai"

    def to_dict(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "subject_b": self.subject_b,
            "subject_c": self.subject_c,
            "body_email": self.body_email,
            "whatsapp_short": self.whatsapp_short,
            "sms": self.sms,
            "call_script": self.call_script,
            "followup_day3": self.followup_day3,
            "followup_day7": self.followup_day7,
            "followup_day14": self.followup_day14,
            "angle_id": self.angle_id,
            "source": self.source,
        }


def _local_area(biz: Business, city: str) -> str:
    """Try to extract a hyper-local area (3rd-from-last in address)."""
    if not biz.address:
        return ""
    parts = [p.strip() for p in biz.address.split(",") if p.strip()]
    # Local area is often parts[-3] in 5-part addresses (street, area, city, state, country)
    if len(parts) >= 4:
        candidate = parts[-3]
        if candidate and candidate != city:
            return candidate
    return ""


def _templated_messages(biz: Business) -> LeadMessages:
    """Generate all message variants from templates (no LLM needed)."""
    cat = _guess_category(biz.name, biz.category)
    cat_nice = cat.replace("_", " ")
    city = _extract_city(biz.address)
    area = _local_area(biz, city)

    angle = _pick_angle_for(biz)
    name = biz.name or "your business"

    # Fill the angle template
    subject = _fill(angle["subject_styles"][0], biz, cat_nice, city, area)
    subject_b = _fill(angle["subject_styles"][-1], biz, cat_nice, city, area)
    subject_c = (
        _fill(angle["subject_styles"][0], biz, cat_nice, city, area) + " (2-min read)"
        if len(angle["subject_styles"]) == 1
        else _fill(angle["subject_styles"][1] + " — 60s read", biz, cat_nice, city, area)
    )

    opener = _fill(angle["opener"], biz, cat_nice, city, area)
    bridge = _fill(angle["bridge"], biz, cat_nice, city, area)
    offer = _fill(angle["offer"], biz, cat_nice, city, area)
    close = _fill(angle["close"], biz, cat_nice, city, area)

    body = f"{opener}\n\n{bridge}\n\n{offer}\n\n{close}"

    # Channel-specific variants
    whatsapp = f"Hi {name} — came across your {cat_nice} in {city}. Quick idea worth 60 seconds? Happy to share details."
    sms = f"Hi {name}, a quick thought re your {cat_nice} in {city}. Worth a 2-min call? - sent via MapLead"

    call_script = (
        f"[OPENING]\n"
        f"Hi, may I speak with the owner or manager? My name is [your name], I'm a local {cat_nice} consultant.\n\n"
        f"[HOOK]\n"
        f"I'm calling {cat_nice} businesses in {city} — found {name} on Google Maps. You have "
        f"a strong reputation and I had a quick observation.\n\n"
        f"[PITCH]\n"
        f"{bridge[:280]}\n\n"
        f"[OFFER]\n"
        f"I help {cat_nice} businesses with three specific things: faster responses, smarter "
        f"follow-ups, and reviews that pull their weight. Most clients see results in weeks.\n\n"
        f"[CLOSE]\n"
        f"If you have 10 minutes this week, I can show you the specifics — no pitch, just the playbook. "
        f"When works for you?"
    )

    # Follow-up sequence (different angles each time)
    followup_day3 = (
        f"Hi {name} team,\n\n"
        f"Following up on my note from earlier this week — totally understand if it's not a fit, "
        f"but if there's any chance the timing was just off, I'm happy to send a 1-pager "
        f"specific to {cat_nice} in {city}.\n\n"
        f"Just reply 'send it' and I'll keep it short."
    )

    followup_day7 = (
        f"Hi {name},\n\n"
        f"Quick one — I just wrapped a project with another {cat_nice} in {city}, and the result "
        f"was pretty striking (recover ~10 hrs/week in missed-call automation). "
        f"Thought it might be relevant for {name}.\n\n"
        f"Worth a quick look? I'll send a 3-bullet summary if so."
    )

    followup_day14 = (
        f"Hi {name},\n\n"
        f"Last note on this — completely understand if {cat_nice} ops in {city} "
        f"isn't a priority right now. If anything changes in the next few months — "
        f"expansion, staffing changes, slow season end — I'd love to be a quick "
        f"resource. Wishing the team the best.\n\n"
        f"[your name]"
    )

    return LeadMessages(
        name=name,
        category=cat,
        city=city,
        angle_id=angle["id"],
        subject=subject,
        subject_b=subject_b,
        subject_c=subject_c,
        body_email=body,
        whatsapp_short=whatsapp,
        sms=sms,
        call_script=call_script,
        followup_day3=followup_day3,
        followup_day7=followup_day7,
        followup_day14=followup_day14,
        source="template",
    )


# ---------------------------------------------------------------------------
# AI-enhanced message (when key works)
# ---------------------------------------------------------------------------


@dataclass
class MessageEngine:
    """Generates unique messages per lead. AI when available, templates otherwise."""

    ai: Optional[AICore] = None

    def for_lead(self, biz: Business, channel: str = "email") -> LeadMessages:
        """Generate full message set for one lead.

        channel: "email" | "whatsapp" | "call" (primary channel to enhance)
        """
        base = _templated_messages(biz)

        if self.ai is None or not self.ai.is_working():
            return base

        try:
            enhanced = self._ai_enhance_all(biz, base, channel)
            if enhanced:
                enhanced.angle_id = base.angle_id
                enhanced.name = base.name
                enhanced.category = base.category
                enhanced.city = base.city
                enhanced.source = "ai"
                return enhanced
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI message enhance failed for %s: %s", biz.name, exc)

        return base

    def _ai_enhance_all(
        self, biz: Business, base: LeadMessages, channel: str
    ) -> LeadMessages | None:
        """Ask the AI to write a more unique version of the template-derived message."""
        if self.ai is None:
            return None

        sys_msg = (
            "You are a top-tier B2B copywriter with a track record in cold outreach "
            "for Indian SMBs. You write short, warm, specific messages that read like "
            "they came from a thoughtful person — not a template. "
            "You mention concrete details from the business (city, category, "
            "rating, reviews). You avoid generic filler and salesy language. "
            "You write three or four sentences max in the body."
        )
        user = self._build_enhance_prompt(biz, base, channel)
        raw = self.ai._call(sys_msg, user, max_tokens=400, temperature=0.6)
        if not raw or len(raw) < 40:
            return None
        # Split '|||' separated output into body + alternate subject
        return self._parse_enhance(raw, base)

    @staticmethod
    def _build_enhance_prompt(biz: Business, base: LeadMessages, channel: str) -> str:
        return (
            f"Rewrite this outreach message so it feels less templated. Keep it {channel}-friendly. "
            f"Mention the business by name and the city naturally. Don't invent facts "
            f"about the business.\n\n"
            f"Business: {biz.name}\n"
            f"Category: {biz.category or 'local business'}\n"
            f"City: {_extract_city(biz.address)}\n"
            f"Rating: {biz.reviews_average} ({biz.reviews_count} reviews)\n"
            f"Angle: {base.angle_id}\n\n"
            f"Draft body:\n{base.body_email}\n\n"
            f"Output format (use these exact labels, separated by '|||'):\n"
            f"SUBJECT|||your new email subject line (<= 60 chars)|||\n"
            f"BODY|||your rewritten body (3-5 sentences, plain text, no bullets)|||\n"
            f"WHATSAPP|||your 1-line WhatsApp version (<= 160 chars)|||"
        )

    @staticmethod
    def _parse_enhance(raw: str, base: LeadMessages) -> LeadMessages | None:
        try:
            parts = [p.strip() for p in raw.split("|||")]
            # Expecting: SUBJECT|||<text>|||BODY|||<text>|||WHATSAPP|||<text>
            subject = ""
            body = ""
            wa = ""
            i = 0
            while i < len(parts):
                label = parts[i].upper().strip()
                val = parts[i + 1] if i + 1 < len(parts) else ""
                if label.startswith("SUBJECT"):
                    subject = val
                elif label.startswith("BODY"):
                    body = val
                elif label.startswith("WHATSAPP"):
                    wa = val
                i += 2
            if not body or len(body) < 30:
                return None
            out = LeadMessages(
                subject=subject or base.subject,
                subject_b=base.subject_b,
                subject_c=base.subject_c,
                body_email=body,
                whatsapp_short=wa or base.whatsapp_short,
                sms=base.sms,
                call_script=base.call_script,
                followup_day3=base.followup_day3,
                followup_day7=base.followup_day7,
                followup_day14=base.followup_day14,
                source="ai",
            )
            return out
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Convenience: enrich a list of businesses
# ---------------------------------------------------------------------------


def enrich_leads_with_messages(
    businesses: list[Business],
    ai: Optional[AICore] = None,
    channel: str = "email",
) -> list[Business]:
    """Add message fields to each Business (mutates in place).

    Adds these to Business dataclass (uses getattr so it works with old code):
        * ai_subject / ai_subject_b / ai_subject_c
        * ai_body_email
        * ai_whatsapp
        * ai_sms
        * ai_call_script
        * ai_followup_day3 / ai_followup_day7 / ai_followup_day14
        * ai_angle_id
        * ai_messages_source
    """
    engine = MessageEngine(ai=ai)
    for biz in businesses:
        msgs = engine.for_lead(biz, channel=channel)
        d = msgs.to_dict()
        biz.ai_subject = d["subject"]
        biz.ai_subject_b = d["subject_b"]
        biz.ai_subject_c = d["subject_c"]
        biz.ai_body_email = d["body_email"]
        biz.ai_whatsapp = d["whatsapp_short"]
        biz.ai_sms = d["sms"]
        biz.ai_call_script = d["call_script"]
        biz.ai_followup_day3 = d["followup_day3"]
        biz.ai_followup_day7 = d["followup_day7"]
        biz.ai_followup_day14 = d["followup_day14"]
        biz.ai_angle_id = d["angle_id"]
        biz.ai_messages_source = d["source"]
    return businesses


def get_message_for(biz: Business) -> dict[str, str]:
    """Return all message variants for one lead (template-only, instant)."""
    return _templated_messages(biz).to_dict()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    from scraper import Business

    sample_bizs = [
        Business(name="Punjabi Rasoi", address="12 Linking Road, Bandra West, Mumbai 400050",
                 phone_number="+91 22 2640 1234", category="restaurant", reviews_average=4.3, reviews_count=542),
        Business(name="Joe's Cafe", address="5 Park Ave, New York, NY 10016",
                 phone_number="+1 212-555-0100", category="cafe", reviews_average=4.7, reviews_count=1280),
        Business(name="Quick Mart", phone_number="+91 99876 54321"),
    ]
    for biz in sample_bizs:
        print(f"\n=== {biz.name} ===")
        m = get_message_for(biz)
        print(f"Angle: {m['angle_id']}")
        print(f"Subject: {m['subject']}")
        print(f"\n--- Body ---")
        print(m["body_email"])
        print(f"\n--- WhatsApp ---")
        print(m["whatsapp_short"])
        print(f"\n--- Call script (first 200 chars) ---")
        print(m["call_script"][:200] + "...")
