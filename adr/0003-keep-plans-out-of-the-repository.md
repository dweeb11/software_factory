# ADR 0003: Keep plans out of the repository

## Status

Accepted

## Context

Implementation plans describe a temporary route through uncertainty. Once the
code changes, committed plans become stale parallel descriptions of behavior.
They obscure current truth and create maintenance work without improving the
running system.

## Decision

Plans, roadmaps, speculative task lists, and session notes remain outside the
repository. The repository contains current code, tests, executable examples,
concise operating documentation, and architectural decisions whose rationale is
expected to outlive their implementation.

## Consequences

- Work planning happens in conversations, external notes, issue trackers, or
  transient run state.
- Accepted architectural decisions may be recorded as ADRs.
- Current behavior is learned from executable artifacts rather than historical
  plans.
- Contributors must not create `PLAN.md`, `ROADMAP.md`, or equivalent planning
  documents in this repository.
