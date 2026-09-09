"""Forgiving product and place lookup.

A vendor types "powerbanks", "power-bank", "pawa bank" or "POWER BANKS" and
means the same thing every time. A search that answers only the spelling we
happened to write in a config file is a search that tells a real user they
are wrong, which is not the product's job.

So matching runs in widening stages, cheapest and most certain first:

  1. exact, after normalising case, punctuation and spacing
  2. known alias, including plurals and local spellings
  3. substring, either direction
  4. edit distance, scaled to word length

Stage 4 is where "decorder" reaches "decoder" and "kilimal" reaches
"kilimall". It is deliberately last and deliberately bounded: a distance
threshold generous enough to catch a real typo is also generous enough to
match two unrelated short words, so short inputs get a tighter budget.

Nothing here guesses silently. Every result carries how it was matched and a
confidence, so the interface can say "showing power banks" when it is sure
and "did you mean power banks?" when it is not.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from soko.classify import category_index
from soko.normalize import taxonomy


# Words that carry no product meaning. Stripped before matching so "cheap
# power banks in nairobi" and "power bank" reach the same place.
STOPWORDS = frozenset({
    "a", "an", "the", "in", "on", "at", "for", "of", "to", "and", "or",
    "me", "my", "i", "we", "you", "is", "are", "do", "does", "what", "whats",
    "how", "much", "many", "cost", "costs", "price", "prices", "priced",
    "sell", "sells", "selling", "buy", "cheap", "cheapest", "best", "good",
    "please", "show", "tell", "find", "get", "want", "need", "looking",
    "kenya", "kenyan", "ksh", "kes", "shillings", "bob",
})


def fold(text: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    "Power-Bank!!"  ->  "power bank"
    "POWERBANKS"    ->  "powerbanks"

    Hyphens become spaces rather than vanishing, so "power-bank" folds to
    "power bank" and matches the two-word alias rather than the one-word one.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    spaced = re.sub(r"[-_/\\.,+&()\[\]{}'\"!?:;]+", " ", lowered)
    return re.sub(r"\s+", " ", spaced).strip()


def _collapse(text: str) -> str:
    """Fold and remove spaces, so "power bank" and "powerbank" are one key."""
    return fold(text).replace(" ", "")


def strip_stopwords(text: str) -> str:
    """Drop filler words, but never return nothing.

    "what do power banks cost" -> "power banks"
    "the best"                 -> "the best"   (all stopwords, so kept whole)
    """
    words = fold(text).split()
    kept = [w for w in words if w not in STOPWORDS]
    return " ".join(kept) if kept else fold(text)


def _singular(word: str) -> str:
    """Crude English singulariser, enough for product nouns.

    Deliberately not a stemmer. "batteries" -> "battery" and "cases" ->
    "case" is the whole job; over-stemming would collide unrelated words.
    """
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def singularise(text: str) -> str:
    return " ".join(_singular(w) for w in text.split())


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, abandoned early once it exceeds `cap`.

    The cap is what keeps this cheap: comparing a query against every alias
    in the taxonomy would otherwise be a full matrix each time, and we only
    ever care whether the distance is small.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best_in_row = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost, # substitution
            )
            current.append(value)
            best_in_row = min(best_in_row, value)
        if best_in_row > cap:
            return cap + 1
        previous = current
    return previous[-1]


def _budget(word: str) -> int:
    """How many typos to forgive in a word of this length.

    Short words get almost none: at distance 2, "case" reaches "cash", "cast"
    and "care", and a wrong match is worse than no match.
    """
    length = len(word)
    if length <= 4:
        return 1
    if length <= 7:
        return 2
    return 3


@dataclass(frozen=True)
class Match:
    """One resolved lookup, with how sure we are and why."""

    code: str
    label: str
    kind: str                 # "category" | "county" | "platform"
    how: str                  # "exact" | "alias" | "partial" | "fuzzy"
    score: float              # 1.0 exact, lower for looser matches
    typed: str

    @property
    def certain(self) -> bool:
        """Whether the interface can act without asking.

        A fuzzy match is shown as "did you mean", never applied silently. A
        vendor who typed something we half-recognised should see that we
        guessed, not discover it in the figures.
        """
        return self.how in {"exact", "alias"} or self.score >= 0.92

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "kind": self.kind,
            "how": self.how,
            "score": round(self.score, 3),
            "typed": self.typed,
            "certain": self.certain,
        }


def _index(pairs: Iterable[tuple[str, str, str]]) -> list[tuple[str, str, str, str]]:
    """(code, label, kind, searchable-alias) rows, expanded per alias."""
    rows: list[tuple[str, str, str, str]] = []
    for code, label, kind in pairs:
        aliases = {fold(code.replace("_", " ")), fold(label)}
        rows.extend((code, label, kind, a) for a in aliases if a)
    return rows


@functools.lru_cache(maxsize=1)
def category_targets() -> list[tuple[str, str, str, str]]:
    """Every searchable name for every category.

    Built from the taxonomy's own keywords rather than a second hand-written
    list, so adding a keyword makes it searchable in the same edit. That is
    the point of the taxonomy being the single source of truth.
    """
    rows: list[tuple[str, str, str, str]] = []
    for entry in taxonomy()["categories"]:
        code, label = entry["code"], entry["name"]
        aliases = {fold(code.replace("_", " ")), fold(label)}
        aliases.update(fold(k) for k in entry.get("keywords", []))
        rows.extend((code, label, "category", a) for a in aliases if a)
    return rows


@functools.lru_cache(maxsize=1)
def county_targets() -> list[tuple[str, str, str, str]]:
    from soko.logistics import counties

    return _index(
        (c["code"], c["name"], "county")
        for c in counties()
    )


@functools.lru_cache(maxsize=1)
def platform_targets() -> list[tuple[str, str, str, str]]:
    from soko.policies import known_platforms, policy

    rows: list[tuple[str, str, str, str]] = []
    for code in known_platforms():
        entry = policy(code) or {}
        label = entry.get("name", code)
        aliases = {
            fold(code.replace("_", " ")),
            fold(label),
            # "jumia_ke" should be reachable as plain "jumia".
            fold(code.split("_")[0]),
            fold(label.replace(" Kenya", "")),
        }
        rows.extend((code, label, "platform", a) for a in aliases if a)
    return rows


def _search(query: str, rows: list[tuple[str, str, str, str]]) -> Match | None:
    """Best match for one query against one target set."""
    typed = (query or "").strip()
    if not typed:
        return None

    folded = fold(typed)
    if not folded:
        return None

    trimmed = strip_stopwords(typed)
    collapsed = _collapse(typed)
    singular = singularise(trimmed)

    forms = [f for f in (folded, trimmed, singular) if f]

    # 1. Exact, on any normalised form.
    for form in forms:
        for code, label, kind, alias in rows:
            if alias == form:
                return Match(code, label, kind, "exact", 1.0, typed)

    # 2. Space-insensitive and plural-insensitive: "powerbanks" -> "power bank".
    for code, label, kind, alias in rows:
        alias_collapsed = alias.replace(" ", "")
        if alias_collapsed == collapsed:
            return Match(code, label, kind, "alias", 0.98, typed)
        if singularise(alias) == singular or _singular(alias_collapsed) == _singular(collapsed):
            return Match(code, label, kind, "alias", 0.96, typed)

    # 3. Containment, longest alias wins so "power bank" beats "bank".
    contained: list[tuple[int, str, str, str]] = []
    for code, label, kind, alias in rows:
        if len(alias) < 4:
            continue
        for form in forms:
            if alias in form or (len(form) >= 4 and form in alias):
                contained.append((len(alias), code, label, kind))
                break
    if contained:
        contained.sort(reverse=True)
        _, code, label, kind = contained[0]
        return Match(code, label, kind, "partial", 0.9, typed)

    # 4. Edit distance, last resort and tightly bounded.
    best: tuple[float, str, str, str] | None = None
    for code, label, kind, alias in rows:
        if not alias:
            continue
        for form in forms:
            budget = min(_budget(alias), _budget(form))
            distance = edit_distance(form, alias, cap=budget)
            if distance <= budget:
                longest = max(len(form), len(alias)) or 1
                score = 1.0 - (distance / longest)
                if best is None or score > best[0]:
                    best = (score, code, label, kind)

    if best and best[0] >= 0.6:
        score, code, label, kind = best
        return Match(code, label, kind, "fuzzy", score, typed)

    return None


def find_category(query: str) -> Match | None:
    return _search(query, category_targets())


def find_county(query: str) -> Match | None:
    return _search(query, county_targets())


def find_platform(query: str) -> Match | None:
    return _search(query, platform_targets())


def suggest_categories(query: str, limit: int = 5) -> list[Match]:
    """Near misses, for when nothing matched well enough to act on.

    A dead end that lists what we do have is a better experience than one
    that only says no.
    """
    typed = (query or "").strip()
    folded = fold(typed)
    if not folded:
        return []

    trimmed = strip_stopwords(typed)
    seen: dict[str, Match] = {}

    for code, label, kind, alias in category_targets():
        if code in seen or not alias:
            continue
        for form in (folded, trimmed):
            budget = max(_budget(alias), 2)
            distance = edit_distance(form, alias, cap=budget)
            if distance <= budget:
                longest = max(len(form), len(alias)) or 1
                seen[code] = Match(
                    code, label, kind, "fuzzy", 1.0 - distance / longest, typed
                )
                break

    ranked = sorted(seen.values(), key=lambda m: m.score, reverse=True)
    return ranked[:limit]


def all_categories() -> list[dict[str, str]]:
    """Every category, for populating a picker."""
    return [
        {"code": code, "label": entry["name"]}
        for code, entry in sorted(
            category_index().items(), key=lambda kv: kv[1]["name"]
        )
    ]
