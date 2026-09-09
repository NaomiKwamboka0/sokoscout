# Architecture

Technical specification for the SokoScout prototype. The commercial case is in
[the proposal](../proposal/proposal.html); this document is how it is built and
why.

**Scope:** Jumia Kenya first, Kilimall second. Python throughout.
**The one rule everything else follows:** no model ever computes a number.

---

## Pipeline

```
  config/platforms.yaml     access policy, verified against live robots.txt
  config/taxonomy.yaml      Kenyan retail vocabulary, as explicit rules
  config/counties.yaml      coverage, down to ward
  config/logistics.yaml     last mile cost, dated
        │
        ├──► sources/jumia.py        sitemap → JSON-LD → Listing
        └──► sources/<platform>.py   same contract, new file
                    │
                    ▼
             JsonlStore / raw_listing     append only, idempotent per day
                    │
                    ▼
             classify.py                  keyword rules, confidence banded
                    │
                    ▼
             aggregate.py                 median, spread, saturation, trend
                    │                     REFUSES below 30 observations
                    ▼
             answer.py                    route → fixed template → cited answer
                    │
                    ▼
             cli.py / app.py              the vendor asks a question here
```

---

## The three properties that matter

Everything in this codebase serves one of three properties. When a change
seems to improve the product but breaks one of these, it is the wrong change.

### 1. No model computes a number

Questions are classified into a closed set of intents by regular expression,
routed to a fixed query, and the resulting figures are placed into a template
by string substitution. `answer.py` has no path by which generated text can
become a figure, because the templates are the only way to produce answer text
and they take their values as parameters.

The cost is real and is accepted: a rambling multi-part question is handled
less gracefully than a frontier model would handle it. What is bought is that
the same question always produces the same answer, and that every number is
traceable to the query that computed it.

### 2. Every figure carries its evidence

`summarise_prices()` returns a `PriceSummary` that contains an `Evidence`
object. There is no constructor that produces one without the other, so a
figure without its citation is not representable. The evidence names the
observation count, the distinct seller count, the platforms, the collection
date, and any platform caveat.

### 3. It refuses rather than guessing

Three separate refusals, each a control flow rather than a convention:

| Refusal | Where | Why |
|---|---|---|
| Below 30 observations | `aggregate.py` | A median over eleven listings is not a market rate |
| Unrecognised category | `cli.py` | Found in testing: it answered about a *different* category |
| Missing commission or last-mile | `aggregate.py` | A guessed input is a wrong answer about money |

