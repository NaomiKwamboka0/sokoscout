"""The web application.

Hand written HTML and CSS, no framework and no build step. This is a
deliberate operational choice carried over from the predecessor, which still
runs years later partly because there is nothing to rebuild.

Every route that returns data is behind authentication. There is no
"temporarily open for testing" route, because that is exactly how the
predecessor ended up with an open database on a public URL.
"""

from __future__ import annotations

import html
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from soko.answer import Intent, route
from soko.auth import (
    Account,
    AuthError,
    TIERS,
    gate_answer,
    hash_password,
    hash_token,
    new_session_token,
    session_expiry,
    start_trial,
    verify_password,
)
from soko.classify import taxonomy
from soko.compare import compare
from soko.pipeline import JsonlStore, enrich, rollup
from soko.search import all_categories, find_category, suggest_categories
from soko import logistics, markets, ui

app = FastAPI(title="SokoScout", docs_url=None, redoc_url=None)

import os

# Which collected observations to serve. Set SOKO_DATA to point at a different
# run without editing code, so a demo and a live crawl can be served from the
# same checkout.
DATA_FILE = Path(os.environ.get("SOKO_DATA", "data/run.jsonl"))

# Secure cookies are sent only over HTTPS, which is correct in production and
# makes the cookie invisible to a test client talking plain HTTP to an ASGI
# app in-process.
#
# This is a switch rather than a relaxation: it defaults to True, so the
# insecure setting has to be asked for explicitly and cannot be reached by
# forgetting to configure something. Nothing in the deployed path sets it.
# A Secure cookie is only sent over HTTPS, which is correct in production and
# means the session cookie is silently dropped when running on plain HTTP at
# localhost: you would sign in and land straight back on the sign-in page.
#
# Opt out explicitly with SOKO_INSECURE_COOKIE=1 for local viewing. It defaults
# to secure, so the unsafe setting has to be asked for and cannot be reached by
# forgetting to configure something.
COOKIE_SECURE = os.environ.get("SOKO_INSECURE_COOKIE") != "1"


def _set_session_cookie(response: Response, token: str) -> None:
    """Attach the session cookie with the flags that make it safe.

    One place, so the flags cannot drift between the signup and signin paths.
    HttpOnly means script cannot read it, so an XSS bug cannot lift a session.
    SameSite=lax means it is not sent on a cross-site POST.
    """
    response.set_cookie(
        "session",
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )

# In memory stores for the prototype. These are the two things that become
# Postgres tables first, and the interface is kept narrow so that swap is a
# small change rather than a rewrite.
_ACCOUNTS: dict[str, Account] = {}
_SESSIONS: dict[str, tuple[int, Any]] = {}
_NEXT_ID = [1]


def _rollups() -> dict[tuple[str, str], dict[str, Any]]:
    """Current figures, recomputed from the store.

    Recomputed per request in the prototype because the dataset is small and
    correctness matters more than latency here. This is the first thing to
    become a nightly rollup table when the catalogue grows.
    """
    store = JsonlStore(DATA_FILE)
    rows = list(enrich(store.read()))
    return rollup(rows) if rows else {}


