# ADR 0001: Use a protocol-centered kernel

## Status

Accepted

## Context

Agent runtimes, models, trackers, forges, and terminal managers change faster
than the invariants of trustworthy work. Embedding one vendor's commands or one
agent's workflow in the factory core would make execution details define the
product.

## Decision

The factory is centered on versioned work, run, evidence, completion, and
authority records. Exact state transitions and gates belong to executable code.
Runtimes and external systems integrate through adapters around that protocol.

## Consequences

- A run can move between compatible execution backends without changing its
  meaning.
- External integrations must translate into protocol records rather than add
  vendor fields to the core.
- Protocol changes require explicit versioning.
- The protocol must remain smaller and more stable than the adapters around it.
