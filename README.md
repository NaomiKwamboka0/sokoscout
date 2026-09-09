# SokoScout

Kenyan marketplace price intelligence. It collects what products actually sell
for on Jumia and Kilimall, and answers vendors' questions about price levels,
saturation, commissions and margins — with the evidence attached to every
figure, and a refusal when the data is too thin to support one.

The commercial case is in [the proposal](proposal/proposal.html). How it is
built and why is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt

python -m soko categories                          # what it can classify
python -m soko collect --limit 20 --out data/run.jsonl
python -m soko stats  --in data/run.jsonl
python -m soko rollup --in data/run.jsonl
python -m soko ask "What do phone cases sell for on Jumia?" --in data/run.jsonl

python -m soko compare --price 2500 --category power_banks --county kisumu
python -m soko policies --platform jumia_ke
```

The web application:

```bash
python -m uvicorn soko.app:app --reload
```

No database required. Observations land in a JSONL file that is append-only
and idempotent per day, so reruns resume rather than duplicate.

`collect` hits a live marketplace. It is rate limited and identifies itself
per `config/platforms.yaml`, but it is still somebody else's server: keep the
limit modest.

---

## What it answers

Three kinds of question, and it is explicit about which kind it is answering.

**Prices, from collected listings.** What a category sells for, how many
sellers are in it, how crowded it is, what margin a landed cost supports.
Every figure states the listings and sellers behind it, and refuses below
thirty observations.

**Policies and the cost of doing business, from published documentation.**
Commissions, payout cycles, onboarding requirements, returns, and a
side-by-side of what it actually costs to sell the same product on each
platform. Ranked by what the vendor keeps rather than by headline commission,
because a lower commission with a real return cost is often the worse deal.

**What is busy, with the caveat attached.** A vendor asking "what is most
searched in Nakuru" is told plainly that no marketplace publishes search
volume, given what sellers are actually listing instead, and told that
assortment is a weaker signal that can equally mean saturation. The
county part is answered honestly too: listing data is national, and what
genuinely changes by county is delivery cost, so that is what it gives.

---

## What the answers look like

```
$ python -m soko ask "What do phone cases sell for on Jumia?"
The median price for phone accessories on Jumia Kenya is KSh 726.00. Half of
listings fall between KSh 475.50 and KSh 911.25. Based on 60 listings from 23
sellers on jumia_ke, collected 2026-09-09.

$ python -m soko ask "What is the price of beauty products?"
The data is too thin to answer that. We have 12 observations and need 30
before stating a figure.

$ python -m soko ask "What sells best in Kisumu?"
We observe listings and prices, not sales volume. Nobody publishes how many
units moved, and inferring it from how many sellers list something would be a
guess dressed up as a figure. What we can tell you is what a category costs,
how many sellers are in it, how much stock they carry, and which way prices
are moving. Ask which categories are busiest and we will tell you where
sellers are concentrated, which is a real signal even though it is not the
same thing.

$ python -m soko ask "What margin can I expect on power banks in Kisumu?"
Median selling price for power banks is KSh 3016.00. After the 11.0% category
commission and KSh 260 last mile, a landed cost under KSh 1519.44 keeps you
above a 30% gross margin. Based on 45 listings from 22 sellers on jumia_ke,
collected 2026-09-09. Last mile is for Kisumu.

$ python -m soko ask "Where should I sell, Jumia or Kilimall?"
On a KSh 1000 sale, Kilimall Kenya leaves you the most: KSh 755.00 after an 8%
commission and KSh 160 delivery. That is KSh 17.50 per unit more than Jumia
Kenya. Jumia Kenya pays faster though, on a 14 day cycle against 15. Caveat:
commission is the midpoint of Kilimall Kenya's published 5% to 12% band, not a
category rate. Not included, for lack of confirmed figures: Glovo, Uber Eats.

