# Software Factory

Software Factory turns explicit human intent and authority into verified outcomes.
Agents may choose how to perform reversible engineering work, but they may act
only within the authority granted to the work and may claim completion only from
recorded evidence.

```mermaid
flowchart LR
    A[Human intent] --> B[Work packet]
    B --> C[Autonomous run]
    C --> D[Evidence]
    D --> E[Completion gate]
    E --> F[Authority decision]
    F --> G[Outcome and receipt]
```

## Operating boundary

- The human owns product intent and consequential design decisions.
- The factory owns routine, reversible implementation judgment.
- Proof that work is complete does not grant permission to merge or deploy it.
- Unknown, stale, or malformed state blocks consequential actions.
- Every run must end complete, blocked, cancelled, or failed by name.
- Repository code and executable data structures are authoritative.
- Human-facing commands must explain that state in plain language.

## Implemented surface

The first executable slice defines and validates:

- a versioned work packet;
- acceptance criteria with evidence requirements;
- resolved and unresolved decisions;
- generic dependency states;
- an authority envelope with explicit grants and denials;
- a computed packet-readiness verdict;
- plain-language packet and readiness views.

Validate or inspect the included example after installing the package:

```sh
factory packet validate examples/basic-change/packet.json
factory packet show examples/basic-change/packet.json
factory packet readiness examples/basic-change/packet.json
factory packet readiness examples/blocked-change/packet.json
```

Readiness is a read-only packet-definition check. It does not create a run or
perform execution preflight.

| Exit code | Meaning |
|---:|---|
| `0` | The packet is ready to enter run preflight. |
| `1` | The packet is valid but has readiness blockers. |
| `2` | The packet is unreadable or malformed. |

The repository deliberately contains no roadmap or implementation plan. Durable
architectural decisions live in `adr/`; current behavior lives in code, tests,
and executable examples.

## Participate

- Use [Discussions](https://github.com/dweeb11/software_factory/discussions) for
  early ideas and architecture questions.
- Report incorrect existing behavior with the bug-report Issue Form.
- Use the work-packet Issue Form for concrete, bounded changes with checkable
  outcomes.

Issue content can propose intent and evidence requirements, but it cannot grant
an agent authority to push, merge, deploy, or perform another consequential
action. Authority is recorded separately by a trusted maintainer.

## License

Licensed under the [Apache License 2.0](LICENSE).
