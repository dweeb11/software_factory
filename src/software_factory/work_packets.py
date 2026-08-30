from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


PACKET_KINDS = frozenset({"inquiry", "experiment", "change", "program"})
EVIDENCE_KINDS = frozenset({"source", "test", "runtime", "review", "human"})
AUTHORITY_ACTIONS = frozenset(
    {
        "edit",
        "commit",
        "push",
        "open-pr",
        "update-pr",
        "update-tracker",
        "merge",
        "deploy",
    }
)
AUTHORITY_PRESETS = {
    "advise": frozenset(),
    "build": frozenset({"edit", "commit"}),
    "deliver": frozenset(
        {"edit", "commit", "push", "open-pr", "update-pr", "update-tracker"}
    ),
    "land": frozenset(
        {
            "edit",
            "commit",
            "push",
            "open-pr",
            "update-pr",
            "update-tracker",
            "merge",
        }
    ),
    "operate": AUTHORITY_ACTIONS,
}


class PacketError(ValueError):
    """A work packet is unreadable or violates the protocol."""


@dataclass(frozen=True)
class Scope:
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    statement: str
    evidence_required: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    id: str
    question: str
    state: str
    decision: str | None
    decided_by: str | None


@dataclass(frozen=True)
class AuthorityEnvelope:
    mode: str
    granted_by: str
    allow: frozenset[str]
    deny: frozenset[str]

    @property
    def effective_actions(self) -> frozenset[str]:
        return (AUTHORITY_PRESETS[self.mode] | self.allow) - self.deny

    def allows(self, action: str) -> bool:
        if action not in AUTHORITY_ACTIONS:
            raise PacketError(f"unknown authority action: {action}")
        return action in self.effective_actions


@dataclass(frozen=True)
class WorkPacket:
    schema_version: int
    id: str
    kind: str
    intent: str
    desired_outcome: str
    scope: Scope
    acceptance: tuple[AcceptanceCriterion, ...]
    decisions: tuple[Decision, ...]
    authority: AuthorityEnvelope

    @property
    def unresolved_decisions(self) -> tuple[Decision, ...]:
        return tuple(decision for decision in self.decisions if decision.state == "unresolved")

    @classmethod
    def from_path(cls, path: str | Path) -> WorkPacket:
        packet_path = Path(path)
        try:
            raw = json.loads(packet_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise PacketError(f"cannot read packet {packet_path}: {error}") from error
        except json.JSONDecodeError as error:
            raise PacketError(
                f"packet {packet_path} is not valid JSON: line {error.lineno}, column {error.colno}"
            ) from error
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: Any) -> WorkPacket:
        data = _mapping(raw, "packet")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise PacketError("schema_version must be the integer 1")

        packet_id = _text(data.get("id"), "id")
        kind = _text(data.get("kind"), "kind")
        if kind not in PACKET_KINDS:
            raise PacketError(
                f"kind must be one of: {', '.join(sorted(PACKET_KINDS))}"
            )

        scope_data = _mapping(data.get("scope"), "scope")
        scope = Scope(
            include=_text_list(scope_data.get("include"), "scope.include"),
            exclude=_text_list(scope_data.get("exclude"), "scope.exclude"),
        )

        acceptance = _acceptance(data.get("acceptance"))
        decisions = _decisions(data.get("decisions"))
        authority = _authority(data.get("authority"))

        return cls(
            schema_version=schema_version,
            id=packet_id,
            kind=kind,
            intent=_text(data.get("intent"), "intent"),
            desired_outcome=_text(data.get("desired_outcome"), "desired_outcome"),
            scope=scope,
            acceptance=acceptance,
            decisions=decisions,
            authority=authority,
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PacketError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PacketError(f"{field} must be a non-empty string")
    return value.strip()


def _text_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PacketError(f"{field} must be a list")
    return tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(value))


def _acceptance(value: Any) -> tuple[AcceptanceCriterion, ...]:
    if not isinstance(value, list) or not value:
        raise PacketError("acceptance must be a non-empty list")

    criteria: list[AcceptanceCriterion] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        field = f"acceptance[{index}]"
        data = _mapping(item, field)
        criterion_id = _text(data.get("id"), f"{field}.id")
        if criterion_id in seen_ids:
            raise PacketError(f"duplicate acceptance criterion id: {criterion_id}")
        seen_ids.add(criterion_id)

        evidence = _text_list(
            data.get("evidence_required"), f"{field}.evidence_required"
        )
        if not evidence:
            raise PacketError(f"{field}.evidence_required must not be empty")
        unknown_evidence = set(evidence) - EVIDENCE_KINDS
        if unknown_evidence:
            raise PacketError(
                f"{field}.evidence_required contains unknown kinds: "
                f"{', '.join(sorted(unknown_evidence))}"
            )

        criteria.append(
            AcceptanceCriterion(
                id=criterion_id,
                statement=_text(data.get("statement"), f"{field}.statement"),
                evidence_required=evidence,
            )
        )
    return tuple(criteria)


def _decisions(value: Any) -> tuple[Decision, ...]:
    if not isinstance(value, list):
        raise PacketError("decisions must be a list")

    decisions: list[Decision] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        field = f"decisions[{index}]"
        data = _mapping(item, field)
        decision_id = _text(data.get("id"), f"{field}.id")
        if decision_id in seen_ids:
            raise PacketError(f"duplicate decision id: {decision_id}")
        seen_ids.add(decision_id)

        state = _text(data.get("state"), f"{field}.state")
        if state not in {"resolved", "unresolved"}:
            raise PacketError(f"{field}.state must be resolved or unresolved")

        decision = data.get("decision")
        decided_by = data.get("decided_by")
        if state == "resolved":
            decision = _text(decision, f"{field}.decision")
            decided_by = _text(decided_by, f"{field}.decided_by")
        elif decision is not None or decided_by is not None:
            raise PacketError(
                f"{field} is unresolved and must not claim a decision or decision maker"
            )

        decisions.append(
            Decision(
                id=decision_id,
                question=_text(data.get("question"), f"{field}.question"),
                state=state,
                decision=decision,
                decided_by=decided_by,
            )
        )
    return tuple(decisions)


def _authority(value: Any) -> AuthorityEnvelope:
    data = _mapping(value, "authority")
    mode = _text(data.get("mode"), "authority.mode")
    if mode not in AUTHORITY_PRESETS:
        raise PacketError(
            f"authority.mode must be one of: {', '.join(AUTHORITY_PRESETS)}"
        )

    allow = frozenset(_text_list(data.get("allow"), "authority.allow"))
    deny = frozenset(_text_list(data.get("deny"), "authority.deny"))
    unknown_actions = (allow | deny) - AUTHORITY_ACTIONS
    if unknown_actions:
        raise PacketError(
            "authority contains unknown actions: "
            f"{', '.join(sorted(unknown_actions))}"
        )

    return AuthorityEnvelope(
        mode=mode,
        granted_by=_text(data.get("granted_by"), "authority.granted_by"),
        allow=allow,
        deny=deny,
    )
