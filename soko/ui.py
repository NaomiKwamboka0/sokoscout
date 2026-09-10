"""The vendor-facing pages.

Two tabs, because a vendor arrives with one of two things in mind:

  Compare    I know what I want to sell and where. Show me the platforms
             side by side and tell me where I keep the most.

  Ask Soko   I have a question in words. Answer it plainly.

Hand-written HTML and CSS, no framework and no build step. Nothing here needs
to be rebuilt to be deployed, which is why the predecessor still runs years
later.

Everything user-supplied or scraped is escaped through `e()`. Product titles
and seller names come off somebody else's website, so they are exactly the
kind of text that carries a script tag eventually.
"""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote


def e(value: Any) -> str:
    """Escape for HTML. Scraped text reaches these pages."""
    return html.escape(str(value if value is not None else ""))


STYLE = """
:root {
  --ink:#12161d; --muted:#606a7b; --faint:#8b94a3; --line:#e4e8ee;
  --bg:#fff; --panel:#f7f9fb; --accent:#0a6b3d; --accent-soft:#e8f3ed;
  --warn:#8a5a00; --warn-bg:#fdf7ea; --warn-line:#ecdcb8;
  --good:#0a6b3d; --shadow:0 1px 2px rgba(18,22,29,.05);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
.wrap{max-width:940px;margin:0 auto;padding:0 20px 72px}

header{display:flex;justify-content:space-between;align-items:baseline;
  gap:16px;padding:22px 0 0}
.brand{font-size:19px;font-weight:650;letter-spacing:-.02em}
.brand span{color:var(--muted);font-weight:400;font-size:14px}
.who{font-size:13px;color:var(--muted);display:flex;align-items:center;
  gap:12px;flex-wrap:wrap;justify-content:flex-end}
.who a{color:var(--muted)}
.market{display:flex;align-items:center;gap:6px;margin:0}
.market label{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--faint);font-weight:600}
.market select{padding:5px 9px;font-size:13px;border:1px solid var(--line);
  border-radius:7px;background:#fff;color:var(--ink);width:auto;cursor:pointer}

nav{display:flex;gap:2px;margin:20px 0 26px;border-bottom:1px solid var(--line)}
nav a{padding:9px 18px;font-size:14.5px;font-weight:500;text-decoration:none;
  color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--accent);border-bottom-color:var(--accent)}

.panel{background:var(--panel);border:1px solid var(--line);border-radius:11px;
  padding:18px;margin-bottom:22px}
.fields{display:grid;grid-template-columns:1.7fr 1.1fr auto;gap:11px;align-items:end}
@media(max-width:760px){.fields{grid-template-columns:1fr}}
.field{display:flex;flex-direction:column;gap:5px;min-width:0}
label{font-size:12px;font-weight:600;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted)}
input[type=text],input[type=email],input[type=password],select{
  padding:10px 12px;border:1px solid var(--line);border-radius:8px;
  font-size:14.5px;font-family:inherit;color:var(--ink);background:#fff;width:100%}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px;
  border-color:transparent}
button{padding:10px 22px;border:0;border-radius:8px;background:var(--accent);
  color:#fff;font-size:14.5px;font-weight:550;cursor:pointer;font-family:inherit;
  white-space:nowrap}
button:hover{background:#085530}

.picks{display:flex;flex-wrap:wrap;gap:14px;margin-top:13px;
  padding-top:13px;border-top:1px solid var(--line)}
.picks .lab{font-size:12px;font-weight:600;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted);padding-top:2px}
.pick{display:flex;align-items:center;gap:6px;font-size:13.5px;color:var(--muted)}
.pick input{accent-color:var(--accent);width:15px;height:15px}
.pick.off{color:var(--faint)}

.did{background:var(--warn-bg);border:1px solid var(--warn-line);
  border-radius:9px;padding:11px 15px;margin-bottom:18px;font-size:14px}
.did a{font-weight:600}

.verdict{background:var(--accent-soft);border:1px solid #cfe4d8;
  border-radius:10px;padding:15px 18px;margin-bottom:20px;font-size:15px}
.verdict .h{font-size:11.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:var(--accent);margin-bottom:5px}

.cols{display:grid;gap:14px;margin-bottom:8px;
  grid-template-columns:repeat(auto-fit,minmax(268px,1fr))}
.col{border:1px solid var(--line);border-radius:11px;overflow:hidden;
  box-shadow:var(--shadow);background:#fff}
.col.win{border-color:var(--accent);border-width:1.5px}
.col.thin{opacity:.85;background:var(--panel)}
.col h3{margin:0;padding:12px 16px;font-size:14.5px;background:var(--panel);
  border-bottom:1px solid var(--line);display:flex;justify-content:space-between;
  align-items:center;gap:8px}
.col.win h3{background:var(--accent-soft);color:var(--accent)}
.tag{font-size:10.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;padding:2px 7px;border-radius:9px;
  background:var(--accent);color:#fff}
.big{padding:15px 16px 6px;font-size:27px;font-weight:640;letter-spacing:-.02em}
.big small{font-size:13px;font-weight:400;color:var(--muted);
  letter-spacing:0;display:block;margin-top:3px}
.rows{padding:6px 16px 14px}
.row{display:flex;justify-content:space-between;gap:10px;padding:6px 0;
  font-size:13.5px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .k{color:var(--muted)}
.row .v{font-variant-numeric:tabular-nums;text-align:right}
.row.keep{border-top:1.5px solid var(--line);margin-top:4px;padding-top:9px;
  font-weight:640;border-bottom:0}
.row.keep .v{color:var(--good);font-size:15.5px}
.gap{padding:12px 16px;font-size:13.5px;color:var(--warn);
  background:var(--warn-bg);border-top:1px solid var(--warn-line)}
.src{padding:10px 16px;border-top:1px solid var(--line);font-size:12.5px;
  background:var(--panel)}
.src a{color:var(--muted);text-decoration:none;border-bottom:1px solid var(--line)}
.src a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.egs{padding:0 16px 13px;font-size:12.5px}
.egs .t{color:var(--faint);margin-bottom:5px;font-size:11.5px;
  text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.eg{display:flex;justify-content:space-between;gap:9px;padding:3px 0;color:var(--muted)}
.eg a{color:var(--muted);text-decoration:none}
.eg a:hover{color:var(--accent);text-decoration:underline}
.eg .p{font-variant-numeric:tabular-nums;white-space:nowrap}

.answer{border:1px solid var(--line);border-radius:11px;padding:17px 19px;
  margin-bottom:16px;background:var(--panel);font-size:15px}
.answer p{margin:0 0 9px}
.answer p:last-child{margin-bottom:0}
.answer .ev{font-size:13px;color:var(--muted)}
.answer.no{background:var(--warn-bg);border-color:var(--warn-line)}
.answer.no .h{font-size:11.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:var(--warn);margin-bottom:6px}
.answer .cite{margin-top:11px;padding-top:10px;border-top:1px solid var(--line);
  font-size:12.5px}
.answer .cite a{color:var(--muted)}

.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:6px}
.chips a{font-size:13px;padding:6px 12px;border:1px solid var(--line);
  border-radius:16px;color:var(--muted);text-decoration:none;background:#fff}
.chips a:hover{border-color:var(--accent);color:var(--accent)}
h2{font-size:15px;margin:30px 0 11px;letter-spacing:-.01em}
.note{font-size:13.5px;color:var(--muted);margin:0 0 20px}
.foot{margin-top:34px;padding-top:15px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--faint)}
table.plain{width:100%;border-collapse:collapse;font-size:14px}
table.plain th{text-align:left;font-size:11.5px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted);padding:8px 10px;
  border-bottom:1px solid var(--line);font-weight:600}
table.plain td{padding:9px 10px;border-bottom:1px solid var(--line)}
table.plain td.n{text-align:right;font-variant-numeric:tabular-nums}
table.plain tr.thin td{color:var(--faint)}
.auth{max-width:390px;margin:64px auto}
.auth form{display:flex;flex-direction:column;gap:11px;margin-top:16px}
.auth .err{color:#a1231f;font-size:14px;margin:0}
"""


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


