# Provenant

**Executable requirements with change-impact tracing for hardware test data.**

Hardware programs verify requirements against test data, then the design moves:
a chamber pressure is retuned, a load cell is recalibrated, a threshold is
tightened at review. Nobody re-checks which verdicts that silently invalidated.
Provenant makes every requirement a check that runs itself against the data,
records exactly which inputs each verdict depended on, and, when anything
changes, tells you what broke, why, and whether you need to recompute or
physically re-test.

```text
$ provenant impact --set chamber_pressure_nominal=100bar

What-if impact  vs provenant.lock
  chamber_pressure_nominal = 100 bar

Analysis outputs
  thrust_vac: 22.03 kN -> 18.36 kN

Verdict changes
  PRP-011  Predicted vacuum thrust: VERIFIED -> FAILED
      18.36 kN vs 20 kN (design)
  PRP-010  Measured vacuum thrust: VERIFIED -> RETEST
      RUN-0003 tested a different configuration (chamber_pressure_nominal)
  ...

Test queue
  TC-HF-001  Full-duration hot fire  (last run RUN-0003)
      chamber_pressure_nominal: tested at 120 bar, design is now 100 bar
      verifies PRP-010, PRP-031, PRP-050, TCS-020
```

No LLM in the loop: this is the deterministic backbone that answers "is this
evidence still valid?" with a reason you can audit.

## Quickstart

```bash
pip install -e .                 # Python 3.10+, one dependency (PyYAML)

provenant demo te2               # write the TE-2 engine demo project
cd te2
provenant verify                 # evaluate everything, record provenant.lock
provenant trace TCS-020          # follow a verdict to the sample that decided it
provenant impact --set expansion_ratio=40   # what-if, touches nothing on disk
provenant report                 # self-contained HTML report
```

Don't want to install anything? Open `examples/report-baseline.html` and
`examples/report-what-if.html` in a browser.

## The 60-second demo

The demo is a small upper-stage engine program (the TE-2 example from Tensol's
own landing page) with 13 requirements, 3 test cases, 4 hot-fire runs and a
first-order nozzle performance model. The data is synthetic but physical:
under-damped start-up transients, thermal lag on the throat wall, sensor noise,
a thermocouple dropout, and a DAQ column rename between test campaigns.

1. `provenant verify`: 9 of 10 checkable requirements verified. PRP-060
   (restart) is unverified because its test case has never been run, so it sits
   in the test queue.
2. Edit `design.yaml`, chamber pressure 120 to 100 bar, then `provenant status`.
   Predicted thrust falls below 20 kN, so PRP-011 fails. Five test-verified
   requirements move to **retest**, because the runs behind them were fired at
   120 bar and the test cases declare they are sensitive to chamber pressure.
   Engine mass is untouched, because nothing ties it to chamber pressure.
3. Revert, then tighten TCS-020 from 900 K to 850 K. Now the verdict flips to
   failed from the existing data with **no retest needed**: the requirement
   changed, not the hardware.
4. Add `scale: 1.02` to the thrust load cell in `channels.yaml`. Exactly one
   piece of evidence goes stale (thrust from RUN-0003). Temperature, pressure
   and duration results are untouched because they never read that channel.

`status` exits with code 3 whenever the lock is out of date, so it works as a
CI gate on the repository that holds the requirements.

## Writing requirements

Requirements live in YAML, with a machine-checkable condition:

```yaml
- id: TCS-020
  parent: SYS-001
  title: Throat wall temperature
  method: test
  condition: max(throat_wall_temp) <= 900 K during steady_state
```

| Form | Meaning |
|---|---|
| `max(ch)`, `min(ch)`, `mean(ch)`, `initial(ch)`, `final(ch)` | Reduce a channel over the record or a window. `mean` is time-weighted, so irregular sampling and dropouts don't bias it. |
| `duration(ch >= 108 bar)` | Total time the inner comparison holds, interpolated at crossings so it doesn't depend on sample placement. |
| `value(name)` | A design parameter or an analysis-model output, for `analysis` and `inspection` requirements. |
| `during window` | Restrict to a window defined by the test case. |

Units are first-class. `20 kN`, `20000 N` and `4496 lbf` mean the same thing,
and comparing a force against a pressure is a load-time error, not a wrong
answer. Supported dimensions include pressure, force, temperature (with
offsets), mass, length, time, mass flow and velocity.

Windows are defined per test case from events detected in the data, so the
same requirement is judged over the physically equivalent part of every run
even when timing differs:

