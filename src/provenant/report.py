"""Self-contained HTML verification report.

One file, no JavaScript, no external assets except an optional web font with a
system fallback. It opens offline, prints cleanly for a design review package,
and every verdict expands to the evidence behind it: the run, the raw column,
the resolved window, the sample at which the governing value occurred, and a
plot of the signal with the window and limit drawn in.
"""

from __future__ import annotations

import datetime as _dt
import html
from collections.abc import Iterable

from . import __version__
from .evaluate import (
    ERROR,
    FAILED,
    RETEST,
    STATUS_ORDER,
    UNVERIFIED,
    VERIFIED,
    Evaluation,
    Evidence,
    RequirementResult,
)
from .expr import Duration
from .lock import ChangeReport
from .units import format_number, format_si

_OP = {"<=": "≤", ">=": "≥", "<": "<", ">": ">"}
_STATUS_LABEL = {
    VERIFIED: "Verified",
    FAILED: "Failed",
    RETEST: "Retest",
    ERROR: "Error",
    UNVERIFIED: "Unverified",
    "pass": "Pass",
    "fail": "Fail",
}


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _chip(status: str | None) -> str:
    status = status or "none"
    return f'<span class="chip s-{_e(status)}">{_e(_STATUS_LABEL.get(status, status.title()))}</span>'


def _margin(m: float | None) -> str:
    return "" if m is None else f"{m * 100:+.1f}%"


def _seconds(t: float | None) -> str:
    return "—" if t is None else f"{format_number(t)} s"


# --------------------------------------------------------------------------- plot


def _plot(e: Evidence, result: RequirementResult) -> str:
    pv = e.provenance
    points = [(t, v) for t, v in pv.trace if v is not None]
    cond = result.requirement.condition
    if len(points) < 2 or cond is None:
        return ""
    width, height = 640.0, 170.0
    left, right, top, bottom = 46.0, 12.0, 12.0, 26.0
    t_min, t_max = pv.trace[0][0], pv.trace[-1][0]
    if t_max <= t_min:
        return ""
    values = [v for _, v in points]

    # Level line: the limit for aggregates, the duration level for duration().
    measure = cond.measure
    if isinstance(measure, Duration):
        level_si, level_unit, level_op = measure.threshold.si, measure.threshold.unit, measure.op
    else:
        level_si, level_unit, level_op = cond.threshold.si, cond.threshold.unit, cond.op
    level = None
    if pv.trace_unit == level_unit.symbol:
        level = level_unit.from_si(level_si)
        values.append(level)
    v_min, v_max = min(values), max(values)
    pad = (v_max - v_min) * 0.08 or 1.0
    v_min, v_max = v_min - pad, v_max + pad

    def x(t: float) -> float:
        return left + (t - t_min) / (t_max - t_min) * (width - left - right)

    def y(v: float) -> float:
        return top + (v_max - v) / (v_max - v_min) * (height - top - bottom)

    parts: list[str] = []
    if pv.t0 is not None and pv.t1 is not None and pv.window:
        x0, x1 = x(max(pv.t0, t_min)), x(min(pv.t1, t_max))
        parts.append(
            f'<rect class="win" x="{x0:.1f}" y="{top}" width="{max(x1 - x0, 0):.1f}" height="{height - top - bottom}"/>'
        )
        parts.append(f'<text class="axis" x="{x0 + 4:.1f}" y="{height - bottom - 5}">{_e(pv.window)}</text>')

    segments: list[list[str]] = [[]]
    for t, v in pv.trace:
        if v is None:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(f"{x(t):.1f},{y(v):.1f}")
    for seg in segments:
        if len(seg) > 1:
            parts.append(f'<polyline class="sig" points="{" ".join(seg)}"/>')

    if level is not None:
        yl = y(level)
        parts.append(
            f'<line class="limit s-{_e(e.verdict)}" x1="{left}" x2="{width - right}" y1="{yl:.1f}" y2="{yl:.1f}"/>'
        )
        label = f"{_OP[level_op]} {format_number(level)}"
        parts.append(f'<text class="axis end" x="{width - right - 2}" y="{yl - 4:.1f}">{_e(label)}</text>')

    if pv.t_at is not None and e.measured_si is not None and pv.trace_unit == cond.threshold.unit.symbol:
        mv = cond.threshold.unit.from_si(e.measured_si)
        parts.append(f'<circle class="mark s-{_e(e.verdict)}" cx="{x(pv.t_at):.1f}" cy="{y(mv):.1f}" r="3.5"/>')

    base = height - bottom
    parts.append(f'<line class="frame" x1="{left}" x2="{width - right}" y1="{base}" y2="{base}"/>')
    parts.append(f'<line class="frame" x1="{left}" x2="{left}" y1="{top}" y2="{base}"/>')
    parts.append(f'<text class="axis end" x="{left - 6}" y="{top + 8}">{_e(format_number(v_max))}</text>')
    parts.append(f'<text class="axis end" x="{left - 6}" y="{base}">{_e(format_number(v_min))}</text>')
    parts.append(f'<text class="axis" x="{left}" y="{height - 8}">{_e(_seconds(t_min))}</text>')
    parts.append(f'<text class="axis end" x="{width - right}" y="{height - 8}">{_e(_seconds(t_max))}</text>')
    parts.append(f'<text class="axis" x="{left + 4}" y="{top + 11}">{_e(pv.channel)} [{_e(pv.trace_unit)}]</text>')

    label = f"{pv.channel} in {e.source}: {e.measured} against limit {e.threshold}, {_STATUS_LABEL.get(e.verdict, e.verdict)}"
    return (
        f'<svg class="plot" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_e(label)}">'
        f"<title>{_e(label)}</title>{''.join(parts)}</svg>"
    )


