# ADR 0007: Separate observation from gate decisions

## Status

Accepted

## Context

Some execution facts are mechanically observable, while others require repository
interpretation or human judgment. An agent can help discover commands, policies,
capabilities, and verification routes, but an agent's confidence is not a stable
or auditable authorization boundary. Conversely, a deterministic gate cannot
itself discover every fact without coupling the core to runtimes, repositories,
or vendors.

## Decision

Collectors produce versioned observations with provenance and timestamps.
Collectors may be deterministic probes, adapters, agents, or humans. They do not
decide whether a gate passes.

A deterministic evaluator consumes the current protocol records and collected
observations, validates their identity and freshness, accumulates every blocker,
and produces the gate verdict. The evaluator performs no writes or external
actions. Unknown, stale, malformed, missing, or negative required observations
block.

Execution preflight follows this division. A collected environment snapshot
reports controller availability, workspace state, execution capabilities,
verification routes, requested worker actions, and current authority status. The
preflight evaluator independently rechecks the run, exact packet version, and
packet readiness before evaluating that snapshot.

A passing preflight verdict does not acquire controller ownership, transition the
run, start a worker, or grant authority. Activation must recheck mutable facts
while enforcing single-writer ownership.

## Consequences

- Inspection agents can improve discovery without becoming protocol authorities.
- The same evaluator can consume observations from different runtimes and tools.
- Gate behavior is reproducible for the same records, observations, and evaluation
  time.
- Collector trust and identity authentication remain separate policy concerns.
- A stale passing snapshot cannot authorize later execution.
- Future completion gates can apply the same pattern to deterministic, agent,
  human, and runtime evidence.

## Reference

- [Work packet #4](https://github.com/dweeb11/software_factory/issues/4)