```yaml
events:
  ignition: {channel: chamber_pressure, rises_above: 10 bar}
  shutdown: {channel: chamber_pressure, falls_below: 10 bar, after: ignition}
windows:
  steady_state: {start: ignition + 2 s, end: shutdown - 1 s}
```

## Project layout

```text
project.yaml        name, program, analysis model
requirements.yaml   requirement tree with conditions
test_cases.yaml     what each test verifies, its events and windows,
                    and which design parameters it is sensitive to
channels.yaml       logical channel -> raw DAQ column, unit, calibration, aliases
design.yaml         current design parameters, with tolerances
runs/*.yaml + .csv  one metadata file and one data file per test run
provenant.lock      the verified baseline (commit this)
```

## Commands

| Command | What it does |
|---|---|
| `verify` | Evaluate every requirement, print the table, update the lock. Exit 1 if anything failed. |
| `status` | Compare against the lock. Exit 3 if any evidence is stale or any verdict changed. |
| `impact [--set k=v]` | Same comparison, framed as impact. With `--set`, a what-if that never touches disk. |
| `trace REQ...` | Requirement, test case, run, raw column, window, governing sample, data hash. |
| `report [-o file]` | Offline HTML report with signal plots, the change summary and the test queue. |
| `check` | Validate the project and list every problem at once. |
| `reqif export / import / diff` | Exchange requirements with DOORS, Polarion, Jama and suppliers. |
| `demo DIR` | Write the demo project. |

## How it works

**Fingerprints, not timestamps.** Each verdict stores a hash of exactly the
inputs it consumed: the canonical condition, the window and events it used, the
run's data file, the mappings of the channels it read, and the run's recorded
configuration. A change marks only the evidence whose inputs changed, and the
reason is the name of the component whose hash moved. This is the same idea as
a content-addressed build cache (Bazel, Nix).

**Canonical forms.** Conditions and quantities are hashed in SI after parsing,
so `max(p) <= 150 bar` and `max( p )<=15 MPa` are the same requirement and a
reformat does not invalidate anything.

**Re-evaluate vs retest.** These are different costs and Provenant keeps them
apart. If an input changed (threshold, calibration, window), it recomputes from
the data already on disk. If the design moved outside the tolerance recorded
with the run for a parameter the test case is sensitive to, no amount of
recomputing helps: the test case goes into the queue.

**Superseded and void runs.** The latest run of a test case speaks for it;
earlier runs stay visible in `trace` and the report but no longer count. Runs
marked `void` (a facility abort, say) are excluded with their reason shown.

**Lock file.** `provenant.lock` is deterministic JSON, written atomically and
only when its content changes, so it diffs cleanly in code review.

**ReqIF.** Export is well-formed ReqIF 1.2. Import is tolerant of other tools'
conventions (`Object Identifier`, `Object Heading`, XHTML text, enumeration
values) and merging keeps local fields the other side doesn't carry, so a
supplier round-trip through a tool that drops `Provenant.Condition` doesn't
erase your checks. An import that would leave the project invalid is rolled
back.

## Design decisions

- **A small condition language instead of Python expressions.** Requirements
  are reviewed by systems engineers, not programmers, and must be safe to load
  from a supplier. The grammar is parsed, never `eval`-ed, and errors point at
  the exact character.
- **Test cases declare their sensitivities.** Deciding that a hot-fire is
  sensitive to chamber pressure but not to engine mass is an engineering
  judgment. Making it explicit and reviewable beats inferring it.
- **Errors are verdicts, not crashes.** A missing event, a corrupt file or a
  failing model becomes an `error` status on the affected requirements, with
  the reason, while everything else still evaluates.
- **Validation reports everything at once.** A broken project gets one complete
  list of problems instead of a fix-one, rerun loop.

## Limitations and next steps

- Data input is CSV; HDF5 and TDMS readers would slot in behind `Recording`.
- One built-in analysis model. A plugin entry point for program-specific models
  is the obvious extension.
- Run-to-run statistics (for example, a requirement on the 3-sigma of a value
  across all qualification firings) are not expressible yet.
- Traceability is to the run level. Linking evidence to specific hardware
  serial numbers and their as-built configuration is the next layer.

## Development

```bash
pip install -e ".[dev]"
pytest --cov=provenant   # 181 tests, 96% line coverage
ruff check src tests
mypy                     # strict
```

The physics model is checked against standard isentropic tables and the
Sutton & Biblarz thrust-coefficient charts.

## License

MIT
