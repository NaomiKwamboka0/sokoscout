"""Command line entry point.

Every command prints a JSON summary on its last line, so a scheduler or an
n8n Execute Command node can parse the outcome without scraping human readable
output. The convention is carried over from the predecessor, where it worked.

    python -m soko collect --limit 50 --out data/run.jsonl
    python -m soko rollup --in data/run.jsonl
    python -m soko ask "what do phone cases sell for on jumia"
    python -m soko stats --in data/run.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from soko.answer import Answer, Intent, assemble, refuse, route
from soko.classify import commission_rate, taxonomy
from soko.pipeline import JsonlStore, enrich, rollup, summarise_run
from soko.sources.base import BlockedError, EmptyResultError, guard_not_empty

app = typer.Typer(add_completion=False, help="SokoScout: Kenyan marketplace price intelligence.")

DEFAULT_OUT = "data/run.jsonl"


def _emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    """The last line of every command: one JSON object, nothing after it."""
    print(json.dumps(payload, default=str))
    raise typer.Exit(exit_code)


def _vocabularies() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    """Enumerations the router validates question parameters against.

    Built from the same config the collector uses, so a category the product
    can answer about is by construction a category we collect.
    """
    import yaml
    from soko.sources.base import CONFIG_DIR, platform_config

    # Each category is addressable by its keywords, by its full display name,
    # and by the individual significant words of that name. Without the last
    # of these, "Beauty and Personal Care" is only reachable by typing the
    # whole phrase, and a vendor asking about "beauty" matches nothing. That
    # failure is quiet rather than loud, which is what makes it dangerous.
    stopwords = {"and", "or", "the", "of", "for", "personal", "care", "home"}
    categories = {}
    for entry in taxonomy()["categories"]:
        name = entry["name"].lower()
        aliases = {name, *entry["keywords"]}
        aliases.update(
            word for word in name.replace("'", "").split()
            if len(word) > 3 and word not in stopwords
        )
        categories[entry["code"]] = sorted(aliases)
    platforms = {
        entry["code"]: [entry["name"].lower(), entry["name"].split()[0].lower()]
        for entry in platform_config()["platforms"]
    }
    with open(CONFIG_DIR / "counties.yaml", encoding="utf-8") as fh:
        counties_cfg = yaml.safe_load(fh)
    counties = {
        entry["code"]: [entry["name"].lower()] for entry in counties_cfg["counties"]
    }
    return categories, platforms, counties


@app.command()
def collect(
    platform: str = typer.Option("jumia_ke", help="Platform code to collect from."),
    limit: int = typer.Option(50, help="Maximum product pages to fetch."),
    out: str = typer.Option(DEFAULT_OUT, help="JSONL file to append observations to."),
    allow_empty: bool = typer.Option(
        False,
        help="Permit a run that collects nothing. Only for testing a driver "
             "against a deliberately narrow scope.",
    ),
) -> None:
    """Collect listings from a platform and append them to the store.

    Hits a live marketplace. Rate limited and identified per config, but it is
    still somebody else's server: keep the limit modest unless there is a
    reason not to.
    """
    if platform == "jumia_ke":
        from soko.sources.jumia import JumiaDriver
        driver = JumiaDriver()
    else:
        _emit(
            {
                "ok": False,
                "error": f"No driver for platform {platform!r}. Built: jumia_ke.",
            },
            exit_code=1,
        )
        return

    store = JsonlStore(out)
    listings = []

    try:
        for listing in driver.collect(limit=limit):
            listings.append(listing)
    except BlockedError as exc:
        driver.close()
        _emit({"ok": False, "error": "blocked", "detail": str(exc), **driver.stats.as_dict()}, 2)
        return
    except KeyboardInterrupt:
        # A partial run is still worth keeping: the observations already
        # collected are real, and the next run resumes rather than restarting.
        pass

    try:
        if not allow_empty:
            guard_not_empty(listings, platform, f"collect --limit {limit}")
    except EmptyResultError as exc:
        driver.close()
        _emit({"ok": False, "error": "empty", "detail": str(exc), **driver.stats.as_dict()}, 3)
        return

    written = store.write(listings)
    stats = driver.stats.as_dict()
    driver.close()

    _emit({
        "ok": True,
        "command": "collect",
        "collected": len(listings),
        "written": written,
        "duplicates_skipped": len(listings) - written,
        "store": str(Path(out).resolve()),
        **stats,
    })


@app.command("rollup")
def rollup_command(
    in_: str = typer.Option(DEFAULT_OUT, "--in", help="JSONL store to aggregate."),
    minimum: int = typer.Option(
        None,
        help="Observation threshold. Below this a category is reported as too "
             "thin rather than given a figure. Lower it only for inspection.",
    ),
    show_thin: bool = typer.Option(True, help="List categories that fell below the threshold."),
) -> None:
    """Aggregate stored observations into per category figures."""
    store = JsonlStore(in_)
    rows = list(enrich(store.read()))

    if not rows:
        _emit({"ok": False, "error": "empty_store", "store": in_}, 1)
        return

    results = rollup(rows, minimum=minimum)
    available = {k: v for k, v in results.items() if v["available"]}
    thin = {k: v for k, v in results.items() if not v["available"]}

    for (category, platform), figures in sorted(available.items()):
        typer.echo(
            f"{category:<22} {platform:<14} "
            f"median KSh {figures['median']:>10} "
            f"n={figures['evidence']['observation_count']:<5} "
            f"sellers={figures['evidence']['seller_count']:<4} "
            f"{figures['saturation_label']}"
        )

    if show_thin and thin:
        typer.echo("")
        typer.echo("Too thin to state a figure:")
        for (category, platform), info in sorted(thin.items()):
            typer.echo(
                f"  {category:<22} {platform:<14} "
                f"{info['observation_count']} of {info['needed']} observations"
            )

    _emit({
        "ok": True,
        "command": "rollup",
        "categories_with_figures": len(available),
        "categories_too_thin": len(thin),
        **summarise_run(rows),
    })


@app.command()
def ask(
    question: str = typer.Argument(..., help="A question, in plain language."),
    in_: str = typer.Option(DEFAULT_OUT, "--in", help="JSONL store to answer from."),
    minimum: int = typer.Option(None, help="Override the observation threshold."),
) -> None:
    """Answer a question from the collected catalogue.

    No question ever triggers a live crawl. Answers come from what has already
    been collected, which is why an answer is fast and why no vendor's
    curiosity can get us rate limited.
    """
    categories, platforms, counties = _vocabularies()
    routed = route(question, categories, platforms, counties)

    if not routed.answerable:
        typer.echo(routed.refusal)
        _emit({
            "ok": True,
            "command": "ask",
            "answered": False,
            "reason": routed.refusal,
            **routed.as_dict(),
        })
        return

    store = JsonlStore(in_)
    rows = list(enrich(store.read()))
    if not rows:
        message = "Nothing has been collected yet, so there is nothing to answer from."
        typer.echo(message)
        _emit({"ok": False, "command": "ask", "answered": False, "reason": message}, 1)
        return

    results = rollup(rows, minimum=minimum)

    # A question that names no category we recognise must refuse, not answer
    # about whichever category happens to have the most data.
    #
    # This was a real bug and it is the exact failure the whole product is
    # built to avoid: "what is the price of beauty products", with beauty
    # unmatched, returned the phone accessories median in a confident sentence
    # with a citation attached. A wrong number carrying real evidence is worse
    # than no answer, because the evidence is what makes it believable.
    #
    # Intents that are inherently about a single product category require one.
    # Saturation is the exception: "which category is least saturated" is a
    # legitimate question across all of them.
    # Intents answered from the whole catalogue and the policy files rather
    # than from one category's figures. Handled before the category guard,
    # because "where should I sell" names no category and should not be
    # refused for that.
    if routed.intent in {
        Intent.COST_OF_BUSINESS, Intent.PAYOUT, Intent.RETURNS,
        Intent.POLICY, Intent.ACTIVITY, Intent.OPPORTUNITY,
    }:
        answer = _answer_catalogue_wide(routed, results)
        typer.echo(answer["text"])
        _emit({
            "ok": True, "command": "ask",
            "answered": answer.get("answered", True),
            "intent": routed.intent.value,
        })
        return

    needs_category = routed.intent in {
        Intent.PRICE_LEVEL,
        Intent.SELLER_COUNT,
        Intent.TREND,
        Intent.MARGIN,
        Intent.COMMISSION,
    }

    if needs_category and routed.category_code is None:
        known = ", ".join(sorted(c.replace("_", " ") for c in
                                 {cat for cat, _ in results}))
        message = (
            "I could not tell which product category that question is about, "
            "so I am not going to answer it with a figure from a different "
            f"one. Categories I have data for: {known or 'none yet'}."
        )
        typer.echo(message)
        _emit({"ok": True, "command": "ask", "answered": False,
               "reason": "category_not_recognised", **routed.as_dict()})
        return

    # Narrow to what the question asked about. An unspecified platform means
    # every platform we have, which is the honest reading of "what does this
    # cost" with no platform named.
    matches = [
        figures for (category, platform), figures in results.items()
        if (routed.category_code is None or category == routed.category_code)
        and (routed.platform_code is None or platform == routed.platform_code)
    ]

    if not matches:
        message = (
            f"Nothing collected yet for that combination"
            f"{f' ({routed.category_code})' if routed.category_code else ''}. "
            f"The catalogue only answers about what it has seen."
        )
        typer.echo(message)
        _emit({"ok": True, "command": "ask", "answered": False, "reason": message,
               **routed.as_dict()})
        return

    usable = [m for m in matches if m["available"]]
    if not usable:
        thinnest = max(matches, key=lambda m: m["observation_count"])
        message = (
            f"The data is too thin to answer that. We have "
            f"{thinnest['observation_count']} observations and need "
            f"{thinnest['needed']} before stating a figure."
        )
        typer.echo(message)
        _emit({"ok": True, "command": "ask", "answered": False, "reason": message,
               **routed.as_dict()})
        return

    # Most evidence wins when a question spans platforms.
    best = max(usable, key=lambda m: m["evidence"]["observation_count"])
    answer = _answer_from(routed, best)

    typer.echo(answer.text)
    _emit({
        "ok": True,
        "command": "ask",
        "answered": answer.answered,
        "intent": answer.intent.value,
        "evidence": answer.evidence,
    })


def _answer_catalogue_wide(routed, results: dict) -> dict[str, Any]:
    """Answers drawn from the policy files and the whole catalogue.

    These do not belong to one category's figures, so they bypass the
    category guard. Each returns a dict with `text` rather than an Answer,
    because their evidence is a document read on a date rather than an
    observation count.
    """
    from decimal import Decimal

    from soko.demand import activity_answer, opportunity_answer
    from soko.policies import (
        compare_platforms,
        onboarding_answer,
        payout_answer,
        returns_answer,
    )

    platform = routed.platform_code or "jumia_ke"

    if routed.intent is Intent.PAYOUT:
        return payout_answer(platform)

    if routed.intent is Intent.RETURNS:
        return returns_answer(platform)

    if routed.intent is Intent.POLICY:
        return onboarding_answer(platform)

    if routed.intent is Intent.ACTIVITY:
        county = routed.county_code
        return activity_answer(
            results,
            county_code=county,
            platform_code=routed.platform_code,
        )

    if routed.intent is Intent.OPPORTUNITY:
        return opportunity_answer(results, platform_code=routed.platform_code)

    if routed.intent is Intent.COST_OF_BUSINESS:
        # The comparison needs a price to work from. Use the named category's
        # median where there is one, so the figures are about a real product
        # rather than an abstract sale.
        price = Decimal(1000)
        category = routed.category_code
        if category:
            for (cat, plat), figures in results.items():
                if cat == category and figures.get("available"):
                    price = Decimal(figures["median"])
                    break

        comparison = compare_platforms(
            sale_price=price,
            category_code=category,
            county_code=routed.county_code or "nairobi",
        )
        if "text" not in comparison:
            return {
                "answered": False,
                "text": (
                    "We do not have enough confirmed fee and delivery data to "
                    "compare platforms on that yet."
                ),
            }

        text = comparison["text"]
        if comparison.get("not_comparable"):
            skipped = ", ".join(p["platform_name"] for p in comparison["not_comparable"])
            text += f" Not included, for lack of confirmed figures: {skipped}."
        return {"answered": True, "text": text}

    return {"answered": False, "text": "That is not built yet."}


def _answer_from(routed, figures: dict[str, Any]):
    """Fill the template for this intent from computed figures.

    Every value passed in came out of the aggregation layer. Nothing here
    computes anything: this function moves numbers into a sentence.
    """
    from soko.sources.base import platform_entry

    category_name = figures["category_code"].replace("_", " ")
    platform_name = platform_entry(figures["platform_code"])["name"]
    evidence = figures["evidence"]

    if routed.intent is Intent.PRICE_LEVEL:
        return assemble(Intent.PRICE_LEVEL, {
            "category_name": category_name,
            "platform_name": platform_name,
            "median": figures["median"],
            "p25": figures["p25"],
            "p75": figures["p75"],
        }, evidence)

    if routed.intent is Intent.SELLER_COUNT:
        return assemble(Intent.SELLER_COUNT, {
            "category_name": category_name,
            "platform_name": platform_name,
            "seller_count": evidence["seller_count"],
            "listing_count": evidence["observation_count"],
        }, evidence)

    if routed.intent is Intent.SATURATION:
        from decimal import Decimal
        spread = Decimal(figures["p75"]) - Decimal(figures["p25"])
        sellers = evidence["seller_count"] or 1
        return assemble(Intent.SATURATION, {
            "category_name": category_name,
            "platform_name": platform_name,
            "label": figures["saturation_label"],
            "listings_per_seller": round(evidence["observation_count"] / sellers, 1),
            "spread": spread,
            "median": figures["median"],
        }, evidence)

    if routed.intent is Intent.COMMISSION:
        rate = commission_rate(figures["category_code"])
        if rate is None:
            return refuse(
                f"We do not have a published commission rate for "
                f"{category_name}. Stating a guess would be a wrong answer "
                f"about money.",
                Intent.COMMISSION,
            )
        entry = next(
            e for e in taxonomy()["categories"] if e["code"] == figures["category_code"]
        )
        return assemble(Intent.COMMISSION, {
            "category_name": category_name,
            "platform_name": platform_name,
            "commission_percent": round(rate * 100, 1),
            "commission_read": entry["commission_read"],
        }, evidence)

    if routed.intent is Intent.MARGIN:
        from decimal import Decimal
        from soko.aggregate import MarginInputs, landed_cost_ceiling
        from soko.logistics import delivery_cost

        # A margin is always against a place, because last mile is where the
        # cost differences live. With no county named, Nairobi is the default
        # and the answer says so rather than leaving it implied.
        county = routed.county_code or "nairobi"
        cost = delivery_cost(figures["platform_code"], county)

        result = landed_cost_ceiling(MarginInputs(
            median_price=Decimal(figures["median"]),
            commission_rate=commission_rate(figures["category_code"]),
            last_mile_kes=cost.door_delivery if cost else None,
            sources={
                "commission": "published category schedule",
                "last_mile": cost.source if cost else "unavailable",
            },
        ))
        if not result["available"]:
            return refuse(result["reason"], Intent.MARGIN)

        answer = assemble(Intent.MARGIN, {
            "category_name": category_name,
            "median_price": result["median_price"],
            "commission_percent": round(result["commission_rate"] * 100, 1),
            "last_mile_kes": result["last_mile_kes"],
            "landed_cost_ceiling_kes": result["landed_cost_ceiling_kes"],
            "target_percent": int(result["target_margin"] * 100),
        }, evidence)

        # An unviable category is a real answer and must not be buried under a
        # ceiling figure that reads like an opportunity.
        if not result["viable"]:
            answer.text += (
                f" That ceiling is at or below zero, which means at this "
                f"market price there is no landed cost that reaches a "
                f"{int(result['target_margin'] * 100)}% margin."
            )
        if cost:
            answer.text += f" Last mile is for {cost.county_name}."
        return answer

    if routed.intent is Intent.COVERAGE:
        from soko.logistics import coverage_answer

        if not routed.county_code:
            return refuse(
                "Which county? Delivery cost and coverage vary enough between "
                "them that a national answer would be misleading.",
                Intent.COVERAGE,
            )
        result = coverage_answer(figures["platform_code"], routed.county_code)
        if not result["available"]:
            return refuse(result["reason"], Intent.COVERAGE)
        return Answer(text=result["text"], intent=Intent.COVERAGE, answered=True)

    if routed.intent is Intent.TREND:
        return refuse(
            "We do not have enough days of history for that yet. The series "
            "starts the day collection starts and deepens from there.",
            Intent.TREND,
        )

    return refuse(
        f"That routes to {routed.intent.value}, which is not built yet.",
        routed.intent,
    )


@app.command()
def stats(
    in_: str = typer.Option(DEFAULT_OUT, "--in", help="JSONL store to inspect."),
) -> None:
    """What has been collected so far. Does not crawl anything."""
    store = JsonlStore(in_)
    rows = list(enrich(store.read()))

    if not rows:
        typer.echo(f"Nothing collected yet in {in_}.")
        _emit({"ok": True, "command": "stats", "observations": 0})
        return

    summary = summarise_run(rows)
    typer.echo(f"Observations:        {summary['observations']:,}")
    typer.echo(f"Classified:          {summary['classified']:,} "
               f"({summary['classification_rate']:.0%})")
    typer.echo(f"Aggregatable:        {summary['aggregatable']:,}")
    typer.echo("")
    typer.echo("By category:")
    for category, count in list(summary["by_category"].items())[:15]:
        typer.echo(f"  {category:<24} {count:>6,}")

    _emit({"ok": True, "command": "stats", **summary})


@app.command("compare")
def compare_command(
    price: float = typer.Option(1000.0, help="Sale price in KSh to compare at."),
    category: str = typer.Option(None, help="Category code, for its commission rate."),
    county: str = typer.Option("nairobi", help="County, for the last mile cost."),
) -> None:
    """What it costs to sell the same product on each platform.

    Ranked by what you keep, not by commission rate. Those orders differ, and
    the difference is the whole point: a platform with a lower commission and
    a real return cost can be the more expensive place to sell.
    """
    from decimal import Decimal

    from soko.policies import compare_platforms

    result = compare_platforms(
        sale_price=Decimal(str(price)),
        category_code=category,
        county_code=county,
    )

    typer.echo(f"On a KSh {price:,.0f} sale in {county.replace('_', ' ').title()}:")
    typer.echo("")
    typer.echo(f"  {'Platform':<16} {'Commission':>11} {'Delivery':>9} "
               f"{'Other':>8} {'You keep':>10} {'Take':>7} {'Payout':>7}")
    typer.echo("  " + "-" * 74)

    for entry in result["platforms"]:
        other = sum(float(f["amount_kes"]) for f in entry["other_fees"])
        payout = f"{entry['payout_days']}d" if entry["payout_days"] else "-"
        typer.echo(
            f"  {entry['platform_name']:<16} "
            f"{float(entry['commission_kes']):>11,.2f} "
            f"{float(entry['last_mile_kes']):>9,.2f} "
            f"{other:>8,.2f} "
            f"{float(entry['net_receipt_kes']):>10,.2f} "
            f"{entry['take_rate']:>6.1%} "
            f"{payout:>7}"
        )

    for entry in result["not_comparable"]:
        typer.echo(f"  {entry['platform_name']:<16} {'not comparable':>48}")

    if result.get("text"):
        typer.echo("")
        typer.echo(result["text"])

    # Anything unconfirmed is stated rather than buried, because a vendor
    # comparing platforms on a fabricated fee is worse off than one told we
    # do not know.
    caveats = {note for p in result["platforms"] for note in p["unconfirmed"]}
    if caveats:
        typer.echo("")
        typer.echo("Caveats:")
        for note in sorted(caveats):
            typer.echo(f"  - {note}")

    _emit({
        "ok": True,
        "command": "compare",
        "compared": len(result["platforms"]),
        "not_comparable": len(result["not_comparable"]),
        "best": result.get("best"),
    })


@app.command()
def policies(
    platform: str = typer.Option("jumia_ke", help="Platform code."),
) -> None:
    """Onboarding, payout and returns policy for one platform."""
    from soko.policies import onboarding_answer, payout_answer, returns_answer

    for label, answer in (
        ("Onboarding", onboarding_answer(platform)),
        ("Payout", payout_answer(platform)),
        ("Returns", returns_answer(platform)),
    ):
        if not answer.get("available", True):
            typer.echo(f"{label}: {answer.get('reason')}")
            continue
        typer.echo(f"{label}:")
        typer.echo(f"  {answer['text']}")
        typer.echo("")

    _emit({"ok": True, "command": "policies", "platform": platform})


@app.command()
def categories() -> None:
    """List the categories the classifier knows, with commission rates."""
    entries = taxonomy()["categories"]
    for entry in entries:
        rate = entry.get("commission_rate")
        rate_text = f"{rate * 100:.0f}%" if rate else "unknown"
        typer.echo(f"{entry['code']:<22} {entry['name']:<28} commission {rate_text:>8}  "
                   f"{len(entry['keywords'])} keywords")

    _emit({"ok": True, "command": "categories", "count": len(entries)})


def main() -> None:
    try:
        app()
    except Exception as exc:  # noqa: BLE001 - last resort, must still emit JSON
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
