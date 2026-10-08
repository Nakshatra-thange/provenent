# Examples

Open these in a browser; nothing needs installing.

- `report-baseline.html`: the TE-2 demo as verified. Nine of ten checkable
  requirements pass; the restart test has never been run.
- `report-what-if.html`: the same project with chamber pressure lowered from
  120 to 100 bar as a what-if. Predicted thrust fails, five test-verified
  requirements need a retest, and the change summary explains each one.
- `te2/`: the demo project itself, with its `provenant.lock`. Try
  `provenant -C examples/te2 trace TCS-020`.
- `te2.reqif`: the requirements exported as ReqIF 1.2.

To regenerate: `provenant demo te2 && cd te2 && provenant verify && provenant report`.
