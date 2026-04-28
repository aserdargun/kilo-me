-- ----------------------------------------------------------------------------
-- kilo-me — SQLite memory schema
-- Stores every agent prompt + outcome with FTS5 search.
-- ----------------------------------------------------------------------------

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Sessions group related prompts (one Kilo task = one session).
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    user_intent TEXT,
    agent_chain TEXT  -- comma-separated agent slugs in order
);

-- One row per agent invocation. Updated in place from null -> success/fail.
CREATE TABLE IF NOT EXISTS prompts (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    ts          TEXT NOT NULL,
    agent       TEXT NOT NULL,
    model       TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    completion  TEXT,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    success     INTEGER,                        -- 0 / 1 / NULL (in-flight)
    tags        TEXT,                           -- JSON array
    mermaid_id  TEXT,                           -- ChromaDB document id
    cost_usd    REAL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_prompts_ts      ON prompts(ts);
CREATE INDEX IF NOT EXISTS idx_prompts_agent   ON prompts(agent);
CREATE INDEX IF NOT EXISTS idx_prompts_model   ON prompts(model);
CREATE INDEX IF NOT EXISTS idx_prompts_success ON prompts(success);

-- FTS5 virtual table for fast keyword + ranked search across prompt bodies.
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
    prompt,
    completion,
    tags,
    content='prompts',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Triggers keep FTS index in sync with the prompts table.
CREATE TRIGGER IF NOT EXISTS prompts_ai AFTER INSERT ON prompts BEGIN
    INSERT INTO prompts_fts(rowid, prompt, completion, tags)
    VALUES (new.rowid, new.prompt, COALESCE(new.completion, ''), COALESCE(new.tags, ''));
END;

CREATE TRIGGER IF NOT EXISTS prompts_ad AFTER DELETE ON prompts BEGIN
    INSERT INTO prompts_fts(prompts_fts, rowid, prompt, completion, tags)
    VALUES ('delete', old.rowid, old.prompt, COALESCE(old.completion, ''), COALESCE(old.tags, ''));
END;

CREATE TRIGGER IF NOT EXISTS prompts_au AFTER UPDATE ON prompts BEGIN
    INSERT INTO prompts_fts(prompts_fts, rowid, prompt, completion, tags)
    VALUES ('delete', old.rowid, old.prompt, COALESCE(old.completion, ''), COALESCE(old.tags, ''));
    INSERT INTO prompts_fts(rowid, prompt, completion, tags)
    VALUES (new.rowid, new.prompt, COALESCE(new.completion, ''), COALESCE(new.tags, ''));
END;

-- Promotion ledger: one row per pattern that graduated to the GitHub repo.
CREATE TABLE IF NOT EXISTS promotions (
    id           TEXT PRIMARY KEY,
    promoted_at  TEXT NOT NULL,
    title        TEXT NOT NULL,
    domain       TEXT NOT NULL,
    pattern_n    INTEGER NOT NULL,
    prompt_ids   TEXT NOT NULL,                 -- JSON array
    mermaid_ids  TEXT,                          -- JSON array
    repo_path    TEXT,                          -- path inside best-practices repo
    pr_url       TEXT
);

CREATE INDEX IF NOT EXISTS idx_promotions_domain ON promotions(domain);