def current_account(session: str | None = Cookie(default=None)) -> Account:
    """The signed in account, or 401.

    A dependency rather than a decorator so that forgetting it produces a
    route with no account argument, which fails loudly at import time rather
    than silently serving data to anyone.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    entry = _SESSIONS.get(hash_token(session))
    if not entry:
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.")

    account_id, _ = entry
    for account in _ACCOUNTS.values():
        if account.id == account_id:
            if not account.active():
                raise HTTPException(status_code=403, detail="This account is not active.")
            return account
    raise HTTPException(status_code=401, detail="Session expired. Sign in again.")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

EXAMPLES = [
    "When do I get paid on Jumia?",
    "What are the seller onboarding requirements?",
    "What is the returns policy?",
    "What margin can I expect on power banks in Kisumu?",
    "Which categories are busiest?",
    "Where is there room in the market?",
    "Can Jumia deliver to Mombasa?",
]


@app.get("/", response_class=HTMLResponse)
def home(
    q: str = "",
    county: str = "nairobi",
    country: str = "kenya",
    platform: list[str] = Query(default=[]),
    session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """The compare tab: pick a product and a place, see the platforms."""
    if not session or hash_token(session) not in _SESSIONS:
        return RedirectResponse("/signin", status_code=303)

    account = current_account(session)
    market = markets.country(country)

    pickers = ui.search_panel(
        categories=all_categories(),
        counties=logistics.counties(),
        countries=[c.as_dict() for c in markets.countries()],
        platforms=markets.all_platforms(market.code),
        typed=q,
        county=county,
        country_code=market.code,
        chosen=platform,
    )

    head = ui.chrome(
        account.email, account.tier, "compare",
        countries=[c.as_dict() for c in markets.countries()],
        country_code=market.code,
    )

    if not market.active:
        # A country we have not collected says so, rather than showing Kenyan
        # figures under another flag.
        body = (
            f"<div class=answer><p>We have not collected any "
            f"{ui.e(market.name)} data yet.</p>"
            f"<p class=ev>{ui.e(market.note)}</p></div>"
        )
        return HTMLResponse(ui.page("SokoScout", head + pickers + body))

    if not q.strip():
        return HTMLResponse(ui.page("SokoScout", head + pickers + _starting_point()))

    match = find_category(q)
    if not match:
        return HTMLResponse(ui.page("SokoScout", head + pickers + _no_match(q)))

    # A guess is shown as a guess. Silently answering about a different
    # product than the vendor typed is the failure this product exists to
    # avoid, and it would be invisible to them.
    hint = "" if match.certain else ui.did_you_mean(
        match.as_dict(), county, market.code
    )

    place = logistics.county(county) if county else None
    rows = list(enrich(JsonlStore(DATA_FILE).read()))
    allowed = [p for p in (platform or []) if account.may_see_platform(p)]

    result = compare(
        category_code=match.code,
        category_label=match.label,
        rows=rows,
        rollups=_rollups(),
        county_code=county or "nairobi",
        county_label=(place or {}).get("name") if place else "Kenya",
        country_code=market.code,
        chosen_platforms=allowed or None,
    )

    return HTMLResponse(ui.page(
        f"{match.label} - SokoScout",
        head + pickers + hint + ui.comparison(result),
    ))


def _starting_point() -> str:
    """Shown before a search, so the page is never blank."""
    chips = "".join(
        f"<a href='/?q={quote(c)}'>{ui.e(c)}</a>"
        for c in ("power banks", "phone cases", "tv remote", "earbuds", "laptop")
    )
    return (
        "<p class=note>Pick what you sell and where you deliver. We show each "
        "platform's typical price, what it takes in fees, and what you keep.</p>"
        f"<h2>Try one of these</h2><div class=chips>{chips}</div>"
    )


def _no_match(typed: str) -> str:
    """A dead end that shows what we do have.

    Listing the near misses is worth more than only saying no: a vendor who
    typed something we do not track can usually reach what they meant in one
    click.
    """
    near = suggest_categories(typed)
    if near:
        chips = "".join(
            f"<a href='/?q={quote(m.label)}'>{ui.e(m.label)}</a>" for m in near
        )
        return (
            f"<div class=did>We do not track &ldquo;{ui.e(typed)}&rdquo; yet. "
            f"Did you mean one of these?</div><div class=chips>{chips}</div>"
        )

    chips = "".join(
        f"<a href='/?q={quote(c['label'])}'>{ui.e(c['label'])}</a>"
        for c in all_categories()[:12]
    )
    return (
        f"<div class=did>We do not track &ldquo;{ui.e(typed)}&rdquo; yet, and we "
        f"would rather say so than show you a figure for something else.</div>"
        f"<h2>What we do track</h2><div class=chips>{chips}</div>"
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_page(
    q: str = "",
    country: str = "kenya",
    session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """The Ask Soko tab: questions in words."""
    account = current_account(session)
    answer = _answer_block(q, account) if q.strip() else ""

    chips = "".join(f"<a href='/ask?q={quote(x)}'>{ui.e(x)}</a>" for x in EXAMPLES)
    heading = "Other things to ask" if q.strip() else "Things you can ask"

    body = (
        "<form class=panel method=get action=/ask>"
        "<div class=fields style='grid-template-columns:1fr auto'>"
        "<div class=field>"
        "<label for=q>Ask anything about selling online in Kenya</label>"
        f"<input type=text id=q name=q value=\"{ui.e(q)}\" autofocus "
        "autocomplete=off placeholder=\"When do I get paid? "
        "What margin on power banks?\">"
        "</div>"
        "<div class=field><button type=submit>Ask</button></div>"
        "</div></form>"
        f"{answer}"
        f"<h2>{heading}</h2><div class=chips>{chips}</div>"
    )

    return HTMLResponse(ui.page(
        "Ask Soko - SokoScout",
        ui.chrome(
            account.email, account.tier, "ask",
            countries=[c.as_dict() for c in markets.countries()],
            country_code=country,
        ) + body,
    ))


@app.get("/policies", response_class=HTMLResponse)
def policies_page(
    country: str = "kenya",
    session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Every platform's rules, each linking to the documentation it came from."""
    account = current_account(session)

    from soko.policies import known_platforms, policy as platform_policy

    cards = []
    for code in known_platforms():
        entry = platform_policy(code) or {}
        if not account.may_see_platform(code):
            continue

        commission = entry.get("commission") or {}
        low, high = commission.get("range_low"), commission.get("range_high")
        band = (
            f"{low:.0%} to {high:.0%}"
            if low is not None and high is not None
            else "not confirmed"
        )
        payout = (entry.get("payout") or {}).get("cycle_days")
        onboarding = entry.get("onboarding") or {}
        returns = entry.get("returns") or {}

        requirements = "".join(
            f"<div class=row><span class=k>{ui.e(r)}</span></div>"
            for r in (onboarding.get("requirements") or [])
        ) or "<div class=row><span class=k>Not confirmed.</span></div>"

        source = ""
        if entry.get("source_url"):
            source = (
                f"<div class=src><a href='{ui.e(entry['source_url'])}' "
                f"target=_blank rel=noopener>Seller documentation &nearr;</a>"
                f" &middot; read {ui.e(entry.get('read', ''))}</div>"
            )

        cards.append(
            f"<div class=col><h3>{ui.e(entry.get('name', code))}</h3>"
            "<div class=rows>"
            f"<div class=row><span class=k>Commission</span>"
            f"<span class=v>{band}</span></div>"
            f"<div class=row><span class=k>Paid after</span>"
            f"<span class=v>{payout or '&mdash;'} days</span></div>"
            f"<div class=row><span class=k>Returns window</span>"
            f"<span class=v>{returns.get('window_days', '&mdash;')} days</span></div>"
            f"<div class=row><span class=k>Approval takes</span>"
            f"<span class=v>{onboarding.get('typical_days') or '&mdash;'} days</span>"
            "</div></div>"
            f"<div class=egs><div class=t>To sign up you need</div>"
            f"<div class=rows>{requirements}</div></div>"
            f"{source}</div>"
        )

    body = (
        "<p class=note>What each platform charges and requires. Every figure "
        "links to the seller documentation it came from, so you can check it "
        "yourself.</p>"
        f"<div class=cols>{''.join(cards)}</div>"
    )

    return HTMLResponse(ui.page(
        "Policies - SokoScout",
        ui.chrome(
            account.email, account.tier, "policies",
            countries=[c.as_dict() for c in markets.countries()],
            country_code=country,
        ) + body,
    ))


