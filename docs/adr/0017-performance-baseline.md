# A performance baseline has two forms: conversational pin (interactive) and committed file (CI)

v2 introduces **baselines** — the reference a run is compared against to decide whether performance
regressed. Two contexts need a baseline and they have different durability requirements: an
**interactive** conversation (a person asking "did this run get slower?") and a **CI gate** (an
automated check that must reproduce the same verdict on every push). One representation does not fit
both.

**Decision — interactive: a conversational pin, or "the last passing run".** In a conversation a
baseline is **an explicit execution id the user pins for that conversation** ("baseline against
execution 98765"). Absent a pin, it defaults to **the last passing run** of the test resolved at
call time — looked up live, never remembered. This is **conversational memory only**: the pin lives
for the conversation and is **never persisted across sessions**, consistent with conventions §4.6's
carry-context-forward-but-don't-store rule.

**Decision — CI: a committed file in the user's repo.** A CI gate must be reproducible and
reviewable, so its baseline is a **file the user commits**: `.blazemeter/baseline.json`, mapping
`test_id → execution_id`:

```json
{ "12345": "98765", "67890": "99001" }
```

The **gate skill reads** this file to find the baseline execution to compare against; the
**baseline skill writes / updates** it (e.g. "promote this run to the new baseline"), producing a
diff the user reviews and commits like any other change. The map is keyed by `test_id` so one file
covers every test a repo gates.

**Why the committed CI file does not violate the no-stored-account-state rule.** Conventions §4.6
and ADR-0012 forbid the plugin from **persisting resolved BlazeMeter context** (account / workspace /
project) to disk or caching it across sessions — because cached context silently reintroduces the
"assume the wrong account" failure. `.blazemeter/baseline.json` is a different kind of thing: it is
**the user's own repo state** — explicit, version-controlled, reviewed in a PR, and owned by the
user — not the plugin quietly caching account/workspace context behind the user's back. It records a
**deliberate, reproducible CI decision** ("this execution is the bar"), the same way a lockfile or a
CI config does. The rule we keep is the one that matters: the plugin never *invisibly* remembers
resolved context. A file the user writes and commits is visible, reviewable, and intentional.

**Trade-off accepted:** two representations mean the baseline skill must read/write both a
conversational pin and the committed file, and the two can diverge (a pinned execution in chat is
not the committed CI baseline). We accept that because the alternatives are worse: a single
conversational baseline can't gate CI reproducibly, and a single committed baseline would force
every interactive question to mutate the repo. Keeping them separate matches each context's real
durability need.
