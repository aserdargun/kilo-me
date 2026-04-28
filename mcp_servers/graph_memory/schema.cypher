// ----------------------------------------------------------------------------
// kilo-me — graph-memory schema (Kuzu Cypher DDL)
//
// Authoritative description of the relationship layer. The server applies
// this schema lazily on first connection via _ensure_schema(); this file is
// kept for human readers and for `kuzu` CLI users who want to inspect or
// re-apply by hand.
// ----------------------------------------------------------------------------

// Node tables -----------------------------------------------------------------
CREATE NODE TABLE IF NOT EXISTS Prompt(
    id STRING,
    agent STRING,
    model STRING,
    success INT64,
    PRIMARY KEY (id)
);

CREATE NODE TABLE IF NOT EXISTS Agent(
    id STRING,
    description STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE IF NOT EXISTS Tag(
    id STRING,
    category STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE IF NOT EXISTS Diagram(
    id STRING,
    title STRING,
    PRIMARY KEY (id)
);

CREATE NODE TABLE IF NOT EXISTS Decision(
    id STRING,
    pattern_n INT64,
    PRIMARY KEY (id)
);

CREATE NODE TABLE IF NOT EXISTS Pattern(
    id STRING,
    domain STRING,
    PRIMARY KEY (id)
);

// Rel tables ------------------------------------------------------------------
CREATE REL TABLE IF NOT EXISTS LOGGED_BY(   FROM Prompt   TO Agent,    properties STRING );
CREATE REL TABLE IF NOT EXISTS TAGGED(      FROM Prompt   TO Tag,      properties STRING );
CREATE REL TABLE IF NOT EXISTS DEPICTS(     FROM Prompt   TO Diagram,  properties STRING );
CREATE REL TABLE IF NOT EXISTS PROMOTED_TO( FROM Prompt   TO Decision, properties STRING );
CREATE REL TABLE IF NOT EXISTS DERIVES(     FROM Decision TO Pattern,  properties STRING );