def _answer_block(question: str, account: Account) -> str:
    """One answer, with its evidence and a link to where it came from."""
    payload = answer_question(question, account)

    if not payload["answered"]:
        return (
            f"<div class='answer no'><div class=h>Not answering that</div>"
            f"<p>{ui.e(payload['text'])}</p></div>"
        )

    # The answer engine appends the citation to the text, because a figure
    # must never travel without its evidence and the CLI has nowhere else to
    # put it. Split it off here so it renders once, in its own muted line.
    text = payload["text"]
    citation = ""
    index = text.rfind("Based on ")
    if index > 0:
        text, citation = text[:index].rstrip(), text[index:].strip()

    body = f"<p>{ui.e(text)}</p>"
    if citation:
        body += f"<p class=ev>{ui.e(citation)}</p>"

    # Where a policy answered the question, link the documentation so the
    # vendor can check it rather than taking our word for it.
    source = payload.get("source_url")
    if source:
        body += (
            f"<div class=cite>Source: <a href='{ui.e(source)}' target=_blank "
            f"rel=noopener>{ui.e(payload.get('source_name', 'seller documentation'))}"
            f" &nearr;</a></div>"
        )

    return f"<div class=answer>{body}</div>"


# ---------------------------------------------------------------------------
# The answer endpoint, shared by the page and the API
# ---------------------------------------------------------------------------

