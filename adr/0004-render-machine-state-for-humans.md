# ADR 0004: Render machine state for humans

## Status

Accepted

## Context

Executable records are less ambiguous than prose, but raw code and structured
data are not an appropriate primary interface for every operator. Making code
the source of truth must not require a person to read implementation syntax to
understand what the factory is doing.

## Decision

Code and executable records remain authoritative. The factory provides
plain-language views over work, authority, evidence, blockers, and outcomes.
These views are generated from current records rather than maintained as a
second source of truth.

## Consequences

- Every important machine record needs a human-readable renderer.
- Command output explains impact, current state, required action, and relevant
  authority without exposing internal syntax unnecessarily.
- Examples and behavioral test names supplement generated views but do not
  replace them.
