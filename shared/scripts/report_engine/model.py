"""The normalized **Report data model** — the boundary between retrieval and rendering.

A retrieval step produces a ``ReportModel`` (from MCP/REST data); the renderer
consumes it. Keeping this as an explicit, validated seam means the renderer can be
tested against fixtures with no network, and future report types reuse the same
shape. The model carries **data and narrative only** — never credentials.

Everything is plain stdlib dataclasses with ``from_dict`` / ``to_dict`` round-tripping
so a model can be serialized to JSON (the CLI reads a model JSON file) and rebuilt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


class ReportModelError(ValueError):
    """A Report data model is missing a required field or has the wrong shape."""


# --- leaf value types --------------------------------------------------------


@dataclass(frozen=True)
class ContextRef:
    """A named, optionally-id'd node in the account → workspace → project → test chain."""

    name: str
    id: str | None = None


@dataclass(frozen=True)
class ReportContext:
    """The resolved BlazeMeter context the report was built against (display-only)."""

    account: ContextRef | None = None
    workspace: ContextRef | None = None
    project: ContextRef | None = None
    test: ContextRef | None = None


@dataclass(frozen=True)
class RunKpis:
    """Aggregate KPIs for one execution. Any field may be ``None`` if unavailable."""

    avg_rt_ms: float | None = None
    p90_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    rps: float | None = None
    error_rate_pct: float | None = None
    concurrency: float | None = None


@dataclass(frozen=True)
class RunRow:
    """One execution in the time series — drives both the trend table and the charts."""

    execution_id: str
    timestamp: str  # ISO 8601
    label: str = ""
    status: str = ""  # pass | fail | unset | abort | error | noData
    kpis: RunKpis = field(default_factory=RunKpis)


@dataclass(frozen=True)
class Regression:
    """A flagged KPI movement between runs."""

    kpi: str
    from_value: float
    to_value: float
    pct_change: float
    direction: str = ""  # "up" | "down"
    severity: str = "info"  # info | warning | critical
    run_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class SlaRule:
    label: str
    threshold: str
    pass_rate_pct: float | None = None
    note: str = ""


@dataclass(frozen=True)
class SlaCompliance:
    pass_count: int = 0
    fail_count: int = 0
    rules: list[SlaRule] = field(default_factory=list)


@dataclass(frozen=True)
class EndpointRow:
    name: str
    p95_ms: float | None = None
    error_rate_pct: float | None = None
    trend: str = ""


@dataclass(frozen=True)
class ReportMeta:
    """Report header info. ``generated_at`` is **supplied** (never wall-clock-read here)
    so renders are deterministic and golden-testable."""

    title: str
    generated_at: str  # ISO 8601, supplied by the caller
    subtitle: str = ""
    context: ReportContext = field(default_factory=ReportContext)
    window_start: str | None = None
    window_end: str | None = None