# --------------------------------------------------------------------------- sections


def _evidence_block(e: Evidence, result: RequirementResult, evaluation: Evaluation) -> str:
    project = evaluation.project
    pv = e.provenance
    rows: list[tuple[str, str]] = []
    if e.source == "design":
        title = "Current design"
        for name, value in pv.parameters.items():
            source = project.design[name].source if name in project.design else ""
            rows.append((name, _e(value) + (f' <span class="muted">{_e(source)}</span>' if source else "")))
    else:
        run = e.run
        assert run is not None
        tc = project.test_cases.get(e.test_case_id or "")
        title = f"{run.id} · {tc.title if tc else e.test_case_id}"
        rows.append(("Run", f"{_e(run.id)}, {_e(run.date)}, article {_e(run.article)}"))
        rows.append(("Test case", _e(e.test_case_id)))
        if pv.column:
            rows.append(("Channel", f"{_e(pv.channel)} from column <code>{_e(pv.column)}</code> [{_e(pv.raw_unit)}]"))
        if pv.window:
            rows.append(
                (
                    "Window",
                    f"{_e(pv.window)}: {_e(_seconds(pv.t0))} to {_e(_seconds(pv.t1))} "
                    f'<span class="muted">({_e(pv.window_definition)})</span>',
                )
            )
        else:
            rows.append(("Window", f"entire record, {_e(_seconds(pv.t0))} to {_e(_seconds(pv.t1))}"))
        for name, t in pv.events.items():
            rows.append((f"Event {name}", _e(_seconds(t))))
        if pv.t_at is not None:
            rows.append(("Governing sample", f"t = {_e(_seconds(pv.t_at))}"))
        if pv.samples is not None:
            missing = f", {pv.missing} missing" if pv.missing else ""
            rows.append(("Samples", f"{pv.samples} valid{missing}"))
        sha = f' <span class="muted">sha256 {_e((pv.data_sha256 or "")[:16])}</span>' if pv.data_sha256 else ""
        rows.append(("Data", f"<code>{_e(pv.data_file)}</code>{sha}"))
        if run.notes:
            rows.append(("Notes", _e(run.notes).replace("\n", "<br>")))
    for d in e.config_diffs:
        rows.append(
            (
                "Configuration",
                f'<span class="warn">{_e(d.parameter)} tested at {_e(d.recorded)}, design is now {_e(d.current)}</span>',
            )
        )
    if e.message:
        rows.append(("Error", f'<span class="warn">{_e(e.message)}</span>'))

    state = ""
    if e.superseded_by:
        state = f'<span class="muted">superseded by {_e(e.superseded_by)}</span>'
    elif not e.applicable:
        state = '<span class="warn">different configuration; needs retest</span>'
    value = (
        ""
        if e.measured is None
        else f'<span class="num">{_e(e.measured)}</span> <span class="muted">margin {_e(_margin(e.margin))}</span>'
    )
    table = "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows)
    classes = "evidence" + (" superseded" if e.superseded_by else "")
    return (
        f'<section class="{classes}"><header><h4>{_e(title)}</h4>{_chip(e.verdict)} {value} {state}</header>'
        f'{_plot(e, result)}<table class="kv">{table}</table></section>'
    )


