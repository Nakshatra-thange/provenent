"""Command-line interface.

Exit codes are designed for CI:

    0  success / nothing to report
    1  verification found failed or errored requirements
    2  usage error, invalid project or unreadable input
    3  ``status``: the lock is out of date (stale evidence or changed verdicts)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .demo import write_demo
from .evaluate import (
    ERROR,
    FAIL,
    FAILED,
    PASS,
    RETEST,
    UNVERIFIED,
    VERIFIED,
    Evaluation,
    Evidence,
    RequirementResult,
    evaluate,
)
from .lock import LOCK_NAME, ChangeReport, LockError, build_lock, compare, read_lock, write_lock
from .project import Project, ProjectError, find_project_root
from .reqif import (
    RecordDiff,
    ReqIFError,
    diff_records,
    export_reqif,
    load_records,
    merge_into_yaml,
    records_from_project,
    records_from_yaml,
)
from .units import Quantity, UnitError, format_number, parse_quantity

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_STALE = 0, 1, 2, 3


# --------------------------------------------------------------------------- terminal


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def magenta(self, t: str) -> str:
        return self._wrap("35", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)

    def status(self, status: str | None) -> str:
        label = (status or "—").upper()
        painters: dict[str, Callable[[str], str]] = {
            VERIFIED: self.green,
            PASS: self.green,
            FAILED: self.red,
            FAIL: self.red,
            RETEST: self.yellow,
            ERROR: self.magenta,
            UNVERIFIED: self.dim,
        }
        paint = painters.get(status or "")
        return paint(label) if paint else label


def _visible_len(text: str) -> int:
    length, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            i = text.index("m", i) + 1
            continue
        length += 1
        i += 1
    return length


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def _table(rows: list[list[str]], header: list[str], style: Style) -> str:
    widths = [max(_visible_len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(_pad(style.bold(h), w) for h, w in zip(header, widths, strict=True)).rstrip()]
    for row in rows:
        lines.append("  ".join(_pad(c, w) for c, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _margin(m: float | None) -> str:
    if m is None:
        return ""
    return f"{m * 100:+.1f}%"


_OP_SYMBOL = {"<=": "≤", ">=": "≥", "<": "<", ">": ">"}


def _limit(result: RequirementResult) -> str:
    cond = result.requirement.condition
    return f"{_OP_SYMBOL[cond.op]} {cond.threshold}" if cond else ""


def _governing(result: RequirementResult) -> Evidence | None:
    """The evidence that decides the requirement: lowest margin among what counts."""
    candidates = [e for e in result.active_evidence if e.measured is not None]
    applicable = [e for e in candidates if e.applicable] or candidates
    if not applicable:
        return None
    return min(applicable, key=lambda e: e.margin if e.margin is not None else float("inf"))


# --------------------------------------------------------------------------- helpers


def _root(args: argparse.Namespace) -> Path:
    return find_project_root(args.project) if args.project_search else Path(args.project)


def _load(args: argparse.Namespace) -> Project:
    project = Project.load(_root(args))
    overrides = _parse_overrides(getattr(args, "set", None) or [])
    if overrides:
        project = project.with_overrides(overrides)
    return project


def _parse_overrides(items: list[str]) -> dict[str, Quantity]:
    overrides: dict[str, Quantity] = {}
    for item in items:
        if "=" not in item:
            raise UnitError(f"--set expects name=value, got {item!r}")
        name, value = item.split("=", 1)
        overrides[name.strip()] = parse_quantity(value)
    return overrides


def _print_warnings(project: Project, style: Style) -> None:
    for issue in project.warnings:
        print(style.yellow(f"warning: {issue.where}: {issue.message}"), file=sys.stderr)


def _summary(evaluation: Evaluation, style: Style) -> str:
    counts = evaluation.counts()
    leaves = len(evaluation.leaves)
    parts = [
        f"{counts[VERIFIED]}/{leaves} requirements verified ({evaluation.coverage:.0%})",
        style.red(f"{counts[FAILED]} failed") if counts[FAILED] else f"{counts[FAILED]} failed",
        style.magenta(f"{counts[ERROR]} error") if counts[ERROR] else None,
        style.yellow(f"{counts[RETEST]} need retest") if counts[RETEST] else f"{counts[RETEST]} need retest",
        f"{counts[UNVERIFIED]} unverified",
    ]
    return "Coverage: " + ", ".join(p for p in parts if p)


def _print_retest_queue(evaluation: Evaluation, style: Style) -> None:
    if not evaluation.retest_queue:
        return
    print()
    print(style.bold("Test queue"))
    for item in evaluation.retest_queue:
        last = f"last run {item.last_run.id}" if item.last_run else "no runs yet"
        print(f"  {style.cyan(item.test_case.id)}  {item.test_case.title}  {style.dim('(' + last + ')')}")
        for reason in item.reasons:
            print(f"      {reason}")
        print(f"      {style.dim('verifies ' + ', '.join(item.requirements))}")


def _requirement_table(evaluation: Evaluation, style: Style) -> str:
    rows: list[list[str]] = []
    project = evaluation.project
    for rid, result in evaluation.results.items():
        indent = "  " * project.depth(rid)
        governing = _governing(result)
        measured = governing.measured if governing else ""
        if governing and not governing.applicable:
            measured = style.dim(measured or "")
        rows.append(
            [
                f"{indent}{style.bold(rid)}  {_truncate(result.requirement.title, 34 - len(indent))}",
                style.status(result.status),
                measured or "",
                _limit(result),
                _margin(governing.margin) if governing else "",
                _truncate(result.reason, 64),
            ]
        )
    return _table(rows, ["Requirement", "Status", "Measured", "Limit", "Margin", "Basis"], style)


# --------------------------------------------------------------------------- commands


def cmd_demo(args: argparse.Namespace, style: Style) -> int:
    target = write_demo(Path(args.directory))
    print(f"Created demo project in {target}")
    print(f"Next: cd {target} && provenant verify")
    return EXIT_OK


def cmd_check(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    _print_warnings(project, style)
    print(
        style.green("ok")
        + f"  {project.name}: {len(project.requirements)} requirements, {len(project.test_cases)} test cases, "
        f"{len(project.runs)} runs, {len(project.channels)} channels"
    )
    return EXIT_OK


def cmd_verify(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    _print_warnings(project, style)
    evaluation = evaluate(project)
    print(style.bold(project.name) + (f"  {style.dim(project.program)}" if project.program else ""))
    if project.overrides:
        what_if = ", ".join(f"{k}={v}" for k, v in project.overrides.items())
        print(style.yellow(f"what-if: {what_if} (lock not written)"))
    if evaluation.model_error:
        print(style.magenta(evaluation.model_error))
    print()
    print(_requirement_table(evaluation, style))
    print()
    print(_summary(evaluation, style))
    _print_retest_queue(evaluation, style)
    if not args.no_lock and not project.overrides:
        written = write_lock(project.root, build_lock(evaluation))
        print()
        print(style.dim(f"{'Wrote' if written else 'Unchanged:'} {LOCK_NAME}"))
    counts = evaluation.counts()
    return EXIT_FAILED if counts[FAILED] or counts[ERROR] else EXIT_OK


def _print_changes(report: ChangeReport, style: Style) -> None:
    sections = (
        ("Design changes", report.design_changes, "(new)", "(removed)"),
        ("Requirement changes", report.condition_changes, "(new)", "(removed)"),
        ("Analysis outputs", report.output_changes, "—", "—"),
    )
    for heading, changes, new, removed in sections:
        if not changes:
            continue
        print(style.bold(heading))
        for c in changes:
            print(f"  {c.name}: {c.before or new} -> {c.after or removed}")
        print()

    if report.status_changes:
        print(style.bold("Verdict changes"))
        for s in report.status_changes:
            print(f"  {style.bold(s.requirement_id)}  {s.title}: {style.status(s.before)} -> {style.status(s.after)}")
            print(f"      {style.dim(s.reason)}")
        print()

    groups = [
        ("retest", "Evidence invalidated by design change (physical retest needed)"),
        ("reevaluate", "Evidence to re-evaluate (inputs changed; no new test needed)"),
        ("new", "New evidence"),
        ("removed", "Evidence no longer used"),
    ]
    for kind, heading in groups:
        items = [s for s in report.stale if s.kind == kind]
        if not items:
            continue
        print(style.bold(heading))
        for item in items:
            verdict = ""
            if item.kind == "reevaluate" and (item.before_verdict or item.after_verdict):
                before = f"{style.status(item.before_verdict)} {item.before_measured or ''}".strip()
                after = f"{style.status(item.after_verdict)} {item.after_measured or ''}".strip()
                verdict = f"  {before} -> {after}"
            print(f"  {item.requirement_id} @ {item.source}{verdict}")
            for reason in item.reasons:
                print(f"      {style.dim(reason)}")
        print()


def cmd_status(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    _print_warnings(project, style)
    evaluation = evaluate(project)
    lock = read_lock(project.root)
    if lock is None:
        print(style.yellow(f"No {LOCK_NAME} yet. Run 'provenant verify' to record the verified baseline."))
        return EXIT_STALE
    report = compare(lock, evaluation)
    if report.clean:
        print(style.green("Up to date") + f": every verdict matches {LOCK_NAME} ({report.locked_at})")
        print(_summary(evaluation, style))
        _print_retest_queue(evaluation, style)
        return EXIT_OK
    print(style.bold(f"Changes since {LOCK_NAME}") + style.dim(f" ({report.locked_at})"))
    print()
    _print_changes(report, style)
    print(_summary(evaluation, style))
    _print_retest_queue(evaluation, style)
    print()
    print(style.dim("Run 'provenant verify' to accept the current state into the lock."))
    return EXIT_STALE


def cmd_impact(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    _print_warnings(project, style)
    lock = read_lock(project.root)
    if lock is None:
        print(style.yellow(f"No {LOCK_NAME} to compare against. Run 'provenant verify' first."), file=sys.stderr)
        return EXIT_USAGE
    evaluation = evaluate(project)
    report = compare(lock, evaluation)
    title = "What-if impact" if project.overrides else "Impact of uncommitted changes"
    print(style.bold(title) + style.dim(f"  vs {LOCK_NAME} ({report.locked_at})"))
    if project.overrides:
        print(style.yellow("  " + ", ".join(f"{k} = {v}" for k, v in project.overrides.items())))
    print()
    if not report.stale and not report.status_changes and not report.design_changes and not report.condition_changes:
        print(style.green("No impact: every verdict is unchanged."))
        return EXIT_OK
    _print_changes(report, style)
    leaf_ids = {r.requirement.id for r in evaluation.leaves}
    affected = {s.requirement_id for s in report.status_changes} | {s.requirement_id for s in report.stale}
    flips = sum(1 for s in report.status_changes if s.requirement_id in leaf_ids)
    print(
        f"{len(affected)} requirement(s) affected, {flips} verdict change(s), "
        f"{len(evaluation.retest_queue)} test case(s) in the queue"
    )
    _print_retest_queue(evaluation, style)
    return EXIT_OK


def _seconds(t: float | None) -> str:
    return "—" if t is None else f"{format_number(t)} s"


def _trace_one(rid: str, evaluation: Evaluation, style: Style) -> None:
    project = evaluation.project
    result = evaluation.results[rid]
    req = result.requirement
    print(f"{style.bold(rid)}  {req.title}  [{style.status(result.status)}]")
    if req.text:
        print(f"  {style.dim(req.text)}")
    lineage = []
    parent = req.parent
    while parent:
        lineage.append(parent)
        parent = project.requirements[parent].parent
    if lineage:
        print(f"  parent   {' <- '.join(lineage)}")
    children = project.children(rid)
    if children:
        print(f"  children {', '.join(c.id for c in children)}")
    if req.condition is None:
        print(f"  {result.reason}")
        return
    print(f"  method   {req.method}")
    print(f"  check    {req.condition.source}")
    print(f"  status   {result.reason}")
    if req.method != "test":
        for e in result.evidence:
            outcome = e.measured or e.message
            print(f"  └─ current design   {style.status(e.verdict)}  {outcome}  margin {_margin(e.margin)}")
            for name, value in e.provenance.parameters.items():
                source = project.design[name].source if name in project.design else ""
                print(f"       {name} = {value}" + (style.dim(f"   ({source})") if source else ""))
        return
    cases = project.test_cases_for(rid)
    if not cases:
        print(f"  {style.dim('no test case verifies this requirement')}")
    for tc in cases:
        print(f"  └─ {style.cyan(tc.id)}  {tc.title}")
        runs = [e for e in result.evidence if e.test_case_id == tc.id]
        voids = [r for r in project.runs_for(tc.id) if r.void]
        if not runs and not voids:
            print(f"       {style.dim('not yet run')}")
        for e in sorted(runs, key=lambda e: (e.run.date if e.run else "", e.source), reverse=True):
            run = e.run
            assert run is not None
            tag = style.dim(f"superseded by {e.superseded_by}") if e.superseded_by else "latest"
            print(f"     └─ {style.bold(run.id)}  {run.date}  {run.article}  ({tag})")
            verdict = e.measured if e.verdict != ERROR else e.message
            limit = f"{_OP_SYMBOL[req.condition.op]} {e.threshold}"
            print(f"          {style.status(e.verdict)}  {verdict}  limit {limit}  margin {_margin(e.margin)}")
            pv = e.provenance
            if pv.column:
                print(f"          channel {pv.channel} <- column {pv.column} [{pv.raw_unit}]")
            if pv.window:
                print(
                    f"          window  {pv.window} = {_seconds(pv.t0)} … {_seconds(pv.t1)}"
                    + style.dim(f"  ({pv.window_definition})")
                )
            else:
                print(f"          window  entire record ({_seconds(pv.t0)} … {_seconds(pv.t1)})")
            if pv.t_at is not None:
                print(f"          at      t = {_seconds(pv.t_at)}")
            if pv.samples is not None:
                print(f"          samples {pv.samples} valid, {pv.missing} missing")
            sha = (pv.data_sha256 or "")[:12]
            print(f"          data    {pv.data_file}" + (style.dim(f"  sha256:{sha}…") if sha else ""))
            for d in e.config_diffs:
                print(style.yellow(f"          config  {d.parameter}: tested {d.recorded}, design now {d.current}"))
        for void_run in voids:
            print(f"     └─ {style.dim(void_run.id + '  ' + void_run.date + '  void: ' + (void_run.void or ''))}")


def cmd_trace(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    evaluation = evaluate(project)
    missing = [r for r in args.requirements if r not in evaluation.results]
    if missing:
        print(f"unknown requirement(s): {', '.join(missing)}", file=sys.stderr)
        return EXIT_USAGE
    for n, rid in enumerate(args.requirements):
        if n:
            print()
        _trace_one(rid, evaluation, style)
    return EXIT_OK


def cmd_report(args: argparse.Namespace, style: Style) -> int:
    from .report import render_report

    project = _load(args)
    evaluation = evaluate(project, keep_traces=True)
    lock = None
    try:
        lock = read_lock(project.root)
    except LockError as exc:
        print(style.yellow(f"warning: {exc}"), file=sys.stderr)
    report = compare(lock, evaluation) if lock else None
    output = Path(args.output) if args.output else project.root / "provenant-report.html"
    output.write_text(render_report(evaluation, report), encoding="utf-8")
    print(f"Wrote {output}")
    return EXIT_OK


def cmd_reqif_export(args: argparse.Namespace, style: Style) -> int:
    project = _load(args)
    data = export_reqif(records_from_project(project), project.name)
    output = Path(args.output) if args.output else project.root / f"{project.root.name}.reqif"
    output.write_bytes(data)
    print(f"Wrote {output} ({len(project.requirements)} requirements)")
    return EXIT_OK


def _print_record_diff(diff: RecordDiff, style: Style) -> None:
    for r in diff.added:
        print(style.green(f"+ {r.id}  {r.title}"))
    for r in diff.removed:
        print(style.red(f"- {r.id}  {r.title}"))
    for rid, changes in diff.modified.items():
        print(style.yellow(f"~ {rid}"))
        for c in changes:
            print(f"    {c.field}: {c.before!r} -> {c.after!r}")


def cmd_reqif_import(args: argparse.Namespace, style: Style) -> int:
    root = _root(args)
    incoming = load_records(Path(args.file))
    yaml_path = root / "requirements.yaml"
    if args.dry_run:
        current = records_from_yaml(yaml_path) if yaml_path.is_file() else []
        _print_record_diff(diff_records(current, incoming), style)
        print(style.dim("dry run: requirements.yaml not modified"))
        return EXIT_OK
    original = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else None
    diff = merge_into_yaml(yaml_path, incoming, prune=args.prune)
    try:
        Project.load(root)
    except ProjectError as exc:
        if original is not None:
            yaml_path.write_text(original, encoding="utf-8")
        else:
            yaml_path.unlink(missing_ok=True)
        print(style.red("import rolled back: the merged requirements do not validate"), file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if diff.empty:
        print("No changes.")
    else:
        _print_record_diff(diff, style)
        print(f"\nUpdated {yaml_path}. Run 'provenant impact' to see what this breaks.")
    return EXIT_OK


def cmd_reqif_diff(args: argparse.Namespace, style: Style) -> int:
    before, after = load_records(Path(args.before)), load_records(Path(args.after))
    diff = diff_records(before, after)
    if diff.empty:
        print("No differences.")
        return EXIT_OK
    _print_record_diff(diff, style)
    print()
    print(f"{len(diff.added)} added, {len(diff.removed)} removed, {len(diff.modified)} modified")
    return EXIT_FAILED if args.exit_code else EXIT_OK


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    # Global options are accepted before or after the subcommand. Subcommands use
    # SUPPRESS defaults so an option given before the subcommand is not reset.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--project", default=argparse.SUPPRESS, help="project directory")
    common.add_argument("--color", choices=("auto", "always", "never"), default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="provenant",
        description="Executable requirements with change-impact tracing for hardware test data.",
    )
    parser.add_argument("--version", action="version", version=f"provenant {__version__}")
    parser.add_argument("-C", "--project", default=".", help="project directory (default: search upward from cwd)")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    def command(name: str, help_text: str, func: Callable[[argparse.Namespace, Style], int]) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(func=func)
        return p

    def what_if(p: argparse.ArgumentParser) -> None:
        p.add_argument("--set", action="append", metavar="NAME=VALUE", help="what-if design override (repeatable)")

    command("demo", "create the TE-2 engine demo project", cmd_demo).add_argument("directory")
    command("check", "validate the project without evaluating", cmd_check)

    p = command("verify", "evaluate every requirement and update the lock", cmd_verify)
    p.add_argument("--no-lock", action="store_true", help=f"do not write {LOCK_NAME}")
    what_if(p)

    command("status", "show what changed since the lock (exit 3 if out of date)", cmd_status)
    what_if(command("impact", "trace the impact of changes or what-if overrides", cmd_impact))

    p = command("trace", "follow requirements to the evidence behind them", cmd_trace)
    p.add_argument("requirements", nargs="+", metavar="REQ")

    p = command("report", "write a self-contained HTML verification report", cmd_report)
    p.add_argument("-o", "--output", help="output file (default: provenant-report.html in the project)")
    what_if(p)

    reqif = sub.add_parser("reqif", help="exchange requirements as ReqIF").add_subparsers(
        dest="reqif_command", metavar="action", required=True
    )
    p = reqif.add_parser("export", help="export requirements to a .reqif file", parents=[common])
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_reqif_export)
    p = reqif.add_parser("import", help="merge a .reqif file into requirements.yaml", parents=[common])
    p.add_argument("file")
    p.add_argument("--prune", action="store_true", help="remove requirements missing from the file")
    p.add_argument("--dry-run", action="store_true", help="show the changes without writing")
    p.set_defaults(func=cmd_reqif_import)
    p = reqif.add_parser(
        "diff", help="compare two baselines (.reqif, requirements.yaml or project dir)", parents=[common]
    )
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--exit-code", action="store_true", help="exit 1 when there are differences")
    p.set_defaults(func=cmd_reqif_diff)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.project_search = args.project == "."
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    style = Style(color)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="replace")
    try:
        return int(args.func(args, style))
    except ProjectError as exc:
        print(style.red("error: invalid project"), file=sys.stderr)
        for issue in exc.issues:
            line = f"  {issue.where}: {issue.message}"
            print(style.red(line) if issue.severity == "error" else style.yellow(line), file=sys.stderr)
        return EXIT_USAGE
    except (LockError, ReqIFError, UnitError, FileExistsError, FileNotFoundError) as exc:
        print(style.red(f"error: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - output piped into e.g. `head`
        # Point stdout at devnull so the interpreter's final flush does not raise again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
