-- 001: initial schema. Never edit an applied migration; add a new numbered file instead.

CREATE TABLE documents (
    id            INTEGER PRIMARY KEY,
    filename      TEXT NOT NULL,
    local_path    TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    page_count    INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE pages (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number       INTEGER NOT NULL,          -- 1-based
    printed_label     TEXT,
    text              TEXT,                      -- bold spans wrapped as **...**
    blocks_json       TEXT,                      -- list[Block]
    rendered_path     TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
                      CHECK (extraction_status IN ('pending','done','needs_vision','failed')),
    cache_key         TEXT,                      -- content_hash|page|EXTRACTOR_VERSION
    UNIQUE (document_id, page_number)
);

CREATE TABLE decks (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',    -- {"content_pages":[...], "key_pages":[...]}
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE source_items (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    deck_id         INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    item_key        TEXT NOT NULL,               -- stable key, see schemas.SourceItem
    ordinal         INTEGER NOT NULL,            -- document order
    section_key     TEXT,
    question_number TEXT,
    type            TEXT NOT NULL,
    page_refs_json  TEXT NOT NULL,
    raw_content     TEXT NOT NULL,
    item_json       TEXT NOT NULL,               -- full schemas.SourceItem as JSON
    normalized_json TEXT,                        -- schemas.Normalized as JSON
    resolved_answer TEXT,
    answer_source   TEXT,
    disposition     TEXT NOT NULL DEFAULT 'pending'
                    CHECK (disposition IN ('pending','converted','duplicate','blocked','skipped')),
    issue_reason    TEXT,
    UNIQUE (deck_id, item_key)
);

CREATE TABLE evidence (
    id                INTEGER PRIMARY KEY,
    source_item_id    INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    page_number       INTEGER NOT NULL,
    excerpt           TEXT NOT NULL,
    bounding_box_json TEXT,
    kind              TEXT NOT NULL
);

CREATE TABLE cards (
    id                    INTEGER PRIMARY KEY,
    deck_id               INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    source_item_id        INTEGER NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    card_key              TEXT NOT NULL,         -- item_key|generation|ordinal; makes replay idempotent
    ordinal               INTEGER NOT NULL,      -- order within the source item
    front                 TEXT NOT NULL,
    back                  TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft','needs_review','approved','rejected')),
    validation_flags_json TEXT NOT NULL DEFAULT '[]',
    review_reason         TEXT,
    revision              INTEGER NOT NULL DEFAULT 1,
    fingerprint           TEXT,
    duplicate_of          INTEGER REFERENCES cards(id) ON DELETE SET NULL,
    verified_note         TEXT,                  -- user's note when approving despite unresolved evidence
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (deck_id, card_key)
);

CREATE TABLE card_evidence (
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    PRIMARY KEY (card_id, evidence_id)
);

CREATE TABLE card_revisions (
    id         INTEGER PRIMARY KEY,
    card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    front      TEXT NOT NULL,
    back       TEXT NOT NULL,
    origin     TEXT NOT NULL CHECK (origin IN ('generated','edited','regenerated','merged')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    deck_id          INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL DEFAULT 'process' CHECK (kind IN ('process','regenerate')),
    dedupe_key       TEXT NOT NULL,              -- 'process:<deck_id>' or 'regen:<source_item_id>'
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','completed','failed','cancelled')),
    settings_json    TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_id        TEXT,
    heartbeat        TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Reruns, refreshes and second tabs cannot queue the same work twice.
CREATE UNIQUE INDEX jobs_one_active ON jobs (dedupe_key) WHERE status IN ('queued','running');

CREATE TABLE job_steps (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    item_key      TEXT NOT NULL,
    stage         TEXT NOT NULL CHECK (stage IN ('extract','segment','link','generate','dedupe')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','running','done','failed','skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    cache_key     TEXT,
    result_ref    TEXT,
    error         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, stage, item_key)
);

CREATE TABLE model_calls (
    id               INTEGER PRIMARY KEY,
    job_step_id      INTEGER REFERENCES job_steps(id) ON DELETE SET NULL,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    estimated_cost   REAL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE exports (
    id                         INTEGER PRIMARY KEY,
    deck_id                    INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    local_path                 TEXT NOT NULL,
    card_ids_and_revisions_json TEXT NOT NULL,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Single active worker. Row id=1 is the lease; a worker takes it only if free or stale.
CREATE TABLE worker_lease (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id TEXT NOT NULL,
    heartbeat TEXT NOT NULL
);