$ python -m soko compare --price 2500 --category power_banks --county kisumu
  Platform          Commission  Delivery    Other   You keep    Take  Payout
  --------------------------------------------------------------------------
  Kilimall Kenya        275.00    280.00     0.00   1,945.00  22.2%     15d
  Jumia Kenya           275.00    260.00    27.50   1,937.50  22.5%     14d
  Glovo Kenya                                        not comparable
  Uber Eats                                          not comparable

Kilimall Kenya leaves you the most [...] That is KSh 7.50 per unit more than
Jumia Kenya. Treat that lead as unproven: the gap is KSh 7.50 and Kilimall
Kenya has an unconfirmed return handling fee, which is worth up to KSh 20
elsewhere. The ranking could flip once that figure is known.

$ python -m soko ask "What is the most searched product in Nakuru?"
By seller activity, the busiest categories we track are phone accessories,
audio, power banks. [...] Our listing data is national, not per county:
marketplace listings do not say where a buyer is. What does change with Nakuru
is delivery, at KSh 250 to the door against KSh 145 to a pickup station, on a
3 day cycle. That is the figure that actually moves your margin there. This is
what sellers are listing, not what buyers are searching for. No marketplace
publishes search volume.
```

Most of those are refusals or carry a caveat. That is the product working, not
failing. On the first live run of 39 real listings it stated zero figures and
refused all twelve categories, because none had reached thirty observations.

---

## Layout

```
sokoscout/
├─ config/
│  ├─ platforms.yaml      access policy, verified against live robots.txt
│  ├─ taxonomy.yaml       22 categories of Kenyan retail vocabulary
│  ├─ counties.yaml       coverage to ward level
│  ├─ logistics.yaml      last mile cost per county, with the date read
│  └─ policies.yaml       commissions, payouts, onboarding, returns
├─ soko/
│  ├─ normalize.py        price and title parsing. The correctness boundary.
│  ├─ classify.py         listing → category, by explicit rules
│  ├─ aggregate.py        median, spread, saturation, trend, margin
│  ├─ answer.py           question routing and templated answers
│  ├─ policies.py         platform policies and cost-of-business comparison
│  ├─ demand.py           what is busy, and what that does and does not mean
│  ├─ logistics.py        delivery coverage and cost
│  ├─ auth.py             accounts, sessions, tier gating
│  ├─ app.py              the web application
│  ├─ pipeline.py         storage, enrichment, rollup
│  ├─ cli.py              the commands above
│  └─ sources/
│     ├─ base.py          driver contract and collection discipline
│     └─ jumia.py         Jumia Kenya, sitemap driven
├─ tests/                 389 tests, no network access
└─ schema.sql             Postgres schema for when JSONL stops being enough
```

---

## Editing the taxonomy

Classification is keyword rules in `config/taxonomy.yaml`, not a model. A
wrong tag is fixed by editing one line, and the fix is live on the next run.

```yaml
- code: power_banks
  name: Power Banks and Charging
  commission_rate: 0.11
  commission_src: "Jumia Kenya seller commission schedule"
  commission_read: 2026-09-09
  keywords:
    - power bank
    - powerbank
    - portable charger    # one product, four names, one category
```

When adding keywords, the thing to guard against is false positives. A bare
`bag` matches "storage bag" and "sleeping bag"; a bare `remote` matches
"remote control car". A missing keyword only shrinks the sample. A wrong one
corrupts a median that somebody prices stock against.

---

## Tests

```bash
python -m pytest tests/ -q
```

389 tests, none of which touch the network. The suite deliberately covers the
failure paths harder than the happy ones: a collector that parses a good page
is easy, a collector that notices it has been served a challenge page with a
200 status is the one worth having.

---

## Status

Built and tested: collection with a live-verified Jumia driver, classification
across 22 categories, aggregation with refusal thresholds, question routing,
margin and coverage answers, platform policy and cost-of-business comparison,
the activity view, authentication with tier gating, and the web application.

Not built yet: the Kilimall and Glovo drivers, price trends (the series starts
the day collection starts, so this needs elapsed time rather than code),
Paystack billing, and email verification delivery.

The gate that matters most is not automatable and has not been done: before
any vendor sees a figure, pull thirty at random and check them against the
live site by hand.
