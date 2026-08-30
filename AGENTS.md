# Software Factory repository contract

## Purpose

Build a runtime-neutral factory that turns versioned work packets into verified
outcomes under explicit authority. Keep the protocol stable and agents,
runtimes, trackers, forges, and verification tools replaceable.

## Repository truth

- Keep plans, roadmaps, speculative task lists, and session notes outside this
  repository.
- Commit only current behavior, executable examples, tests, concise user-facing
  documentation, and durable architectural decisions.
- Record a decision in `adr/` only when its rationale should outlive the change
  that implements it.
- Code and executable structures are authoritative for behavior. Do not maintain
  a second prose specification of implementation details.
- Human-facing commands must render machine state in plain language. A user
  should not need to read Python or JSON to understand status, blockers,
  evidence, or authority.

## Architectural boundaries

- Keep proof of completion separate from authority to act.
- Keep durable intent separate from transactional run state.
- No transcript is a source of truth.
- No adapter decides overall completion.
- No verifier mutates the candidate it certifies.
- No runtime, forge, tracker, model, or vendor path belongs in the core protocol.
- No unknown or malformed state may authorize a consequential action.
- No run disappears without a named terminal state.

## Engineering defaults

- Prefer the smallest complete implementation and the Python standard library.
- Add dependencies only when they materially simplify the current requirement.
- Resolve uncertainty with evidence rather than speculative code.
- Test behavior at public boundaries.
- Use names that communicate domain ownership; avoid generic `utils`, `helpers`,
  `common`, or `manager` modules.
- Do not add procedural scaffolding unless an evaluation shows it improves over
  the work packet, repository contract, and available verification alone.

## Validation

Run the narrowest relevant tests first. The current suite is:

```sh
python3 -m unittest discover -s tests -v
```
