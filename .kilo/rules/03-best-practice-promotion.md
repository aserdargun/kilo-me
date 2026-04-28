# Rule 03 — Promoting patterns to the best-practices repo

Patterns are promoted from local SQLite to the GitHub `kilo-best-practices` repo when ALL of these are true:

1. The same primary domain tag appears in **3 or more** SQLite rows
2. All 3+ rows have `success = true`
3. They span **at least 2 distinct days** (proves it's not a single-session fluke)
4. At least one of them has a Mermaid diagram in `mermaid-vector`

## Promotion mechanics

The Memory Curator agent detects a qualifying pattern and writes a file at:

```
docs/decisions/<kebab-title>.md
```

with this frontmatter:

```yaml
---
title: <human title>
pattern: <n>            # how many SQLite rows back this
first_seen: <ISO date>
last_seen: <ISO date>
domain: <primary tag>
mermaid_ids: [<id>, ...]
sqlite_prompt_ids: [<id>, ...]
---
```

The user runs `make sync-bp` to:

- Copy `docs/decisions/*.md` into the cloned `kilo-best-practices` repo
- Commit with message `promote: <kebab-title> (pattern=<n>)`
- Open a PR (or push to main if user has set `BP_AUTO_MERGE=1`)

## Anti-patterns we deliberately do NOT promote

- One-off bug fixes (use post-mortems instead, in a separate folder)
- Patterns that only succeeded once
- Anything that required `--auto` mode without human review

This is the project's primary defense against AI-generated cargo cult code.