def chrome(
    email: str,
    tier: str,
    tab: str,
    countries: list[dict[str, Any]] | None = None,
    country_code: str = "kenya",
) -> str:
    """Header, country selector and tab bar, shared by every page.

    Ask Soko sits first because it is where a vendor who does not yet know
    what to search goes, and burying it behind the comparison made it look
    like an afterthought.

    The country selector lives in the header rather than inside the search
    panel: it applies to the whole session, not to one query, and every
    figure below it is only true for the market selected.
    """
    def link(href: str, label: str, key: str) -> str:
        on = " class=on" if tab == key else ""
        return f"<a href='{href}'{on}>{label}</a>"

    picker = ""
    if countries:
        options = "".join(
            f"<option value='{e(c['code'])}'"
            f"{' selected' if c['code'] == country_code else ''}"
            f"{'' if c['active'] else ' disabled'}>"
            f"{e(c['name'])}{'' if c['active'] else ' — soon'}</option>"
            for c in countries
        )
        picker = (
            "<form class=market method=get action=/ id=mk>"
            "<label for=hdr-country>Selling in</label>"
            "<select id=hdr-country name=country "
            "onchange=\"document.getElementById('mk').submit()\">"
            f"{options}</select></form>"
        )

    return (
        f"<header><div class=brand>SokoScout "
        f"<span>&middot; marketplace prices, with the evidence</span></div>"
        f"<div class=who>{picker}<span>{e(email)} &middot; {e(tier)}</span>"
        f"<a href=# onclick=\"event.preventDefault();"
        f"document.getElementById('so').submit()\">sign out</a>"
        f"<form id=so method=post action=/signout hidden></form></div></header>"
        f"<nav>{link('/ask', 'Ask Soko', 'ask')}"
        f"{link('/', 'Compare prices', 'compare')}"
        f"{link('/policies', 'Fees &amp; rules', 'policies')}</nav>"
    )


