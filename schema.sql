-- SokoScout catalogue schema.
--
-- Three layers, and the separation between them is the point:
--
--   raw_listing        append only. What the collector saw, unparsed.
--   price_observation  the time series. One row per product per platform per
--                      day. This is the asset that cannot be acquired
--                      retroactively, so it is written on the very first run.
--   category_rollup    nightly aggregates. Every number the answer engine
--                      states comes from here or from a query over
--                      price_observation. No number is ever computed by a model.
--
-- Postgres 16. Explicit SQL, no ORM: the query layer is the correctness
-- boundary of this product and it should be readable by a person checking it.

-- ---------------------------------------------------------------------------
-- Enumerations. Parameters to the answer engine's query library are validated
-- against these, so a question can never inject free text into a statement.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS platform (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    -- 'product' means we see individual product prices. 'category' means we
    -- only see category level ranges. 'manual' means a human maintains a
    -- dated fact table because we do not crawl it at all.
    granularity     TEXT NOT NULL
                    CHECK (granularity IN ('product', 'category', 'manual')),
    -- Surfaced in the product next to every number sourced from this platform.
    -- A vendor about to spend money can tell the difference between 1,200
    -- observations and 40, and hiding it is how a tool like this loses trust.
    caveat          TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS county (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    tier            SMALLINT NOT NULL,
    -- Whether any platform can actually collect or deliver here. A vendor must
    -- never be shown an encouraging answer about a county with no logistics.
    has_logistics   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS category (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_code     TEXT REFERENCES category(code),
    -- Published commission rate for this category, as a fraction. Sourced from
    -- the platform's own schedule, with the date it was read.
    commission_rate NUMERIC(5,4),
    commission_src  TEXT,
    commission_read DATE
);

-- ---------------------------------------------------------------------------
-- Layer 1: raw landing zone
-- ---------------------------------------------------------------------------

-- Append only. Everything the collector fetched, before any parsing decision.
-- Unique on (platform, source_key, collected_on) so a rerun on the same day is
-- idempotent and a rerun tomorrow adds a new observation rather than
-- overwriting yesterday's. Storing the payload means extraction can be
-- improved and re-run over data already collected, without re-crawling.
CREATE TABLE IF NOT EXISTS raw_listing (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform_code   TEXT NOT NULL REFERENCES platform(code),
    source_key      TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    collected_on    DATE NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    http_status     INTEGER,
    payload         JSONB NOT NULL,
    UNIQUE (platform_code, source_key, collected_on)
);

CREATE INDEX IF NOT EXISTS raw_listing_collected_idx
    ON raw_listing (collected_on DESC);

-- ---------------------------------------------------------------------------
-- Layer 2: the time series. The moat.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS product (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform_code   TEXT NOT NULL REFERENCES platform(code),
    platform_sku    TEXT NOT NULL,
    title           TEXT NOT NULL,
    brand           TEXT,
    category_code   TEXT REFERENCES category(code),
    -- Which rule in the classifier fired, and how strongly. Persisted so a
    -- wrong classification is diagnosable rather than mysterious.
    classified_by   TEXT,
    classify_conf   TEXT CHECK (classify_conf IN ('high','medium','low','none')),
    seller_name     TEXT,
    first_seen      DATE NOT NULL,
    last_seen       DATE NOT NULL,
    UNIQUE (platform_code, platform_sku)
);

CREATE INDEX IF NOT EXISTS product_category_idx
    ON product (category_code, platform_code);

-- One row per product per day. The unique constraint is what makes the whole
-- collector resumable: an interrupted crawl reruns and conflicting rows are
-- skipped rather than duplicated.
CREATE TABLE IF NOT EXISTS price_observation (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id      BIGINT NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    observed_on     DATE NOT NULL,
    price_kes       NUMERIC(12,2) NOT NULL CHECK (price_kes > 0),
    was_price_kes   NUMERIC(12,2),
    in_stock        BOOLEAN,
    rating          NUMERIC(3,2),
    rating_count    INTEGER,
    UNIQUE (product_id, observed_on)
);

CREATE INDEX IF NOT EXISTS price_observation_date_idx
    ON price_observation (observed_on DESC);

-- ---------------------------------------------------------------------------
-- Layer 3: nightly aggregates. What the answer engine reads.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS category_rollup (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_code   TEXT NOT NULL REFERENCES category(code),
    platform_code   TEXT NOT NULL REFERENCES platform(code),
    observed_on     DATE NOT NULL,
    -- The figures. Every one of these is what the product quotes.
    median_price    NUMERIC(12,2) NOT NULL,
    p25_price       NUMERIC(12,2) NOT NULL,
    p75_price       NUMERIC(12,2) NOT NULL,
    min_price       NUMERIC(12,2) NOT NULL,
    max_price       NUMERIC(12,2) NOT NULL,
    -- The evidence that travels with every figure. An answer is not allowed to
    -- state a number without these.
    listing_count   INTEGER NOT NULL,
    seller_count    INTEGER NOT NULL,
    -- listings per distinct seller, weighted by price dispersion.
    saturation      NUMERIC(6,3),
    UNIQUE (category_code, platform_code, observed_on)
);

CREATE INDEX IF NOT EXISTS category_rollup_lookup_idx
    ON category_rollup (category_code, platform_code, observed_on DESC);

-- ---------------------------------------------------------------------------
-- Collector bookkeeping. Reruns must resume, not restart.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS crawl_run (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform_code   TEXT NOT NULL REFERENCES platform(code),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','ok','failed','aborted')),
    urls_seen       INTEGER NOT NULL DEFAULT 0,
    urls_ok         INTEGER NOT NULL DEFAULT 0,
    urls_failed     INTEGER NOT NULL DEFAULT 0,
    rows_written    INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- Checkpoint after every unit of work, so an interrupted crawl continues from
-- where it stopped rather than starting over.
CREATE TABLE IF NOT EXISTS crawl_checkpoint (
    platform_code   TEXT NOT NULL REFERENCES platform(code),
    scope           TEXT NOT NULL,
    cursor_value    TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_code, scope)
);

-- ---------------------------------------------------------------------------
-- Accounts. Authentication is phase one, not phase four: the predecessor
-- shipped with every route open and that is the mistake being corrected here.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS account (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    tier            TEXT NOT NULL DEFAULT 'trial'
                    CHECK (tier IN ('trial','starter','growth','pro','premium')),
    trial_ends_on   DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);

-- Server held sessions rather than a self contained token, because a lapsed
-- subscription must revoke access immediately and a self contained token
-- cannot be revoked before it expires without a server side list anyway.
CREATE TABLE IF NOT EXISTS session (
    token_hash      TEXT PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS session_account_idx ON session (account_id);

-- The question log. Pseudonymous account reference, never the email address:
-- what a vendor asks reveals what they are about to invest in, and that must
-- not leak even into our own analytics.
CREATE TABLE IF NOT EXISTS question_log (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_ref     TEXT NOT NULL,
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    question        TEXT NOT NULL,
    intent          TEXT,
    answered        BOOLEAN NOT NULL,
    refusal_reason  TEXT,
    observation_ct  INTEGER
);