def _requirement_rows(evaluation: Evaluation) -> str:
    project = evaluation.project
    out: list[str] = []
    for rid, result in evaluation.results.items():
        req = result.requirement
        depth = project.depth(rid)
        active = [e for e in result.active_evidence if e.measured is not None]
        governing = min(
            [e for e in active if e.applicable] or active,
            key=lambda e: e.margin if e.margin is not None else float("inf"),
            default=None,
        )
        limit = f"{_OP[req.condition.op]} {req.condition.threshold}" if req.condition else ""
        cells = (
            f'<span class="rid">{_e(rid)}</span><span class="rtitle">{_e(req.title)}</span>',
            _chip(result.status),
            f'<span class="num">{_e(governing.measured if governing else "")}</span>',
            f'<span class="num">{_e(limit)}</span>',
            f'<span class="num">{_e(_margin(governing.margin) if governing else "")}</span>',
        )
        summary = "".join(f'<span class="c{i}">{c}</span>' for i, c in enumerate(cells))
        style = f' style="--depth:{depth}"'
        if req.condition is None:
            out.append(
                f'<div class="req group"{style}><div class="row">{summary}</div>'
                f'<p class="reason">{_e(result.reason)}</p></div>'
            )
            continue
        evidence = sorted(result.evidence, key=lambda e: (e.superseded_by is not None, e.source))
        body = "".join(_evidence_block(e, result, evaluation) for e in evidence)
        if not evidence:
            cases = project.test_cases_for(rid)
            hint = (
                f"Planned in {', '.join(tc.id for tc in cases)}; no run recorded yet."
                if cases
                else "Add a test case that verifies this requirement."
            )
            body = f'<p class="empty">{_e(hint)}</p>'
        meta = (f'<p class="req-text">{_e(req.text)}</p>' if req.text else "") + (
            f'<p class="cond"><span class="muted">{_e(req.method)}</span> <code>{_e(req.condition.source)}</code></p>'
            f'<p class="reason">{_e(result.reason)}</p>'
        )
        is_open = " open" if result.status in (FAILED, ERROR) else ""
        out.append(
            f'<details class="req"{style}{is_open}><summary class="row">{summary}</summary>'
            f'<div class="detail">{meta}{body}</div></details>'
        )
    return "\n".join(out)