def _money(symbol: str, value: str | None) -> str:
    if value is None:
        return "&mdash;"
    try:
        return f"{symbol} {float(value):,.0f}"
    except (TypeError, ValueError):
        return f"{symbol} {e(value)}"


def search_panel(
    categories: list[dict[str, str]],
    counties: list[dict[str, str]],
    countries: list[dict[str, Any]],
    platforms: list[dict[str, Any]],
    typed: str = "",
    county: str = "nairobi",
    country_code: str = "kenya",
    chosen: list[str] | None = None,
) -> str:
    """Product, place and platform pickers.

    The product is a free-text box with a datalist rather than a bare select:
    a vendor who knows they want power banks should be able to type it, and
    one who does not should be able to browse. The fuzzy matcher behind it
    means the typing does not have to be exact.
    """
    options = "".join(
        f"<option value='{e(c['label'])}'>" for c in categories
    )

    county_options = "".join(
        f"<option value='{e(c['code'])}'"
        f"{' selected' if c['code'] == county else ''}>{e(c['name'])}</option>"
        for c in counties
    )
    county_options = (
        f"<option value=''{'' if county else ' selected'}>All of Kenya</option>"
        + county_options
    )

    picked = set(chosen or [])
    ticks = []
    for p in platforms:
        checked = " checked" if (not picked or p["code"] in picked) else ""
        off = "" if p["has_data"] else " off"
        hint = "" if p["has_data"] else " title='We have no confirmed figures for this platform'"
        ticks.append(
            f"<label class='pick{off}'{hint}>"
            f"<input type=checkbox name=platform value='{e(p['code'])}'{checked}>"
            f"{e(p['name'])}{'' if p['has_data'] else ' (no data)'}</label>"
        )

    return f"""
    <form class=panel method=get action=/>
      <div class=fields>
        <div class=field>
          <label for=q>What are you selling?</label>
          <input type=text id=q name=q list=cats autocomplete=off
                 placeholder="power banks, phone cases, tv remote…"
                 value="{e(typed)}">
          <datalist id=cats>{options}</datalist>
        </div>
        <div class=field>
          <label for=county>Delivering to</label>
          <select id=county name=county>{county_options}</select>
        </div>
        <div class=field><button type=submit>Compare</button></div>
        <input type=hidden name=country value="{e(country_code)}">
      </div>
      <div class=picks>
        <span class=lab>Platforms</span>
        {''.join(ticks)}
      </div>
    </form>
    """


def did_you_mean(match: dict[str, Any], county: str, country_code: str) -> str:
    """Shown when the match was fuzzy, so a guess is never silent."""
    href = (
        f"/?q={quote(match['label'])}&county={quote(county)}"
        f"&country={quote(country_code)}"
    )
    return (
        f"<div class=did>Showing results for "
        f"<a href='{href}'>{e(match['label'])}</a>. "
        f"You typed &ldquo;{e(match['typed'])}&rdquo;.</div>"
    )