Plus the question-level refusals in `answer.py`: demand questions ("what sells
best"), forecasts ("will I make money"), and competitor cost questions. Each
refusal explains what the product *can* answer, because a refusal that teaches
is worth more than one that stops.

---

## Collection discipline

Three rules enforced in `sources/base.py` rather than left to each driver.

**An empty result set raises.** Anti-bot systems signal throttling by
returning nothing successfully. A collector without this check looks perfectly
healthy while collecting zero rows, and nobody notices until somebody asks why
the median stopped moving. `guard_not_empty()` makes it loud.

**A block stops the run.** A 403, a 429, or a challenge page served with HTTP
200 raises `BlockedError` and abandons the run. Continuing to request after a
platform has said stop turns a temporary throttle into a permanent ban.

**Reruns resume rather than restart.** Storage is unique on
`(platform, source_key, date)`, so re-running a day writes nothing new and
running the next day extends the series.

### What running it live actually taught us

The proposal says to run a collector against the real site before building on
it. Doing that on day one found four things that were invisible in fixtures:

1. **Jumia's robots explicitly permits scraping** under conditions we already
   met: identify as a bot, carry a contact URL, stay under 200 requests per
   minute. We take about 40.

2. **The policy is query-parameter based, not path-prefix based.** The first
   draft of `platforms.yaml` guessed prefixes and would have missed nearly
   every rule they actually wrote, while feeling thorough.

3. **`/catalog/` is not blanket disallowed.** Two endpoints under it are
   explicitly allowed, and they are exactly the specifications and ratings we
   want.

4. **Child sitemaps are gzipped and brand sitemaps sort first.** A plain text
   read of a `.xml.gz` finds no `<loc>` and looks exactly like an empty
   catalogue. And walking the index in order spends the whole limit on brand
   sitemaps, whose URLs robots disallows, collecting nothing.

A fifth was found by a test rather than a run: the facet check has to run
*before* the allow list, or `/phones.html?color=black` matches the `/*.html`
allow and the crawler walks into the facet URLs their file spends two hundred
lines asking us not to touch.

---

## Classification

`classify.py`, driven by `config/taxonomy.yaml`. Deliberately rules and not a
model, for two reasons that outweigh accuracy on an average case: a wrong tag
is fixed by editing one line of YAML by somebody who is not an engineer, and
the same input always produces the same output.

Confidence is banded rather than scored. Only `high` and `medium` listings
enter a published figure; `low` and `none` are stored and counted but never
move a number a vendor reads today. Because the raw payload is stored, a better
rule tomorrow reclassifies from stored data without re-crawling anybody.

There is deliberately **no fallback category**. An unclassified listing is
excluded from every aggregate, because a listing we cannot identify contributes
only noise to a median, and a wrong bucket is worse than a smaller sample.

### The accessory guard

Keyword matching alone cannot tell an item apart from an accessory *for* that
item. The live run produced the canonical case:

```
"Universal Charging Plug For Kids Toy Cars"  ->  toys_games
```

It matched the toys keyword `kids toy`, because that phrase sits inside "Kids
Toy Cars". It is a charger, and it put a 2,499 shilling charger into the toys
median. The adversarial review of the taxonomy predicted this exact failure
and it happened anyway, because no keyword list can express "not the thing
itself".

So there is a second, explicit pass. `accessory_markers` in the taxonomy maps
words like `charger`, `case for`, `cover for` to the category that actually
owns them. A listing carrying one is reassigned. Categories whose main product
*is* the accessory are exempt, or the correction would be the mirror image of
the bug. A redirected listing is capped at medium confidence and records
`classified_by: accessory_guard`, so the correction is visible rather than
silent.

The guard also rescues listings no keyword matched at all: "12V Charger For
Kids Ride On Car Battery" matches no category, but is unambiguously a charger.
That is a positive identification from a different rule, not a fallback.

### What the taxonomy expansion found

The original eleven categories classified **0 of 12** listings on the first
live run, because the sitemap is ordered by recency rather than category and
returns a slice of the whole catalogue. Eleven more categories were drafted
against real titles and then passed through an adversarial review instructed
to refute keywords rather than approve them. That pass rejected **114 of 370**
proposed keywords for false-positive risk.

Eight of its rejections were themselves wrong, on a premise that was checked
and found false: the reviewer believed `electronics_home` owned a bare `tv`
keyword and rejected every TV-prefixed term to avoid a collision.
`electronics_home` actually uses `smart tv` and `led tv`, so no collision
existed. Those eight were restored, which matters because "Tv remote" is the
single most common item in the live sample.

Classification went from 0% to 82% on real data, with no re-crawl, because the
raw payloads were stored.

---

## The three question families

The router splits questions into three families, and which family a question
lands in determines what kind of evidence its answer carries.

**Catalogue questions** (price, seller count, saturation, trend, margin) are
answered from collected observations. Evidence is an observation count, a
seller count and a collection date. These refuse below thirty observations.

**Policy questions** (commission, payout, onboarding, returns, cost of doing
business) are answered from `config/policies.yaml`. Evidence is a document
source and the date it was read, because that is what provenance means for a
published fee schedule. These refuse when a figure is unconfirmed rather than
estimating it.

**Activity questions** ("what is popular", "where is there room") are the
delicate ones, and `soko/demand.py` exists mainly to keep a distinction:

> We can see what sellers **list**. We cannot see what buyers **search for**.

No marketplace publishes search volume. A vendor asking "what is most searched
in Nakuru" is given assortment concentration instead, and told in the same
sentence that it is assortment and not search volume, and that a crowded
category can equally mean saturation. The disclaimer is a module constant
attached to every answer and asserted in the tests, so a caller cannot drop it.

The county half of that question is handled the same way. Marketplace listings
carry no county, so the answer says the listing data is national and offers
what genuinely does vary by county: delivery cost, which really does move the
margin.

`"What sells best in Kisumu?"` stays refused, because it asks what **moved**,
and there is no honest partial answer to that at all.

---

## Cost of doing business

`soko/policies.py`. The comparison a vendor needs is not "which platform has
the lowest commission", which is what they ask, but "what does it cost me in
total to sell this here versus there", which is what they mean.

So `platform_cost()` itemises rather than collapsing: commission, last mile,
a provision for returns at a stated assumed rate, and the payout cycle. The
ranking is by what the vendor keeps, not by commission rate, because those
orders genuinely differ. A platform with a lower commission and a real return
handling fee can be the more expensive place to sell.

Where a figure is unconfirmed the platform is listed under `not_comparable`
with the reason, rather than dropped. A vendor who cannot find a platform
assumes the tool is broken; one who is told why it was excluded does not.

---

## Scoring and saturation

Saturation is listings per distinct seller, weighted by how tightly sellers
have converged on a price. The second term is what separates a large category
from a crowded one: forty sellers with wide dispersion are still finding the
market, while forty sellers within a few shillings of each other have already
competed the margin away.

Returns `None` below three distinct sellers. One shop with four hundred
listings is not a saturated category.

---

## Verification

| What | How | Status |
|---|---|---|
| Price parsing | Unit tests over real forms: `KSh 1,299`, `1,299/=`, `1.5k`, `2500 bob`, plus near-misses `4.5` and `999,999,999` | Passing |
| Classification | The four-names case (power bank / powerbank / portable charger / phone charger) plus Kenyan vocabulary (mtumba, sufuria, kabambe, ngoma) | Passing |
| Block detection | Challenge page served with HTTP 200 must raise | Passing |
| Empty guard | An empty harvest must raise, not pass | Passing |
| Robots compliance | Facets, seller and brand pages refused; the allowed catalog endpoints permitted | Passing |
| Refusal thresholds | Thin categories, unknown categories, missing margin inputs | Passing |
| Idempotence | Rerunning a day writes nothing; the next day extends the series | Passing |
| Accessory guard | A charger for a toy is a charger; the toy is still a toy | Passing |
| Assortment disclaimer | No activity answer can be read as search volume | Passing |
| Cost comparison | Ranks by what the vendor keeps; unconfirmed platforms named | Passing |
| Auth | No open routes, no account enumeration, tier gating on both paths | Passing |
| **Live collection** | **Real runs against Jumia, inspecting what came back** | **Done — see above** |
| Figure precision | Hand-check 30 published figures against the live site | Not yet |

389 tests, none touching the network.

The last row is the gate that matters most and it is not automatable. Before
any vendor sees a figure, pull thirty at random and check them against the
site by hand.

### What the second live run showed

40 pages collected, 27 new rows written and 13 correctly recognised as already
stored, which is idempotence working in production rather than in a fixture.
82% classified across 12 categories.

And then the part worth stating plainly: on 39 real listings it published
**zero figures** and refused all twelve categories, because none had reached
thirty observations. A tool willing to quote a median off twelve TV remotes is
exactly the tool this one is built not to be.

---

## Authentication

Built before the web application rather than after it, because the
predecessor's cautionary tale is precisely that it was written to run on a
laptop and then deployed to the internet with every route open.

- **argon2id** for passwords, and `hash_password` raises rather than falling
  back to a general purpose hash if argon2 is missing. A silent downgrade to
  SHA-256 would produce a system that appears to work and stores passwords
  badly.
- **Server-held sessions**, not a self-contained token. A lapsed subscription
  must revoke access immediately, and a self-contained token cannot be revoked
  before it expires without a server-side list anyway. Only the token's hash is
  stored, so a leaked store does not hand over live sessions.
- **Identical responses** whether or not an account exists, on both signin and
  signup. This product's customer list is commercially sensitive, and a form
  that answers differently for a real address is an enumeration endpoint.
- **Pseudonymous question log.** What a vendor asks reveals what they are about
  to invest in, so the log carries a hash, never the email address.
- `COOKIE_SECURE` defaults to `True` and a test asserts that default in the
  module source, so the insecure setting cannot be reached by forgetting to
  configure something.

---

## What is deliberately not built

- **No Postgres in the prototype path.** `JsonlStore` implements the same
  interface. The fastest way to find out whether collected data is any good is
  to collect some and look at it, not to stand up a database first.
- **In-memory accounts.** The auth logic is real and tested; the storage
  behind it is two dicts. Those are the first things to become tables.
- **No trend answers yet.** The series starts the day collection starts, so
  this needs elapsed time rather than code. The product says so rather than
  extrapolating from two points.
- **No Kilimall or Glovo driver.** Their policies and fees are in
  `config/policies.yaml` so the cost comparison works, but nothing is collected
  from them yet.
- **No Uber Eats.** It returns 403 to non-browser requests and is bot
  protected. Declining to crawl it is the whole policy, and it appears in the
  comparison as explicitly not comparable rather than being silently absent.
- **No search volume, ever, unless it is bought.** Google Trends is a
  legitimate future source for genuine search interest. Until it is collected,
  the product reports assortment and says that is what it is.