def _coverage_bar(evaluation: Evaluation) -> str:
    counts = evaluation.counts()
    total = max(len(evaluation.leaves), 1)
    segments = "".join(
        f'<span class="seg s-{status}" style="flex:{counts[status]}" title="{counts[status]} {status}"></span>'
        for status in (VERIFIED, RETEST, UNVERIFIED, ERROR, FAILED)
        if counts[status]
    )
    legend = "".join(
        f'<li>{_chip(status)} <span class="num">{counts[status]}</span></li>'
        for status in STATUS_ORDER
        if counts[status]
    )
    return (
        f'<div class="coverage"><p class="cov-figure"><span class="num">{counts[VERIFIED]}</span> of '
        f'<span class="num">{total}</span> requirements verified</p>'
        f'<div class="bar" role="img" aria-label="{counts[VERIFIED]} of {total} verified">{segments}</div>'
        f'<ul class="legend">{legend}</ul></div>'
    )


def _list(items: Iterable[str]) -> str:
    return "".join(f"<li>{i}</li>" for i in items)


def _changes(report: ChangeReport | None) -> str:
    heading = "<h2>Changes since the last verification</h2>"
    if report is None:
        return (
            f'<section class="panel">{heading}<p class="muted">No lock recorded yet. '
            "Run <code>provenant verify</code> to record a baseline.</p></section>"
        )
    if report.clean:
        return f'<section class="panel">{heading}<p>Every verdict matches the lock recorded {_e(report.locked_at)}.</p></section>'
    blocks: list[str] = []
    inputs = [
        f"<code>{_e(c.name)}</code> {_e(c.before or 'new')} → {_e(c.after or 'removed')}" for c in report.design_changes
    ]
    inputs += [
        f"<code>{_e(c.name)}</code> {_e(c.before or 'new')} → {_e(c.after or 'removed')}"
        for c in report.condition_changes
    ]
    if inputs:
        blocks.append(f"<h3>What changed</h3><ul>{_list(inputs)}</ul>")
    if report.status_changes:
        flips = [
            f"<strong>{_e(s.requirement_id)}</strong> {_e(s.title)}: {_chip(s.before)} → {_chip(s.after)}"
            f' <span class="muted">{_e(s.reason)}</span>'
            for s in report.status_changes
        ]
        blocks.append(f"<h3>Verdicts that changed</h3><ul>{_list(flips)}</ul>")
    retest = [s for s in report.stale if s.kind == "retest"]
    reeval = [s for s in report.stale if s.kind == "reevaluate"]
    if retest:
        blocks.append(
            "<h3>Evidence invalidated by design change</h3><ul>"
            + _list(
                f'{_e(s.requirement_id)} from {_e(s.source)} <span class="muted">{_e("; ".join(s.reasons))}</span>'
                for s in retest
            )
            + "</ul>"
        )
    if reeval:
        blocks.append(
            "<h3>Evidence re-evaluated with changed inputs</h3><ul>"
            + _list(
                f"{_e(s.requirement_id)} from {_e(s.source)}: {_e(s.before_measured or '—')} → {_e(s.after_measured or '—')}"
                f' <span class="muted">{_e("; ".join(s.reasons))}</span>'
                for s in reeval
            )
            + "</ul>"
        )
    return (
        f'<section class="panel alert">{heading}'
        f'<p class="muted">Compared with the lock recorded {_e(report.locked_at)}.</p>{"".join(blocks)}</section>'
    )


def _queue(evaluation: Evaluation) -> str:
    if not evaluation.retest_queue:
        return (
            '<section class="panel"><h2>Test queue</h2><p>Every test case has a current run '
            "for the design as it stands.</p></section>"
        )
    items = []
    for item in evaluation.retest_queue:
        last = f"last run {item.last_run.id}" if item.last_run else "no runs yet"
        reasons = "; ".join(item.reasons)
        items.append(
            f"<li><strong>{_e(item.test_case.id)}</strong> {_e(item.test_case.title)} "
            f'<span class="muted">({_e(last)})</span><br>{_e(reasons)}'
            f'<br><span class="muted">verifies {_e(", ".join(item.requirements))}</span></li>'
        )
    return f'<section class="panel"><h2>Test queue</h2><ul class="queue">{"".join(items)}</ul></section>'


