# ADR 0008: Derive controller ownership from claim history

## Status

Accepted

## Context

A factory run must not activate more than one worker controller at a time. A
process-local lock disappears during the failure it must help reconcile, while a
time-limited lease can authorize takeover merely because clocks advanced or a
heartbeat was delayed. Silently replacing an ownership file would erase the
provenance needed to distinguish a planned handoff from crash recovery.

## Decision

Controller ownership is represented by a durable, append-only claim history bound
to one run. The local CLI uses one coordination namespace anchored to the operating
system account identity, not its mutable `HOME` environment, and derives a claim
location from the immutable run identifier rather than accepting a caller-selected
location or relying on the run record's mutable pathname. Initial
acquisition exclusively creates that claim directory and its first event; an
existing directory is contention and is never overwritten.

Current ownership is derived from the final event in a contiguous, validated
sequence. There is no separately stored current-owner field. Transfer and recovery
both require the exact current claim identifier and publish the next immutable
event, recording the successor controller, reason, time, and recording identity.
Concurrent changes contend on the same next sequence path, so at most one can be
published.

A controller claim is not a lease and has no automatic expiration. Age may inform
inspection, but it never authorizes takeover. Missing, malformed, ambiguous,
incomplete, or uncertain claim state blocks ownership-dependent action.

## Consequences

- Controller ownership survives controller-process and conversation loss.
- Competing initial acquisition and ownership changes fail without overwriting the
  winning record.
- Every planned transfer and explicit recovery leaves a durable receipt.
- Recovery remains an explicit consequential operation; the factory does not infer
  permission from elapsed time.
- The local filesystem and one resolvable POSIX-account coordination namespace
  supply exclusivity for the first usable implementation. An unavailable account
  database blocks rather than selecting a namespace from mutable environment.
  Multi-user or distributed controllers require a shared supervisor; they are
  outside this guarantee. A future supervisor may replace this storage mechanism
  without changing the ownership semantics.
- Claims identify the controller allowed to coordinate a run; they do not grant
  worker, merge, deployment, or other authority.

## Reference

- [Work packet #5](https://github.com/dweeb11/software_factory/issues/5)