def answer_question(question: str, account: Account) -> dict[str, Any]:
    """Route a question and answer it from collected data.

    Reuses the same routing and templates as the CLI. There is deliberately
    only one answer engine: a second one for the web would be a second place
    for a number to come from.
    """
    from soko.cli import _answer_from, _vocabularies

    categories, platforms, counties = _vocabularies()
    routed = route(question, categories, platforms, counties)

    if not routed.answerable:
        return {"answered": False, "text": routed.refusal, "intent": routed.intent.value}

    if routed.platform_code:
        gate = gate_answer(account, routed.platform_code)
        if gate:
            return {"answered": False, "text": gate, "intent": routed.intent.value}

    results = _rollups()
    if not results:
        return {
            "answered": False,
            "intent": routed.intent.value,
            "text": "Nothing has been collected yet, so there is nothing to answer from.",
        }

    # Catalogue-wide and policy answers: platform costs, payout, returns,
    # onboarding, and the honest version of "what is popular". These name no
    # single category, so they run before the category guard.
    if routed.intent in {
        Intent.COST_OF_BUSINESS, Intent.PAYOUT, Intent.RETURNS,
        Intent.POLICY, Intent.ACTIVITY, Intent.OPPORTUNITY,
    }:
        from soko.cli import _answer_catalogue_wide

        wide = _answer_catalogue_wide(routed, results)
        return {
            "answered": wide.get("answered", True),
            "text": wide["text"],
            "intent": routed.intent.value,
            # Policy answers know which document they came from. Carry it
            # through so the page can link it rather than asking the vendor
            # to take our word for a fee.
            "source_url": wide.get("source_url"),
            "source_name": wide.get("source_name"),
        }

    needs_category = routed.intent in {
        Intent.PRICE_LEVEL, Intent.SELLER_COUNT, Intent.TREND,
        Intent.MARGIN, Intent.COMMISSION,
    }
    if needs_category and routed.category_code is None:
        known = ", ".join(sorted(c.replace("_", " ") for c, _ in results))
        return {
            "answered": False,
            "intent": routed.intent.value,
            "text": (
                "I could not tell which product category that question is "
                "about, so I am not going to answer it with a figure from a "
                f"different one. Categories I have data for: {known}."
            ),
        }

    matches = [
        figures for (category, platform), figures in results.items()
        if (routed.category_code is None or category == routed.category_code)
        and (routed.platform_code is None or platform == routed.platform_code)
        and account.may_see_platform(platform)
    ]

    if not matches:
        return {
            "answered": False,
            "intent": routed.intent.value,
            "text": "Nothing collected yet for that. We only answer about what we have seen.",
        }

    usable = [m for m in matches if m["available"]]
    if not usable:
        thinnest = max(matches, key=lambda m: m["observation_count"])
        return {
            "answered": False,
            "intent": routed.intent.value,
            "text": (
                f"The data is too thin to answer that. We have "
                f"{thinnest['observation_count']} observations and need "
                f"{thinnest['needed']} before stating a figure."
            ),
        }

    best = max(usable, key=lambda m: m["evidence"]["observation_count"])
    answer = _answer_from(routed, best)

    return {
        "answered": answer.answered,
        "text": answer.text,
        "intent": answer.intent.value,
        "evidence": answer.evidence,
    }