def comparison(result: dict[str, Any]) -> str:
    """The side-by-side columns."""
    symbol = result["currency"]
    columns = result["columns"]

    if not columns:
        return (
            "<div class=answer><p>No platforms to compare for that in "
            f"{e(result['country_label'])}.</p></div>"
        )

    best = result["verdict"].get("best")
    cards = []

    for col in columns:
        classes = ["col"]
        if col["comparable"] and col["platform"] == best:
            classes.append("win")
        if not col["comparable"]:
            classes.append("thin")

        tag = "<span class=tag>Best</span>" if (
            col["comparable"] and col["platform"] == best
        ) else ""

        if col["median_kes"]:
            headline = (
                f"<div class=big>{_money(symbol, col['median_kes'])}"
                f"<small>typical price &middot; {col['observations']} listings "
                f"from {col['sellers']} sellers</small></div>"
            )
        else:
            headline = "<div class=big>&mdash;<small>no price yet</small></div>"

        rows = ""
        if col["median_kes"] and not col["comparable"]:
            # A price but no commission, which is Kilimall today. Showing the
            # price alone is genuinely useful for judging where to position
            # against the market, so the column keeps it and says plainly
            # which half is missing rather than going blank.
            rows = (
                "<div class=rows>"
                "<div class=row><span class=k>Commission</span>"
                "<span class=v>not published</span></div>"
                f"<div class=row><span class=k>Delivery to "
                f"{e(result['county_label'])}</span>"
                f"<span class=v>{_money(symbol, col['delivery_kes'])}</span></div>"
                "<div class='row keep'><span class=k>You keep</span>"
                "<span class=v style='color:var(--muted);font-size:13.5px'>"
                "cannot say</span></div>"
                f"<div class=row><span class=k>Paid after</span>"
                f"<span class=v>{col['payout_days'] or '&mdash;'} days</span></div>"
                "</div>"
            )
        elif col["comparable"]:
            fees = "".join(
                f"<div class=row><span class=k>{e(f['name'])}</span>"
                f"<span class=v>&minus;{_money(symbol, f['amount_kes'])}</span></div>"
                for f in col["other_fees"]
            )
            rate = col["commission_rate"]
            rows = (
                f"<div class=rows>"
                f"<div class=row><span class=k>Commission"
                f"{f' ({rate:.0%})' if rate else ''}</span>"
                f"<span class=v>&minus;{_money(symbol, col['commission_kes'])}</span></div>"
                f"<div class=row><span class=k>Delivery to "
                f"{e(result['county_label'])}</span>"
                f"<span class=v>&minus;{_money(symbol, col['delivery_kes'])}</span></div>"
                f"{fees}"
                f"<div class='row keep'><span class=k>You keep</span>"
                f"<span class=v>{_money(symbol, col['net_receipt_kes'])}</span></div>"
                f"<div class=row><span class=k>Paid after</span>"
                f"<span class=v>{col['payout_days'] or '&mdash;'} days</span></div>"
                f"</div>"
            )

        examples = ""
        if col["examples"]:
            items = "".join(
                f"<div class=eg><a href='{e(x['url'])}' target=_blank rel=noopener>"
                f"{e(x['title'][:44])}</a>"
                f"<span class=p>{_money(symbol, x['price_kes'])}</span></div>"
                for x in col["examples"]
            )
            examples = f"<div class=egs><div class=t>Real listings</div>{items}</div>"

        gaps = ""
        if col["gaps"]:
            gaps = f"<div class=gap>{e(col['gaps'][0])}</div>"

        source = ""
        if col["source_url"]:
            collected = ""
            if col.get("collected_on"):
                collected = f" &middot; prices collected {e(col['collected_on'])}"
            source = (
                f"<div class=src>Fees from "
                f"<a href='{e(col['source_url'])}' target=_blank rel=noopener>"
                f"{e(col['platform_name'])} seller centre &nearr;</a>"
                f"{collected}</div>"
            )

        cards.append(
            f"<div class='{' '.join(classes)}'>"
            f"<h3>{e(col['platform_name'])}{tag}</h3>"
            f"{headline}{rows}{examples}{gaps}{source}</div>"
        )

    verdict_html = ""
    if result["verdict"]["text"]:
        verdict_html = (
            f"<div class=verdict><div class=h>Where you keep the most</div>"
            f"{e(result['verdict']['text'])}</div>"
        )

    return f"{verdict_html}<div class=cols>{''.join(cards)}</div>"
