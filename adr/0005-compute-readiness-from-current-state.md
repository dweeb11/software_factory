# ADR 0005: Compute readiness from current state

## Status

Accepted

## Context

A valid work packet can still be unready for autonomous execution because scope
is incomplete, a consequential decision is unresolved, or a dependency is not
satisfied. Persisting a separate readiness file would create a stale parallel
source whenever the packet or its dependencies change.

## Decision

Packet readiness is computed from current packet facts. The evaluator produces a
deterministic report containing every blocker and performs no writes or external
actions.

A future run initializer may record the readiness verdict it observed for audit,
bound to the packet version and other evaluated inputs. That historical verdict
will not authorize a later run and will not replace a fresh evaluation.

Readiness is distinct from execution preflight. Packet readiness asks whether the
work is sufficiently defined. Execution preflight will ask whether the current
environment can perform it.

## Consequences

- Changing a packet or dependency changes the next readiness verdict without
  synchronizing another authoritative file.
- A valid but blocked packet is distinct from malformed input.
- Autonomous controllers can use stable exit codes for ready, blocked, and
  malformed outcomes.
- External tracker and runtime checks remain outside packet readiness.

## Reference

- [Work packet #2](https://github.com/dweeb11/software_factory/issues/2)
