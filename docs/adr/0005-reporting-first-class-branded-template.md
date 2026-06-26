# Reporting is a first-class value area, filling the gaps the platform can't

Cross-cutting reporting and visualization is a primary value area for this repo, not an afterthought.

Rationale: BlazeMeter's native reports are mostly *single-execution* views. The high-value gap is
combining data **across tests, executions, environments, and time** — release reports, multi-run
trends, portfolio scorecards — which the platform itself can't produce.

Approach: a **deterministic, BlazeMeter-branded HTML template** provides the report's fixed chrome
(layout, styling, chart scaffolding); the AI fills in the data and narrative from data retrieved via
the MCP (and the REST API where the MCP falls short — see ADR-0004). Determinism comes from the
template; the AI supplies the content, not the structure.

Trade-off: the branded template adds maintenance and constrains layout, but it buys consistent,
shareable, on-brand artifacts and keeps AI output predictable. Free-form AI-generated reports were
rejected as too inconsistent for stakeholder-facing use.

Reporting composes with the analysis skills: `analyze` / `compare` / `triage` produce the findings;
a report renders them (plus raw retrieved data) into a branded artifact.
