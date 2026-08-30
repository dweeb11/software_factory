# ADR 0006: Derive run state from transition history

## Status

Accepted

## Context

Autonomous work must survive conversation loss, worker exit, restart, and handoff.
A mutable current-state field alone cannot explain how a run arrived there, while
storing both current state and transition history creates two facts that can
disagree. Run initialization must also bind one attempt to the exact intent it
observed without treating readiness evidence as permission to act.

## Decision

A run is a durable record of one attempt to perform one exact work-packet
version. The packet version is identified by a SHA-256 digest of the packet bytes
used during initialization.

Run lifecycle state is represented by an ordered, validated transition history.
The final transition determines current state; no separate authoritative
current-state field is stored. Every transition records its sequence, timestamp,
prior state, next state, reason, and recording identity. Terminal states cannot
transition further.

Initialization recomputes packet readiness and creates transition zero from no
state to `initialized` only for a ready packet. The run records the readiness
verdict it observed for audit. Creating this record starts no worker, performs no
external action, and grants no authority.

The lifecycle distinguishes resumable `waiting` from terminal `blocked`.
Terminal states are `complete`, `blocked`, `cancelled`, `exhausted`, and `failed`.

## Consequences

- Run history explains current state without relying on a transcript.
- A changed packet cannot silently replace the packet version bound to a run.
- Cosmetic byte changes conservatively produce a new packet version.
- A blocked packet produces no run record.
- Future transition commands must enforce the invariant that authorizes each
  transition rather than exposing unrestricted state mutation.
- Packet storage, execution runtimes, forges, trackers, and worker sessions remain
  outside this decision.

## Reference

- [Work packet #3](https://github.com/dweeb11/software_factory/issues/3)
