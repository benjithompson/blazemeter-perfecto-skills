# GitHub integration is MCP-first, and generated CI YAML is secrets-only

v2 adds **Theme B** skills that connect BlazeMeter results to a user's GitHub workflow — posting a
load-test summary as a **PR comment**, setting a **commit status / check**, and scaffolding a
**CI job** that runs a test on push. These skills need two things the BlazeMeter MCP can't give
them: a way to talk to GitHub, and a way to hand a generated CI job its BlazeMeter credentials.

**Decision — use the GitHub MCP, mirroring ADR-0004's BlazeMeter posture.** A skill that touches
GitHub (PR comments, commit status, checks) calls the **GitHub MCP** first, exactly as skills call
the BlazeMeter MCP first. The `gh` CLI and the GitHub REST API are a **documented fallback** only —
used to reach a capability the GitHub MCP doesn't cover, and when used, the skill **says so and why**
in its prose, the same rule ADR-0004 sets for dropping to the BlazeMeter REST API. We do not
hand-roll `gh` calls where an MCP tool exists: the MCP is higher-level, its auth is already
configured, and the call is safer and more stable.

**Decision — generated CI YAML is secrets-only.** The CI-scaffold skill emits GitHub Actions YAML
that authenticates to BlazeMeter by reading a **GitHub Actions secret**:

```yaml
env:
  BLAZEMETER_API_KEY: ${{ secrets.BLAZEMETER_API_KEY }}
```

The plugin **never embeds, logs, or echoes a token**, and the generated YAML contains **only
`secrets.*` references** — no literal key, no key-file path, no `echo`/`cat` of a secret. The user
provisions `BLAZEMETER_API_KEY` once as a repository secret; the workflow consumes it by reference
at run time. This is the CI form of conventions §6 and ADR-0008 (credentials live in the
environment, never in the repo or an artifact), applied to a file the skill writes into the user's
repo.

**Where it lives:** the house-style rule is mirrored in `shared/conventions.md` §5 (Integration) so
every Theme B skill copies the same posture; this ADR is the rationale of record.

**Trade-off accepted:** GitHub MCP coverage may lag a needed operation, so some Theme B skills will
carry a justified `gh`/REST fallback — we pay that only where the MCP can't reach, identical to the
BlazeMeter trade-off in ADR-0004. Requiring a pre-provisioned `secrets.BLAZEMETER_API_KEY` means the
scaffolded workflow does not run until the user adds the secret; that friction is deliberate — it is
the only way to keep a generated, committed artifact free of credentials.
