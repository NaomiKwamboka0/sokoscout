"""The answer engine.

The single most important property of this module: no language model computes
a number. Questions are classified into an intent, routed to a fixed query,
and the figures the database returns are placed into a template. The number in
the sentence is the number the database returned, every time, because there is
no code path by which anything else could get there.

The routing is rules and patterns rather than a model, for the same reason the
classifier is: an intent that routes wrong is fixed by editing one line, and
the same question always routes the same way. A vendor who asks the same
question twice and gets two different answers stops trusting the product, and
they are right to.

What this gives up is stated plainly in the proposal: a rambling multi part
question is handled less gracefully than a frontier model would handle it.
The exchange is answers that are reproducible, auditable and free at the
margin. Where routing genuinely cannot place a question, the product says so
and offers the nearest thing it can answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class Intent(str, Enum):
    """Every question shape the engine can answer.

    Adding an intent means adding a query to the library and a template. It is
    deliberately a closed set: an open ended router that "figures out" what to
    do is the thing that produces confident nonsense.
    """

    PRICE_LEVEL = "price_level"          # what does X sell for
    SELLER_COUNT = "seller_count"        # how many sellers list X
    SATURATION = "saturation"            # how crowded is X
    TREND = "trend"                      # what has X's price done
    MARGIN = "margin"                    # what margin can I expect on X
    COMPARE = "compare"                  # X across platforms
    COMMISSION = "commission"            # what commission does X attract
    COVERAGE = "coverage"                # can this platform deliver to Y
    POLICY = "policy"                    # onboarding, terms, requirements
    COST_OF_BUSINESS = "cost_of_business"  # total cost to sell here vs there
    PAYOUT = "payout"                    # when do I get paid
    RETURNS = "returns"                  # returns policy and who pays
    ACTIVITY = "activity"                # what is busy / popular, honestly
    OPPORTUNITY = "opportunity"          # where is there room
    UNKNOWN = "unknown"


# Patterns are ordered: the first match wins. More specific intents are listed
# before more general ones, because "what margin can I expect on phone cases"
# contains a price question inside it and must not route to PRICE_LEVEL.
_ROUTES: list[tuple[Intent, re.Pattern[str]]] = [
    # Total cost of selling, across platforms. Placed first because it
    # contains words that would otherwise route to commission or compare, and
    # it is the more complete answer to what the vendor is asking.
    (Intent.COST_OF_BUSINESS, re.compile(
        r"\b(cost of (?:doing )?business|total cost|cost to sell|cheaper to sell"
        r"|where should i sell|which platform (?:is )?(?:cheaper|better|costs)"
        r"|what does it cost me|all the fees|total fees|take rate"
        r"|jumia (?:or|vs\.?|versus) kilimall)\b", re.I)),

    (Intent.PAYOUT, re.compile(
        r"\b(when (?:do|will) i get paid|payout|pay ?out|payment (?:cycle|terms)"
        r"|how (?:long|soon) (?:until|before|do) i (?:get|receive)"
        r"|cash cycle|settlement)\b", re.I)),

    (Intent.RETURNS, re.compile(
        r"\b(returns? policy|return window|who pays for (?:the )?return"
        r"|can (?:a )?(?:buyer|customer) return|refund policy)\b", re.I)),

    # "What is most searched in Nakuru" and its relatives. Caught here rather
    # than by the demand refusal below, because there IS an honest partial
    # answer: what sellers are listing. The answer says plainly that it is
    # assortment and not search volume.
    (Intent.ACTIVITY, re.compile(
        r"\b(most searched|most popular|what(?:'s| is) (?:hot|trending|busy|popular)"
        r"|busiest|most (?:listed|active)|what are people (?:buying|selling)"
        r"|top categor|biggest categor|most competitive categor"
        r"|where are (?:the )?sellers)\b", re.I)),

    # "Where is there room" reaches this in several shapes, so the pattern
    # covers the phrasings rather than one canonical form: is/'s there room,
    # room in the market, a gap, an opportunity. The first draft matched only
    # "where is the room" and left "where is there room in the market"
    # unroutable, which is a worse failure than a wrong route because the
    # vendor gets a shrug instead of an answer.
    (Intent.OPPORTUNITY, re.compile(
        r"\b(where(?:'s| is| are)?(?: there)?(?: (?:a|the|any))? "
        r"(?:room|gap|gaps|opportunit\w*|space)"
        r"|(?:room|gap|opportunit\w*|space) (?:in|for) the market"
        r"|least (?:crowded|saturated)|less (?:crowded|saturated)"
        r"|what should i sell|which categor(?:y|ies) should i"
        r"|best categor(?:y|ies) to (?:enter|start|sell)"
        r"|easiest to enter|underserved|not saturated)\b", re.I)),

    (Intent.MARGIN, re.compile(
        r"\b(margin|profit|markup|mark up|worth (?:it|selling)|make money"
        r"|landed cost|how much (?:can|will) i (?:make|earn))\b", re.I)),

    (Intent.TREND, re.compile(
        r"\b(trend|trending|over time|since|last (?:month|quarter|year|\d+ days)"
        r"|going up|going down|rising|falling|history|historical"
        r"|has the price|have prices)\b", re.I)),

    # Saturation is about a NAMED category: "is the earbuds category crowded".
    # The open question "where is there room" is OPPORTUNITY and is matched
    # earlier, so the overlapping phrases are deliberately not repeated here.
    (Intent.SATURATION, re.compile(
        r"\b(saturat\w*|crowded|competitive|competition|too many sellers"
        r"|oversupplied)\b", re.I)),

    (Intent.COMPARE, re.compile(
        r"\b(compare|comparison|versus|vs\.?|which platform|best platform"
        r"|jumia or kilimall|across platforms|platform to)\b", re.I)),

    (Intent.SELLER_COUNT, re.compile(
        r"\b(how many (?:sellers|vendors|shops|listings)|number of (?:sellers|listings)"
        r"|seller count|who else sells)\b", re.I)),

    (Intent.COMMISSION, re.compile(
        r"\b(commission|fee|fees|cut|percentage|what does jumia take"
        r"|charge me|take from)\b", re.I)),

    (Intent.COVERAGE, re.compile(
        r"\b(deliver to|delivery to|ship to|shipping to|collection point"
        r"|coverage|available in|do (?:you|they) cover|pickup station)\b", re.I)),

    (Intent.POLICY, re.compile(
        r"\b(onboard\w*|sign up|register|requirements?|documents?|terms"
        r"|how do i (?:start|become)|kyc|payout|pay on delivery|cash on delivery)\b", re.I)),

    (Intent.PRICE_LEVEL, re.compile(
        r"\b(price|cost|sell for|selling for|going for|how much|median"
        r"|average|typical|charge for|worth)\b", re.I)),
]


# Questions the product deliberately cannot answer, matched before routing.
# Each carries the reason, because "we observe listings and prices, not sales
# volume" is a much better answer than a shrug, and it teaches the vendor what
# the product is for.
_REFUSALS: list[tuple[re.Pattern[str], str]] = [
    # Genuine sales-volume questions, which nobody publishes and we refuse.
    #
    # Deliberately narrower than it first was. "Most popular" and "most
    # searched" used to be refused here too, but there is an honest partial
    # answer to those: what sellers are listing. Those now route to ACTIVITY,
    # which reports assortment and says in the sentence that assortment is
    # not search volume. A refusal that could have been a caveated answer is
    # a worse product, as long as the caveat is genuinely carried.
    #
    # What stays refused is anything asking how many units actually MOVED.
    # There is no honest partial answer to that at all.
    (re.compile(r"\b(units (?:sold|moved)|sales volume|sales figures|how many"
                r" (?:units )?(?:were )?(?:sold|sell per)|revenue for"
                r"|turnover for|conversion rate"
                # "What sells best in Kisumu" is the proposal's canonical
                # refusal and stays one. It asks which products MOVE, which
                # is sales data, not which products are listed. The nearby
                # ACTIVITY intent covers "most popular" and "most searched",
                # where reporting assortment is an honest partial answer;
                # "sells best" has no such reading.
                r"|sells? (?:the )?best|best[- ]?sell(?:er|ing)|top[- ]?sell(?:er|ing)"
                r"|what(?:'s| is) selling|moves? (?:the )?(?:most|fastest)"
                r"|fastest[- ]?moving)\b", re.I),
     "We observe listings and prices, not sales volume. Nobody publishes how "
     "many units moved, and inferring it from how many sellers list something "
     "would be a guess dressed up as a figure. What we can tell you is what a "
     "category costs, how many sellers are in it, how much stock they carry, "
     "and which way prices are moving. Ask which categories are busiest and "
     "we will tell you where sellers are concentrated, which is a real signal "
     "even though it is not the same thing."),

    (re.compile(r"\b(will|should) (?:i|we) (?:make|profit|succeed|do well)"
                r"|is it a good (?:idea|business)|guarantee\w*\b", re.I),
     "That is a forecast, and we do not make them. We can give you the market "
     "price, the commission, the delivery cost and the resulting landed cost "
     "ceiling, which is the arithmetic the decision rests on."),

    (re.compile(r"\b(competitor|rival)(?:'s)? (?:cost|margin|supplier|source)"
                r"|who supplies|where do they (?:buy|source)\b", re.I),
     "We do not have supplier or cost information for individual sellers. "
     "Only what they list publicly and what they charge for it."),
]


@dataclass
class Route:
    """Where a question was sent, and what was extracted from it."""

    intent: Intent
    category_code: str | None = None
    platform_code: str | None = None
    county_code: str | None = None
    window_days: int | None = None
    refusal: str | None = None
    matched_on: str | None = None

    @property
    def answerable(self) -> bool:
        return self.refusal is None and self.intent is not Intent.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "category": self.category_code,
            "platform": self.platform_code,
            "county": self.county_code,
            "window_days": self.window_days,
            "refusal": self.refusal,
            "matched_on": self.matched_on,
        }


_WINDOW_PATTERNS = [
    (re.compile(r"\blast (\d+)\s*days?\b", re.I), lambda m: int(m.group(1))),
    (re.compile(r"\blast (\d+)\s*weeks?\b", re.I), lambda m: int(m.group(1)) * 7),
    (re.compile(r"\blast (\d+)\s*months?\b", re.I), lambda m: int(m.group(1)) * 30),
    (re.compile(r"\blast quarter\b", re.I), lambda m: 90),
    (re.compile(r"\blast month\b", re.I), lambda m: 30),
    (re.compile(r"\blast week\b", re.I), lambda m: 7),
    (re.compile(r"\blast year\b", re.I), lambda m: 365),
    (re.compile(r"\bsince (?:june|jun)\b", re.I), lambda m: 90),
]


def route(
    question: str,
    known_categories: dict[str, list[str]] | None = None,
    known_platforms: dict[str, list[str]] | None = None,
    known_counties: dict[str, list[str]] | None = None,
) -> Route:
    """Classify one question into an intent and its parameters.

    The vocabularies are passed in rather than imported so the caller controls
    what exists. Parameters are matched against these enumerations and never
    taken as free text, which is what makes the downstream query library safe:
    nothing can write ad hoc SQL because nothing supplies an unvalidated
    value to it.
    """
    if not question or not question.strip():
        return Route(intent=Intent.UNKNOWN, refusal="Ask a question and I will try to answer it.")

    text = question.strip()

    # Refusals are checked first. A question about what sells best also
    # contains the word "sell", and routing it to a price answer would answer
    # a question the vendor did not ask.
    for pattern, reason in _REFUSALS:
        match = pattern.search(text)
        if match:
            return Route(intent=Intent.UNKNOWN, refusal=reason, matched_on=match.group(0))

    intent = Intent.UNKNOWN
    matched_on = None
    for candidate, pattern in _ROUTES:
        match = pattern.search(text)
        if match:
            intent, matched_on = candidate, match.group(0)
            break

    return Route(
        intent=intent,
        category_code=_match_vocabulary(text, known_categories or {}),
        platform_code=_match_vocabulary(text, known_platforms or {}),
        county_code=_match_vocabulary(text, known_counties or {}),
        window_days=_match_window(text),
        matched_on=matched_on,
        refusal=None if intent is not Intent.UNKNOWN else (
            "I could not work out what that question is asking for. I can "
            "answer questions about prices, seller counts, saturation, price "
            "trends, margins, commissions and delivery coverage."
        ),
    )


def _match_vocabulary(text: str, vocabulary: dict[str, list[str]]) -> str | None:
    """Find which known code a question refers to.

    Longest alias first, so "phone case" wins over "phone" when both are
    aliases of different categories.

    Plurals are matched without requiring every plural form to be listed as
    its own alias. Vendors write "what do phone cases cost" far more often
    than the singular, and an alias table that only matched the singular
    dropped the category on most real questions while looking like it worked:
    the intent still routed, so the failure showed up as an unexplained
    refusal rather than as an error.

    Only the regular suffixes are handled. An irregular plural belongs in the
    alias list, where it is visible, rather than in a stemming rule here.
    """
    lowered = text.lower()
    best: tuple[int, str] | None = None

    for code, aliases in vocabulary.items():
        for alias in aliases:
            escaped = re.escape(alias.lower())
            # Optional plural on the final word: case/cases, bank/banks,
            # accessory/accessories.
            if escaped.endswith("y"):
                pattern = rf"\b{escaped[:-1]}(?:y|ies)\b"
            else:
                pattern = rf"\b{escaped}(?:e?s)?\b"

            if re.search(pattern, lowered):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), code)
    return best[1] if best else None


def _match_window(text: str) -> int | None:
    for pattern, extract in _WINDOW_PATTERNS:
        match = pattern.search(text)
        if match:
            return extract(match)
    return None


# ---------------------------------------------------------------------------
# Answer assembly
# ---------------------------------------------------------------------------
#
# One template per intent. The figures are substituted in; nothing generates
# them. This is the mechanism by which the number in the sentence is provably
# the number the database returned.


def _evidence_sentence(evidence: dict[str, Any]) -> str:
    """The citation that travels with every figure.

    Assembled from the same result that produced the number, so a figure
    without its evidence is not a thing the system can emit.
    """
    count = evidence["observation_count"]
    sellers = evidence["seller_count"]
    platforms = ", ".join(evidence["platforms"])
    collected = evidence["collected_on"]

    parts = [f"{count:,} listings"]
    if sellers:
        parts.append(f"{sellers:,} sellers")
    sentence = f"Based on {' from '.join(parts)} on {platforms}, collected {collected}."

    for caveat in evidence.get("caveats") or []:
        sentence += f" {caveat}"
    return sentence


TEMPLATES: dict[Intent, Callable[[dict[str, Any]], str]] = {
    Intent.PRICE_LEVEL: lambda f: (
        f"The median price for {f['category_name']} on {f['platform_name']} is "
        f"KSh {f['median']}. Half of listings fall between KSh {f['p25']} and "
        f"KSh {f['p75']}."
    ),
    Intent.SELLER_COUNT: lambda f: (
        f"{f['seller_count']:,} distinct sellers list {f['category_name']} on "
        f"{f['platform_name']}, across {f['listing_count']:,} live listings."
    ),
    Intent.SATURATION: lambda f: (
        f"{f['category_name']} on {f['platform_name']} looks {f['label']}: "
        f"{f['listings_per_seller']} listings per seller, with prices spread "
        f"KSh {f['spread']} around a median of KSh {f['median']}."
    ),
    Intent.TREND: lambda f: (
        f"The median price for {f['category_name']} is {f['direction']}: "
        f"KSh {f['change_kes']} ({f['change_percent']}%) between "
        f"{f['from_date']} and {f['to_date']}."
    ),
    Intent.MARGIN: lambda f: (
        f"Median selling price for {f['category_name']} is KSh "
        f"{f['median_price']}. After the {f['commission_percent']}% category "
        f"commission and KSh {f['last_mile_kes']} last mile, a landed cost "
        f"under KSh {f['landed_cost_ceiling_kes']} keeps you above a "
        f"{f['target_percent']}% gross margin."
    ),
    Intent.COMMISSION: lambda f: (
        f"{f['category_name']} attracts a {f['commission_percent']}% commission "
        f"on {f['platform_name']}, per their published schedule read "
        f"{f['commission_read']}."
    ),
}


@dataclass
class Answer:
    """What the engine returns. Text, and the evidence under it."""

    text: str
    intent: Intent
    evidence: dict[str, Any] | None = None
    answered: bool = True
    refusal_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "intent": self.intent.value,
            "evidence": self.evidence,
            "answered": self.answered,
            "refusal_reason": self.refusal_reason,
        }


def refuse(reason: str, intent: Intent = Intent.UNKNOWN) -> Answer:
    return Answer(text=reason, intent=intent, answered=False, refusal_reason=reason)


def assemble(intent: Intent, figures: dict[str, Any], evidence: dict[str, Any]) -> Answer:
    """Build the answer text for an intent from computed figures.

    Raises if the intent has no template rather than falling back to
    generating prose about the figures. A missing template is a bug to fix in
    one place, not a case to paper over at runtime.
    """
    template = TEMPLATES.get(intent)
    if template is None:
        raise KeyError(
            f"No template for intent {intent.value}. Every answerable intent "
            f"needs one: this is the mechanism that keeps a model from "
            f"phrasing a figure."
        )

    body = template(figures)
    return Answer(
        text=f"{body} {_evidence_sentence(evidence)}",
        intent=intent,
        evidence=evidence,
        answered=True,
    )