def _design(evaluation: Evaluation) -> str:
    project = evaluation.project
    params = "".join(
        f'<tr><th><code>{_e(n)}</code></th><td class="num">{_e(format_si(p.quantity.si, p.quantity.unit))}</td>'
        f'<td class="muted">{_e(p.source)}</td></tr>'
        for n, p in project.design.items()
    )
    outputs = "".join(
        f'<tr><th><code>{_e(n)}</code></th><td class="num">{_e(q)}</td></tr>'
        for n, q in evaluation.model_outputs.items()
    )
    model = project.model
    model_block = ""
    if model:
        error = f'<p class="warn">{_e(evaluation.model_error)}</p>' if evaluation.model_error else ""
        model_block = (
            f"<div><h3>Analysis model <code>{_e(model.name)}</code></h3>"
            f'<p class="muted">{_e(model.description)}</p>{error}<table class="kv">{outputs}</table></div>'
        )
    return (
        f'<section class="panel"><h2>Design baseline</h2><div class="cols"><div><h3>Parameters</h3>'
        f'<table class="kv">{params}</table></div>{model_block}</div></section>'
    )


_CSS = """
:root{
  --bg:#f3f5f6;--surface:#ffffff;--ink:#172026;--muted:#5d6a73;--rule:#d3dadf;--accent:#1c5d99;
  --win:rgba(28,93,153,.08);
  --s-verified:#1f7346;--s-failed:#b3261e;--s-retest:#9a5600;--s-error:#7a3e9d;--s-unverified:#6e7a83;
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#11161a;--surface:#182026;--ink:#e2e7ea;--muted:#93a1aa;--rule:#2b353c;--accent:#77afe3;
  --win:rgba(119,175,227,.10);
  --s-verified:#5cc08a;--s-failed:#f2756e;--s-retest:#e3a24a;--s-error:#c69ae2;--s-unverified:#8d9aa2;
}}
:root[data-theme="dark"]{
  --bg:#11161a;--surface:#182026;--ink:#e2e7ea;--muted:#93a1aa;--rule:#2b353c;--accent:#77afe3;
  --win:rgba(119,175,227,.10);
  --s-verified:#5cc08a;--s-failed:#f2756e;--s-retest:#e3a24a;--s-error:#c69ae2;--s-unverified:#8d9aa2;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Archivo",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.5}
main{max-width:1120px;margin:0 auto;padding:28px 16px 64px}
h1,h2,h3,h4{font-weight:600;line-height:1.2;margin:0}
h2{font-size:1.15rem;margin-bottom:12px}
h3{font-size:.95rem;margin:16px 0 6px}
h4{font-size:.95rem}
code{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:.86em}
.num{font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}
.warn{color:var(--s-retest)}

/* Title block, after an engineering drawing */
.titleblock{display:grid;grid-template-columns:2.2fr repeat(4,1fr);border:2px solid var(--ink);background:var(--surface)}
.titleblock>div{border-left:1px solid var(--ink);padding:8px 12px;min-width:0}
.titleblock>div:first-child{border-left:0;grid-row:span 2;padding:14px 16px;display:flex;flex-direction:column;justify-content:space-between;gap:12px}
.titleblock .cell-b{border-top:1px solid var(--ink)}
.tb-label{display:block;font-size:.72rem;color:var(--muted);font-stretch:112%;letter-spacing:.01em}
.tb-value{display:block;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tb-name{font-size:1.9rem;font-weight:700;font-stretch:88%;letter-spacing:-.01em;line-height:1.05}
.tb-sub{color:var(--muted);font-stretch:112%}

.coverage{margin:20px 0 28px;display:grid;grid-template-columns:auto 1fr auto;gap:18px;align-items:center}
.cov-figure{margin:0;font-weight:600}
.cov-figure .num:first-child{font-size:1.6rem}
.bar{display:flex;height:12px;border:1px solid var(--ink);background:var(--surface)}
.seg{display:block;height:100%}
.seg+.seg{border-left:1px solid var(--surface)}
.seg.s-verified{background:var(--s-verified)}.seg.s-failed{background:var(--s-failed)}
.seg.s-retest{background:var(--s-retest)}.seg.s-error{background:var(--s-error)}
.seg.s-unverified{background:repeating-linear-gradient(135deg,var(--s-unverified) 0 2px,transparent 2px 5px)}
.legend{display:flex;gap:12px;list-style:none;margin:0;padding:0;flex-wrap:wrap}
.legend li{display:flex;gap:5px;align-items:center}

.chip{display:inline-block;font-size:.78rem;font-weight:600;padding:1px 7px;border:1px solid currentColor;border-radius:3px;line-height:1.35;white-space:nowrap}
.chip.s-verified,.chip.s-pass{color:var(--s-verified)}
.chip.s-failed,.chip.s-fail{color:var(--s-failed);background:color-mix(in srgb,var(--s-failed) 12%,transparent)}
.chip.s-retest{color:var(--s-retest)}
.chip.s-error{color:var(--s-error)}
.chip.s-unverified,.chip.s-none{color:var(--s-unverified);border-style:dashed}

.panel{background:var(--surface);border:1px solid var(--rule);padding:16px 18px;margin:0 0 18px}
.panel.alert{border-left:4px solid var(--s-retest)}
.panel ul{margin:6px 0 0;padding-left:18px}
.panel li{margin:4px 0}
.queue li{margin:8px 0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}

.reqs{background:var(--surface);border:1px solid var(--rule);margin-bottom:18px}
.reqs-head,.row{display:grid;grid-template-columns:minmax(0,1fr) 104px 110px 110px 74px;gap:12px;align-items:center;padding:7px 14px}
.reqs-head{font-size:.78rem;color:var(--muted);border-bottom:1px solid var(--rule)}
.req{border-bottom:1px solid var(--rule)}
.req:last-child{border-bottom:0}
.req .c0{padding-left:calc(var(--depth) * 20px);display:flex;gap:10px;min-width:0}
.rid{font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.rtitle{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.req.group .row{background:color-mix(in srgb,var(--bg) 55%,var(--surface))}
.req.group .reason{margin:0;padding:0 14px 8px calc(14px + var(--depth) * 20px);font-size:.85rem;color:var(--muted)}
summary.row{cursor:pointer;list-style:none}
summary.row::-webkit-details-marker{display:none}
summary.row .c0::before{content:"";width:0;height:0;border-left:5px solid var(--muted);border-top:4px solid transparent;border-bottom:4px solid transparent;align-self:center;transition:transform .15s}
details[open]>summary.row .c0::before{transform:rotate(90deg)}
summary.row:hover{background:color-mix(in srgb,var(--accent) 5%,transparent)}
summary.row:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.detail{padding:4px 14px 16px calc(14px + var(--depth) * 20px + 15px)}
.req-text{margin:0 0 6px;max-width:72ch}
.cond{margin:0 0 4px}
.reason{margin:0 0 10px;color:var(--muted);font-size:.88rem}
.empty{color:var(--muted);font-style:italic}

.evidence{border-top:1px dashed var(--rule);padding-top:12px;margin-top:12px}
.evidence header{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.evidence.superseded{opacity:.62}
.plot{display:block;width:100%;max-width:680px;height:auto;margin:6px 0 8px}
.plot .sig{fill:none;stroke:var(--ink);stroke-width:1.2;vector-effect:non-scaling-stroke}
.plot .win{fill:var(--win)}
.plot .frame{stroke:var(--rule)}
.plot .limit{stroke-width:1.2;stroke-dasharray:5 4}
.plot .limit.s-pass{stroke:var(--s-verified)}.plot .limit.s-fail{stroke:var(--s-failed)}.plot .limit.s-error{stroke:var(--s-error)}
.plot .mark{stroke:var(--surface);stroke-width:1.5}
.plot .mark.s-pass{fill:var(--s-verified)}.plot .mark.s-fail{fill:var(--s-failed)}
.plot .axis{font-size:10px;fill:var(--muted);font-family:inherit}
.plot .end{text-anchor:end}
.kv{border-collapse:collapse;font-size:.88rem}
.kv th{text-align:left;font-weight:500;color:var(--muted);padding:2px 16px 2px 0;vertical-align:top;white-space:nowrap}
.kv td{padding:2px 0}
footer{margin-top:28px;color:var(--muted);font-size:.82rem}

@media (max-width:760px){
  .titleblock{grid-template-columns:1fr 1fr}
  .titleblock>div:first-child{grid-column:1/-1;grid-row:auto}
  .titleblock>div{border-left:0;border-top:1px solid var(--ink)}
  .titleblock>div:nth-child(odd):not(:first-child){border-left:1px solid var(--ink)}
  .coverage{grid-template-columns:1fr}
  .reqs-head{display:none}
  .row{grid-template-columns:max-content max-content minmax(0,1fr) auto;row-gap:2px;column-gap:10px}
  .row .c0{grid-column:1/4}
  .row .c1{grid-column:4}
  .row .c2,.row .c3,.row .c4{font-size:.85rem;color:var(--muted)}
  .row .c2{padding-left:calc(var(--depth,0) * 20px + 15px)}
  .req.group .row .c2{padding-left:calc(var(--depth,0) * 20px)}
  .cols{grid-template-columns:1fr}
  .kv th{white-space:normal}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{
  body{background:#fff;font-size:11pt}
  details.req{break-inside:avoid}
  .panel,.reqs{border-color:#999}
}
"""


