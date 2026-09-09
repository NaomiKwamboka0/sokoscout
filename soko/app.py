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
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
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
from soko.pipeline import JsonlStore, enrich, rollup

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

STYLE = """
:root {
  --ink: #14181f; --muted: #5b6472; --line: #e2e6ec; --bg: #ffffff;
  --accent: #0b6b3a; --warn: #8a5a00; --warn-bg: #fdf6e7; --card: #f7f8fa;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 780px; margin: 0 auto; padding: 32px 20px 64px; }
header { border-bottom: 1px solid var(--line); margin-bottom: 28px; padding-bottom: 16px;
  display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }
h1 { font-size: 20px; margin: 0; letter-spacing: -0.01em; }
h1 span { color: var(--muted); font-weight: 400; }
.tier { font-size: 13px; color: var(--muted); }
form.ask { display: flex; gap: 8px; margin: 0 0 28px; }
input[type=text], input[type=email], input[type=password] {
  flex: 1; padding: 11px 13px; border: 1px solid var(--line); border-radius: 7px;
  font-size: 15px; font-family: inherit; color: var(--ink); background: #fff;
}
input:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
button {
  padding: 11px 20px; border: 0; border-radius: 7px; background: var(--accent);
  color: #fff; font-size: 15px; font-weight: 500; cursor: pointer; font-family: inherit;
}
button:hover { background: #095a30; }
.answer { padding: 18px 20px; border: 1px solid var(--line); border-radius: 9px;
  margin-bottom: 16px; background: var(--card); }
.answer p { margin: 0 0 10px; }
.answer p:last-child { margin-bottom: 0; }
.answer .evidence { font-size: 13.5px; color: var(--muted); }
.refusal { border-color: #e8dcc0; background: var(--warn-bg); }
.refusal .label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--warn); font-weight: 600; margin-bottom: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 14.5px; margin-top: 8px; }
th { text-align: left; font-weight: 600; font-size: 12.5px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted); padding: 8px 10px; border-bottom: 1px solid var(--line); }
td { padding: 9px 10px; border-bottom: 1px solid var(--line); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.thin td { color: var(--muted); }
h2 { font-size: 15px; margin: 34px 0 10px; letter-spacing: -0.01em; }
.hint { font-size: 13.5px; color: var(--muted); margin: 0 0 24px; }
.hint code { background: var(--card); padding: 2px 6px; border-radius: 4px;
  font-size: 12.5px; font-family: ui-monospace, monospace; }
.examples { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 26px; }
.examples a { font-size: 13px; padding: 5px 11px; border: 1px solid var(--line);
  border-radius: 20px; color: var(--muted); text-decoration: none; }
.examples a:hover { border-color: var(--accent); color: var(--accent); }
.auth { max-width: 380px; margin: 60px auto; }
.auth form { display: flex; flex-direction: column; gap: 11px; }
.auth .error { color: #a12; font-size: 14px; margin: 0 0 6px; }
.foot { margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 13px; color: var(--muted); }
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


EXAMPLES = [
    "What do phone cases sell for on Jumia?",
    "Where should I sell, Jumia or Kilimall?",
    "What margin can I expect on power banks in Kisumu?",
    "Which categories are busiest?",
    "Where is there room in the market?",
    "When do I get paid on Jumia?",
    "What are the seller onboarding requirements?",
    "Can Jumia deliver to Mombasa?",
]


@app.get("/", response_class=HTMLResponse)
def home(session: str | None = Cookie(default=None)) -> HTMLResponse:
    if not session or hash_token(session) not in _SESSIONS:
        return RedirectResponse("/signin", status_code=303)

    account = current_account(session)
    results = _rollups()

    rows = []
    for (category, platform), figures in sorted(results.items()):
        if not account.may_see_platform(platform):
            continue
        if figures["available"]:
            rows.append(
                f"<tr><td>{html.escape(category.replace('_', ' '))}</td>"
                f"<td class=num>KSh {figures['median']}</td>"
                f"<td class=num>{figures['evidence']['observation_count']}</td>"
                f"<td class=num>{figures['evidence']['seller_count']}</td>"
                f"<td>{figures['saturation_label']}</td></tr>"
            )
        else:
            rows.append(
                f"<tr class=thin><td>{html.escape(category.replace('_', ' '))}</td>"
                f"<td class=num colspan=4>too thin — "
                f"{figures['observation_count']} of {figures['needed']} observations</td></tr>"
            )

    table = (
        "<table><tr><th>Category</th><th class=num>Median</th>"
        "<th class=num>Listings</th><th class=num>Sellers</th><th>Saturation</th></tr>"
        + "".join(rows) + "</table>"
        if rows else
        "<p class=hint>Nothing collected yet. Run <code>python -m soko collect</code>.</p>"
    )

    chips = "".join(
        f"<a href='/ask?q={html.escape(q)}'>{html.escape(q)}</a>" for q in EXAMPLES
    )

    return page("SokoScout", f"""
      <header>
        <h1>SokoScout <span>· Kenyan marketplace prices</span></h1>
        <div class=tier>{html.escape(account.email)} · {account.tier}</div>
      </header>
      <form class=ask action=/ask method=get>
        <input type=text name=q placeholder="Ask about a price, a category, a margin…" autofocus>
        <button type=submit>Ask</button>
      </form>
      <div class=examples>{chips}</div>
      <h2>What we have collected</h2>
      {table}
      <p class=foot>Every figure states the listings and sellers behind it.
      Where the data is too thin we say so rather than giving you a number.</p>
    """)


@app.get("/ask", response_class=HTMLResponse)
def ask_page(q: str = "", session: str | None = Cookie(default=None)) -> HTMLResponse:
    account = current_account(session)
    answer_html = _answer_block(q, account) if q.strip() else ""

    chips = "".join(
        f"<a href='/ask?q={html.escape(e)}'>{html.escape(e)}</a>" for e in EXAMPLES
    )

    return page("SokoScout", f"""
      <header>
        <h1><a href=/ style='color:inherit;text-decoration:none'>SokoScout</a></h1>
        <div class=tier>{html.escape(account.email)} · {account.tier}</div>
      </header>
      <form class=ask action=/ask method=get>
        <input type=text name=q value="{html.escape(q)}" autofocus>
        <button type=submit>Ask</button>
      </form>
      {answer_html}
      <div class=examples>{chips}</div>
    """)


def _answer_block(question: str, account: Account) -> str:
    payload = answer_question(question, account)

    if not payload["answered"]:
        return (
            f"<div class='answer refusal'><div class=label>Not answering that</div>"
            f"<p>{html.escape(payload['text'])}</p></div>"
        )

    # The answer engine already appends the citation to the text, because a
    # figure must never travel without its evidence and the CLI has nowhere
    # else to put it. Rendering the evidence dict again here printed it twice.
    #
    # So split the citation off the end of the text and show it once, in its
    # own muted element. Splitting rather than dropping keeps the rule that
    # evidence is inseparable from the figure: if the sentence is ever absent,
    # nothing is shown rather than the page inventing its own version.
    text = payload["text"]
    citation = ""

    marker = "Based on "
    index = text.rfind(marker)
    if index > 0:
        text, citation = text[:index].rstrip(), text[index:].strip()

    body = f"<p>{html.escape(text)}</p>"
    if citation:
        body += f"<p class=evidence>{html.escape(citation)}</p>"
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
    error_html = f"<p class=error>{html.escape(error)}</p>" if error else ""
    return page("Sign in · SokoScout", f"""
      <div class=auth>
        <h1>SokoScout</h1>
        <p class=hint>Kenyan marketplace prices, with the evidence attached.</p>
        {error_html}
        <form method=post action=/signin>
          <input type=email name=email placeholder="Email" required autofocus>
          <input type=password name=password placeholder="Password" required>
          <button type=submit>Sign in</button>
        </form>
        <p class=hint style='margin-top:16px'>
          No account? <a href=/signup>Start a 14 day free trial</a>.</p>
      </div>
    """)


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
    error_html = f"<p class=error>{html.escape(error)}</p>" if error else ""
    return page("Start a trial · SokoScout", f"""
      <div class=auth>
        <h1>Start a free trial</h1>
        <p class=hint>Fourteen days, no card. Then from KSh 200 a month.</p>
        {error_html}
        <form method=post action=/signup>
          <input type=email name=email placeholder="Email" required autofocus>
          <input type=password name=password placeholder="Password, at least 10 characters" required>
          <button type=submit>Start trial</button>
        </form>
        <p class=hint style='margin-top:16px'>
          Already have an account? <a href=/signin>Sign in</a>.</p>
      </div>
    """)


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
