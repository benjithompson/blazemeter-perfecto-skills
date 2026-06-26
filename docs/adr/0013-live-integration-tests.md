# Testing strategy: hermetic mock unit tests + credential-gated live integration tests

The deterministic shared scripts (credential resolution, the `bzm_*` REST utilities) are
covered two ways, deliberately:

1. **Mock unit tests** — the default. They inject the environment and mock the HTTP layer,
   so they are fast, hermetic, and run everywhere (including CI) with no credentials and no
   network. These remain the bar every utility must clear.

2. **Live integration tests** — opt-in. They exercise the same utilities against the real
   BlazeMeter REST API, which is the only way to catch endpoint/shape drift the mocks can't.
   They replace the old per-utility "run it once by hand before merge" manual-verify step
   (ADR-0010's Definition of Done) with something repeatable.

Live tests are **gated so they are never noisy**:

- Each is marked `@pytest.mark.live` and **deselected by default** (`addopts = -m "not live"`
  in `pyproject.toml`). Plain `pytest` and CI run only the mock tests; you opt in with
  `pytest -m live`.
- Even when selected they **skip, never fail**, unless real Platform Credentials resolve
  *and* the target id the test needs is set. So `pytest -m live` with nothing configured is
  all skips.

Credentials reuse the shared resolver (ADR-0008) — the same env-var scheme as the MCP — and
a gitignored `api-key.json` at the repo root is picked up automatically. Targets are supplied
via `BZM_LIVE_*` env vars or a gitignored `tests/live.env` (see `tests/live.env.example`).

**CI stays credential-free.** Live tests run **locally only** (per the v1 decision); we do not
store a BlazeMeter API key as a CI secret. If we later want live coverage in CI, it is an
additive job gated on a repository secret — not a change to this default.

This keeps the fast/safe inner loop unchanged while making the "does it really work against
BlazeMeter?" check a first-class, automated thing instead of a manual ritual.