def render_report(evaluation: Evaluation, report: ChangeReport | None) -> str:
    project = evaluation.project
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if report is None:
        lock_state = "no baseline"
    elif report.clean:
        lock_state = "matches lock"
    else:
        lock_state = f"{len(report.status_changes)} verdict changes"
    what_if = ""
    if project.overrides:
        values = ", ".join(f"{k} = {v}" for k, v in project.overrides.items())
        what_if = (
            f'<section class="panel alert"><h2>What-if evaluation</h2><p>Design overridden for this report: '
            f"{_e(values)}. These verdicts are not part of the recorded baseline.</p></section>"
        )

    leaves = len(evaluation.leaves)
    cells = [
        ("Program", project.program or "—"),
        ("Requirements", f"{leaves} checked, {len(project.requirements)} total"),
        ("Test runs", f"{len(project.runs)} recorded"),
        ("Coverage", f"{evaluation.coverage:.0%}"),
        ("Generated", now),
        ("Baseline", lock_state),
        ("Test cases", str(len(project.test_cases))),
        ("Tool", f"provenant {__version__}"),
    ]
    tb_cells = "".join(
        f'<div class="{"cell-b" if i >= 4 else "cell-a"}"><span class="tb-label">{_e(k)}</span>'
        f'<span class="tb-value">{_e(v)}</span></div>'
        for i, (k, v) in enumerate(cells)
    )
    title = f"{project.name}: verification evidence"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..700&display=swap">
<style>{_CSS}</style>
</head>
<body>
<main>
<header class="titleblock">
  <div><span class="tb-name">{_e(project.name)}</span><span class="tb-sub">Verification evidence report</span></div>
  {tb_cells}
</header>
{_coverage_bar(evaluation)}
{what_if}
{_changes(report)}
{_queue(evaluation)}
<section class="reqs" aria-label="Requirements">
  <div class="reqs-head"><span>Requirement</span><span>Status</span><span>Measured</span><span>Limit</span><span>Margin</span></div>
  {_requirement_rows(evaluation)}
</section>
{_design(evaluation)}
<footer>Each verdict links to the run, raw column, resolved window and data file hash it was computed from.
Regenerate with <code>provenant report</code>.</footer>
</main>
</body>
</html>
"""