@dataclass(frozen=True)
class ExecutiveSummary:
    headline: str = ""
    verdict: str = ""  # e.g. SHIP / NO-SHIP / REGRESSED / STABLE
    narrative: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportModel:
    """The whole report, normalized. The renderer needs nothing else."""

    meta: ReportMeta
    summary: ExecutiveSummary = field(default_factory=ExecutiveSummary)
    runs: list[RunRow] = field(default_factory=list)
    regressions: list[Regression] = field(default_factory=list)
    sla: SlaCompliance | None = None
    endpoints: list[EndpointRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (JSON-ready). ``None`` SLA is dropped."""
        d = asdict(self)
        if self.sla is None:
            d.pop("sla", None)
        return d


# --- from_dict construction (explicit, so bad shapes fail loudly) ------------


def _ref(d: Any) -> ContextRef | None:
    if d is None:
        return None
    if not isinstance(d, Mapping):
        raise ReportModelError("context ref must be an object with a 'name'")
    name = d.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ReportModelError("context ref requires a non-empty 'name'")
    rid = d.get("id")
    return ContextRef(name=name, id=None if rid is None else str(rid))


def _num(value: Any, fieldname: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportModelError("%s must be a number, got %r" % (fieldname, value))
    return float(value)


def _kpis(d: Any) -> RunKpis:
    if d is None:
        return RunKpis()
    if not isinstance(d, Mapping):
        raise ReportModelError("kpis must be an object")
    return RunKpis(
        avg_rt_ms=_num(d.get("avg_rt_ms"), "kpis.avg_rt_ms"),
        p90_ms=_num(d.get("p90_ms"), "kpis.p90_ms"),
        p95_ms=_num(d.get("p95_ms"), "kpis.p95_ms"),
        p99_ms=_num(d.get("p99_ms"), "kpis.p99_ms"),
        rps=_num(d.get("rps"), "kpis.rps"),
        error_rate_pct=_num(d.get("error_rate_pct"), "kpis.error_rate_pct"),
        concurrency=_num(d.get("concurrency"), "kpis.concurrency"),
    )


def _runs(seq: Any) -> list[RunRow]:
    if seq is None:
        return []
    if not isinstance(seq, Sequence) or isinstance(seq, (str, bytes)):
        raise ReportModelError("'runs' must be a list")
    rows: list[RunRow] = []
    for i, d in enumerate(seq):
        if not isinstance(d, Mapping):
            raise ReportModelError("runs[%d] must be an object" % i)
        eid = d.get("execution_id")
        ts = d.get("timestamp")
        if not eid:
            raise ReportModelError("runs[%d] requires 'execution_id'" % i)
        if not ts:
            raise ReportModelError("runs[%d] requires 'timestamp'" % i)
        rows.append(
            RunRow(
                execution_id=str(eid),
                timestamp=str(ts),
                label=str(d.get("label", "")),
                status=str(d.get("status", "")),
                kpis=_kpis(d.get("kpis")),
            )
        )
    return rows


def _regressions(seq: Any) -> list[Regression]:
    if seq is None:
        return []
    if not isinstance(seq, Sequence) or isinstance(seq, (str, bytes)):
        raise ReportModelError("'regressions' must be a list")
    out: list[Regression] = []
    for i, d in enumerate(seq):
        if not isinstance(d, Mapping):
            raise ReportModelError("regressions[%d] must be an object" % i)
        if "kpi" not in d:
            raise ReportModelError("regressions[%d] requires 'kpi'" % i)
        out.append(
            Regression(
                kpi=str(d["kpi"]),
                from_value=_num(d.get("from_value"), "from_value") or 0.0,
                to_value=_num(d.get("to_value"), "to_value") or 0.0,
                pct_change=_num(d.get("pct_change"), "pct_change") or 0.0,
                direction=str(d.get("direction", "")),
                severity=str(d.get("severity", "info")),
                run_id=None if d.get("run_id") is None else str(d.get("run_id")),
                note=str(d.get("note", "")),
            )
        )
    return out


def _sla(d: Any) -> SlaCompliance | None:
    if d is None:
        return None
    if not isinstance(d, Mapping):
        raise ReportModelError("'sla' must be an object")
    rules = []
    for i, r in enumerate(d.get("rules", []) or []):
        if not isinstance(r, Mapping) or not r.get("label"):
            raise ReportModelError("sla.rules[%d] requires a 'label'" % i)
        rules.append(
            SlaRule(
                label=str(r["label"]),
                threshold=str(r.get("threshold", "")),
                pass_rate_pct=_num(r.get("pass_rate_pct"), "sla.rules.pass_rate_pct"),
                note=str(r.get("note", "")),
            )
        )
    return SlaCompliance(
        pass_count=int(d.get("pass_count", 0) or 0),
        fail_count=int(d.get("fail_count", 0) or 0),
        rules=rules,
    )


def _endpoints(seq: Any) -> list[EndpointRow]:
    if seq is None:
        return []
    out: list[EndpointRow] = []
    for i, d in enumerate(seq):
        if not isinstance(d, Mapping) or not d.get("name"):
            raise ReportModelError("endpoints[%d] requires a 'name'" % i)
        out.append(
            EndpointRow(
                name=str(d["name"]),
                p95_ms=_num(d.get("p95_ms"), "endpoints.p95_ms"),
                error_rate_pct=_num(d.get("error_rate_pct"), "endpoints.error_rate_pct"),
                trend=str(d.get("trend", "")),
            )
        )
    return out


def _meta(d: Any) -> ReportMeta:
    if not isinstance(d, Mapping):
        raise ReportModelError("'meta' is required and must be an object")
    title = d.get("title")
    gen = d.get("generated_at")
    if not isinstance(title, str) or not title.strip():
        raise ReportModelError("meta.title is required")
    if not isinstance(gen, str) or not gen.strip():
        raise ReportModelError("meta.generated_at is required (ISO 8601, supplied by caller)")
    ctx = d.get("context") or {}
    return ReportMeta(
        title=title,
        generated_at=gen,
        subtitle=str(d.get("subtitle", "")),
        context=ReportContext(
            account=_ref(ctx.get("account")),
            workspace=_ref(ctx.get("workspace")),
            project=_ref(ctx.get("project")),
            test=_ref(ctx.get("test")),
        ),
        window_start=None if d.get("window_start") is None else str(d.get("window_start")),
        window_end=None if d.get("window_end") is None else str(d.get("window_end")),
    )


def _summary(d: Any) -> ExecutiveSummary:
    if d is None:
        return ExecutiveSummary()
    if not isinstance(d, Mapping):
        raise ReportModelError("'summary' must be an object")
    narrative = d.get("narrative", []) or []
    if not isinstance(narrative, Sequence) or isinstance(narrative, (str, bytes)):
        raise ReportModelError("summary.narrative must be a list of strings")
    return ExecutiveSummary(
        headline=str(d.get("headline", "")),
        verdict=str(d.get("verdict", "")),
        narrative=[str(p) for p in narrative],
    )


def model_from_dict(d: Mapping[str, Any]) -> ReportModel:
    """Build (and validate) a ``ReportModel`` from a plain dict / parsed JSON."""
    if not isinstance(d, Mapping):
        raise ReportModelError("report model must be a JSON object")
    return ReportModel(
        meta=_meta(d.get("meta")),
        summary=_summary(d.get("summary")),
        runs=_runs(d.get("runs")),
        regressions=_regressions(d.get("regressions")),
        sla=_sla(d.get("sla")),
        endpoints=_endpoints(d.get("endpoints")),
    )
