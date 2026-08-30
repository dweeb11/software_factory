# ADR 0002: Separate proof from authority

## Status

Accepted

## Context

Tests, reviews, and runtime observations can prove that a candidate satisfies its
configured requirements. They do not prove that the factory is permitted to
push, merge, deploy, or perform another consequential action. Combining these
questions would let evidence accidentally grant authority.

## Decision

Completion and authorization are separate records and decisions. A completion
gate determines whether work is proven complete. An authority envelope
determines which action, if any, may follow. Explicit denials override grants.

## Consequences

- A completed candidate may wait for human authorization.
- A previously authorized candidate may proceed automatically after passing its
  gate.
- Changing authority does not change the completion evidence.
- Changing the candidate invalidates evidence bound to its prior revision.