@app.get("/api/ask")
def api_ask(q: str, account: Account = Depends(current_account)) -> JSONResponse:
    """The same answer as the page, as JSON. Premium tier only."""
    if account.tier != "premium":
        return JSONResponse(
            {"error": "API access is a Premium feature."}, status_code=403
        )
    return JSONResponse(answer_question(q, account))


# ---------------------------------------------------------------------------
# Sign up and sign in
# ---------------------------------------------------------------------------

@app.get("/signin", response_class=HTMLResponse)
def signin_page(error: str = "") -> HTMLResponse:
    err = f"<p class=err>{ui.e(error)}</p>" if error else ""
    return HTMLResponse(ui.page("Sign in - SokoScout", f"""
      <div class=auth>
        <div class=brand style='font-size:22px'>SokoScout</div>
        <p class=note>Kenyan marketplace prices, with the evidence attached.</p>
        {err}
        <form method=post action=/signin>
          <input type=email name=email placeholder="Email" required autofocus>
          <input type=password name=password placeholder="Password" required>
          <button type=submit>Sign in</button>
        </form>
        <p class=note style='margin-top:16px'>
          No account? <a href=/signup>Start a 14 day free trial</a>.</p>
      </div>
    """))


@app.post("/signin")
def signin(response: Response, email: str = Form(...), password: str = Form(...)):
    account = _ACCOUNTS.get(email.strip().lower())

    # Identical response whether or not the account exists. A login form that
    # answers differently for a real address is a customer enumeration
    # endpoint, and this product's customer list is commercially sensitive.
    if not account or not verify_password(account.password_hash, password):
        return RedirectResponse(
            "/signin?error=Those+details+did+not+match.", status_code=303
        )

    if not account.active():
        return RedirectResponse(
            "/signin?error=That+account+is+not+active.", status_code=303
        )

    token, stored = new_session_token()
    _SESSIONS[stored] = (account.id, session_expiry())

    redirect = RedirectResponse("/", status_code=303)
    _set_session_cookie(redirect, token)
    return redirect


@app.get("/signup", response_class=HTMLResponse)
def signup_page(error: str = "") -> HTMLResponse:
    err = f"<p class=err>{ui.e(error)}</p>" if error else ""
    return HTMLResponse(ui.page("Start a trial - SokoScout", f"""
      <div class=auth>
        <div class=brand style='font-size:22px'>Start a free trial</div>
        <p class=note>Fourteen days, no card. Then from KSh 200 a month.</p>
        {err}
        <form method=post action=/signup>
          <input type=email name=email placeholder="Email" required autofocus>
          <input type=password name=password
                 placeholder="Password, at least 10 characters" required>
          <button type=submit>Start trial</button>
        </form>
        <p class=note style='margin-top:16px'>
          Already have an account? <a href=/signin>Sign in</a>.</p>
      </div>
    """))


@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...)):
    address = email.strip().lower()
    if address in _ACCOUNTS:
        # Same redirect as a weak password, so this is not an enumeration
        # endpoint either.
        return RedirectResponse("/signup?error=Could+not+create+that+account.", 303)

    try:
        password_hash = hash_password(password)
    except AuthError as exc:
        return RedirectResponse(f"/signup?error={html.escape(str(exc))}", 303)

    account = Account(
        id=_NEXT_ID[0],
        email=address,
        password_hash=password_hash,
        # Verified immediately in the prototype. In production this waits for
        # the emailed link, which is why the flag exists rather than being
        # assumed true.
        email_verified=True,
        tier="trial",
        trial_ends_on=start_trial(),
    )
    _NEXT_ID[0] += 1
    _ACCOUNTS[address] = account

    token, stored = new_session_token()
    _SESSIONS[stored] = (account.id, session_expiry())

    redirect = RedirectResponse("/", status_code=303)
    _set_session_cookie(redirect, token)
    return redirect


@app.post("/signout")
def signout(session: str | None = Cookie(default=None)):
    if session:
        _SESSIONS.pop(hash_token(session), None)
    redirect = RedirectResponse("/signin", status_code=303)
    redirect.delete_cookie("session")
    return redirect


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness only. Deliberately exposes no data and needs no auth."""
    return {"ok": True}
