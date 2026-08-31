# ADR 0009: Guard activation through canonical run publication

## Status

Accepted

## Context

A durable run record can be copied, linked, renamed, or supplied through more than
one pathname. If each caller-selected path could act as the mutable run, two
controllers could independently commit activation for what the protocol identifies
as one run. Controller ownership alone also cannot establish that current packet,
environment, workspace, and worker-handoff facts still match the run that will be
activated.

Activation spans an external preparation interval. Holding a filesystem lock while
an adapter prepares a worker would couple protocol availability to an unbounded
runtime operation, but marking the run active before preparation would create an
orphaned active run after failure.

## Decision

Each initialized run is registered once in an account-scoped coordination namespace
with one immutable publication record naming its exact canonical run entry and the
digest of its initialized bytes. Copies, symbolic links, hard links, renames, and
substituted initialized bytes are not adopted. Missing, malformed, or uncertain
publication state blocks.

A permanent per-run advisory transaction lock serializes compliant controller-claim
mutations and activation operations. The lock is not held while an external worker
is prepared.

Activation uses three durable steps:

1. While the exact publication-bound controller claim is current, recompute
   preflight from current packet and environment facts and publish an immutable
   activation attempt bound to the publication, initialized run bytes, claim event,
   packet, environment, workspace, and evaluator mode.
2. After an adapter reports an idle prepared worker, record one immutable
   worker-ready observation for that attempt. This is a controller-recorded
   observation, not independent proof or authority.
3. Revalidate the publication, exact claim, attempt, receipt, packet, and preflight
   freshness, then atomically replace the canonical run with the single
   `initialized` to `active` transition. That transition is the activation commit
   point and binds the claim, attempt digest, worker identity, and ready-receipt
   digest.

There is no mutable current-activation file and no generic state-setting command.
Exact retries validate the complete persisted binding and reconfirm run-record
durability. A different attempt, claim, packet, receipt, or worker conflicts rather
than being treated as an idempotent retry.

## Consequences

- One local run publication, not a caller-selected alias, owns the mutable lifecycle
  commit point.
- Controller transfer or recovery cannot race a compliant activation commit under
  the same local account.
- A crash before the active transition leaves an inspectable attempt or ready
  observation while the run remains initialized.
- `active` means only that the guarded handoff was committed. It does not mean the
  worker began, work completed, evidence passed, or an external action is
  authorized.
- The first implementation depends on POSIX account identity, `flock`, and local
  filesystem durability. A distributed supervisor may replace those mechanisms
  while preserving the publication, binding, and commit semantics.
- Actual worker preparation, ACP session creation, and the later begin instruction
  remain adapter work outside this decision.

## Reference

- [Work packet #6](https://github.com/dweeb11/software_factory/issues/6)
