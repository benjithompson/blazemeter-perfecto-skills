# Context Resolution is multi-account-safe: confirm the default, never assume it

A user can belong to **multiple accounts**, each with **multiple workspaces, projects, and tests**,
and names collide across them. The original §4 Context Resolution convention leaned on a single
default (`blazemeter_user read` → "the default account/workspace/project"), which silently picks one
context out of many — exactly the "operate against the wrong thing" failure §4 exists to prevent.

**Decision:** the §4 step is rewritten so that **the default is a suggestion to confirm, never a
decision made for the user**. No skill silently resolves a level when more than one is possible.

**Where it lives:** the behavior is encoded in `shared/conventions.md` §4 (the canonical prose every
skill copies), enforced by a new §8 Definition-of-Done item, and mirrored in the reference skill
`skills/bzm-analyze-test` Step 0. These three move together — the reference implementation and
the merge checklist must not contradict the house style.

**The resolution model:**

- **Two entry paths.** A given `test_id` is trusted and resolved *upward* (test → project →
  workspace → account); the displayed context block is the confirmation. Nothing-given or a bare
  test *name* is resolved *top-down* (account → workspace → project), and a name is matched only
  inside the confirmed project — a name is meaningless without a scope, so it is never searched
  across everything.

- **One uniform tiered pick rule at every level** (workspace, project, test). Start from the default
  as a confirmable/overridable suggestion; a level with one option is just displayed. To enumerate,
  list one page (`limit: 50`): a **non-full** first page is a small set → numbered list with ids; a
  **full** first page means more pages exist (the power user with hundreds of workspaces) → do not
  dump the list, ask the user to **name or paste** it. A pasted **id short-circuits** any level via
  direct `read`. The page cap is the threshold — self-tuning, no magic number.

- **Strict failure handling — never fall back to the default.** No match → stop and report; multiple
  matches → a disambiguation menu showing each candidate's parent and id; 403 → report the access
  gap, don't retry; broken upward link → stop and report the gap.

- **AI Consent gate.** AI access is gated per account. The consent state is read from the
  `blazemeter_account read` already made at the top of the chain (no extra call); if the account has
  not consented, stop with a clear message rather than failing cryptically downstream.

- **Carry context forward as conversational memory.** The confirmed account/workspace is reused
  across later skills in the same conversation, displayed each time with a one-step override, to
  avoid the banner blindness that re-prompting identical context would breed. It is **never persisted
  to disk or cached across sessions** — durable caching would reintroduce the "assume" problem later.

**Trade-off accepted:** resolving top-down and disambiguating costs more turns than trusting the
default, and there is no MCP primitive for cross-account name search (matching a name in a large
account means paging). We pay that to guarantee a skill never analyzes, compares, or runs against the
wrong account's data. This sharpens — does not replace — §4's prior rule to validate each level of
**Account → Workspace → Project → Test → Execution** before operating on the next.
