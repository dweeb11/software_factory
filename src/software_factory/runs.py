from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .json_records import DuplicateJsonMemberError, parse_json
from .readiness import ReadinessReport, evaluate_readiness
from .work_packets import WorkPacket


READINESS_EVALUATOR = "packet-readiness-v1"
RUN_STATES = frozenset(
    {
        "initialized",
        "active",
        "waiting",
        "complete",
        "blocked",
        "cancelled",
        "exhausted",
        "failed",
    }
)
TERMINAL_RUN_STATES = frozenset(
    {"complete", "blocked", "cancelled", "exhausted", "failed"}
)
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "initialized": frozenset({"active", "blocked", "cancelled"}),
    "active": frozenset(
        {"waiting", "complete", "blocked", "cancelled", "exhausted", "failed"}
    ),
    "waiting": frozenset(
        {"active", "blocked", "cancelled", "exhausted", "failed"}
    ),
}
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class RunError(ValueError):
    """A run record is unreadable or violates the protocol."""


class RunPersistenceError(OSError):
    """A run record was not published."""


class RunPublicationError(OSError):
    """A run record was published, but the outcome requires inspection."""


@dataclass(frozen=True)
class PacketBinding:
    id: str
    digest: str


@dataclass(frozen=True)
class ReadinessObservation:
    evaluator: str
    evaluated_at: str
    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RunTransition:
    sequence: int
    at: str
    from_state: str | None
    to_state: str
    reason: str
    recorded_by: str


@dataclass(frozen=True)
class RunRecord:
    schema_version: int
    id: str
    packet: PacketBinding
    initiated_by: str
    readiness: ReadinessObservation
    transitions: tuple[RunTransition, ...]

    @property
    def current_state(self) -> str:
        return self.transitions[-1].to_state

    @property
    def terminal(self) -> bool:
        return self.current_state in TERMINAL_RUN_STATES

    @classmethod
    def from_path(cls, path: str | Path) -> RunRecord:
        run_path = Path(path)
        try:
            raw_bytes = run_path.read_bytes()
        except OSError as error:
            raise RunError(f"cannot read run {run_path}: {error}") from error

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunError(
                f"run {run_path} is not valid UTF-8: byte {error.start}"
            ) from error

        try:
            raw = parse_json(text)
        except json.JSONDecodeError as error:
            raise RunError(
                f"run {run_path} is not valid JSON: line {error.lineno}, column {error.colno}"
            ) from error
        except DuplicateJsonMemberError as error:
            raise RunError(f"run {run_path} is ambiguous: {error}") from error
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> RunRecord:
        data = _mapping(raw, "run")
        _require_fields(
            data,
            {"schema_version", "id", "packet", "initiated_by", "readiness", "transitions"},
            "run",
        )

        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise RunError("schema_version must be the integer 1")

        packet_data = _mapping(data.get("packet"), "packet")
        _require_fields(packet_data, {"id", "digest"}, "packet")
        digest = _text(packet_data.get("digest"), "packet.digest")
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise RunError("packet.digest must be sha256 followed by 64 lowercase hexadecimal characters")
        packet = PacketBinding(
            id=_text(packet_data.get("id"), "packet.id"),
            digest=digest,
        )

        readiness_data = _mapping(data.get("readiness"), "readiness")
        _require_fields(
            readiness_data,
            {"evaluator", "evaluated_at", "ready", "blockers"},
            "readiness",
        )
        evaluator = _text(readiness_data.get("evaluator"), "readiness.evaluator")
        if evaluator != READINESS_EVALUATOR:
            raise RunError(f"readiness.evaluator must be {READINESS_EVALUATOR}")
        ready = readiness_data.get("ready")
        if type(ready) is not bool:
            raise RunError("readiness.ready must be a boolean")
        blockers = _text_list(readiness_data.get("blockers"), "readiness.blockers")
        if not ready or blockers:
            raise RunError("an initialized run must record a ready verdict with no blockers")
        readiness = ReadinessObservation(
            evaluator=evaluator,
            evaluated_at=_timestamp(
                readiness_data.get("evaluated_at"), "readiness.evaluated_at"
            ),
            ready=ready,
            blockers=blockers,
        )

        initiated_by = _text(data.get("initiated_by"), "initiated_by")
        transitions = _transitions(data.get("transitions"))
        first = transitions[0]
        if first.recorded_by != initiated_by:
            raise RunError("the initial transition must be recorded by initiated_by")
        if first.at < readiness.evaluated_at:
            raise RunError("the initial transition cannot predate the readiness observation")

        return cls(
            schema_version=schema_version,
            id=_text(data.get("id"), "id"),
            packet=packet,
            initiated_by=initiated_by,
            readiness=readiness,
            transitions=transitions,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "packet": {"id": self.packet.id, "digest": self.packet.digest},
            "initiated_by": self.initiated_by,
            "readiness": {
                "evaluator": self.readiness.evaluator,
                "evaluated_at": self.readiness.evaluated_at,
                "ready": self.readiness.ready,
                "blockers": list(self.readiness.blockers),
            },
            "transitions": [
                {
                    "sequence": transition.sequence,
                    "at": transition.at,
                    "from": transition.from_state,
                    "to": transition.to_state,
                    "reason": transition.reason,
                    "recorded_by": transition.recorded_by,
                }
                for transition in self.transitions
            ],
        }


@dataclass(frozen=True)
class RunInitialization:
    readiness: ReadinessReport
    record: RunRecord | None

    @property
    def initialized(self) -> bool:
        return self.record is not None


def initialize_run(
    packet_content: bytes,
    *,
    run_id: str,
    initiated_by: str,
    now: datetime,
    source: str = "packet",
) -> RunInitialization:
    packet = WorkPacket.from_bytes(packet_content, source=source)
    readiness = evaluate_readiness(packet)
    if not readiness.ready:
        return RunInitialization(readiness=readiness, record=None)

    timestamp = _format_utc(now)
    actor = _text(initiated_by, "initiated_by")
    record = RunRecord(
        schema_version=1,
        id=_text(run_id, "run_id"),
        packet=PacketBinding(
            id=packet.id,
            digest=f"sha256:{hashlib.sha256(packet_content).hexdigest()}",
        ),
        initiated_by=actor,
        readiness=ReadinessObservation(
            evaluator=READINESS_EVALUATOR,
            evaluated_at=timestamp,
            ready=True,
            blockers=(),
        ),
        transitions=(
            RunTransition(
                sequence=0,
                at=timestamp,
                from_state=None,
                to_state="initialized",
                reason="packet-ready",
                recorded_by=actor,
            ),
        ),
    )
    return RunInitialization(readiness=readiness, record=record)


def persist_run(record: RunRecord, path: str | Path) -> None:
    run_path = Path(path)
    parent = run_path.parent
    if not parent.is_dir():
        raise RunPersistenceError(f"run directory does not exist: {parent}")

    content = (json.dumps(record.to_mapping(), indent=2) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{run_path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            _ = temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        raise RunPersistenceError(
            _prepublication_failure(run_path, error, temporary_path, cleanup_error)
        ) from error

    try:
        os.link(temporary_path, run_path)
    except FileExistsError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"run record already exists: {run_path}"
        if cleanup_error is not None:
            message += f"; temporary record {temporary_path} could not be removed: {cleanup_error}"
        raise RunPersistenceError(message) from error
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        raise RunPersistenceError(
            _prepublication_failure(run_path, error, temporary_path, cleanup_error)
        ) from error

    cleanup_error = _remove_temporary(temporary_path)
    if cleanup_error is not None:
        try:
            _fsync_directory(parent)
        except OSError as sync_error:
            message = f"a complete run record currently exists at {run_path}, but temporary record {temporary_path} could not be removed ({cleanup_error}) and durable publication could not be confirmed ({sync_error}); inspect both paths before retrying"
            raise RunPublicationError(message) from sync_error
        message = f"a durable run record exists at {run_path}, but temporary record {temporary_path} could not be removed: {cleanup_error}; inspect both paths before retrying"
        raise RunPublicationError(message) from cleanup_error

    try:
        _fsync_directory(parent)
    except OSError as error:
        message = f"a complete run record currently exists at {run_path}, but durable publication could not be confirmed: {error}; inspect this path before retrying"
        raise RunPublicationError(message) from error


def _remove_temporary(path: Path | None) -> OSError | None:
    if path is None:
        return None
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as error:
        return error
    return None


def _prepublication_failure(
    run_path: Path,
    error: OSError,
    temporary_path: Path | None,
    cleanup_error: OSError | None,
) -> str:
    message = f"cannot persist run {run_path}: {error}"
    if cleanup_error is not None and temporary_path is not None:
        message += f"; temporary record {temporary_path} could not be removed: {cleanup_error}"
    return message


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transitions(value: object) -> tuple[RunTransition, ...]:
    if not isinstance(value, list) or not value:
        raise RunError("transitions must be a non-empty list")

    transitions: list[RunTransition] = []
    previous: RunTransition | None = None
    for index, item in enumerate(cast(list[object], value)):
        field = f"transitions[{index}]"
        data = _mapping(item, field)
        _require_fields(
            data,
            {"sequence", "at", "from", "to", "reason", "recorded_by"},
            field,
        )
        sequence = data.get("sequence")
        if type(sequence) is not int or sequence != index:
            raise RunError(f"{field}.sequence must be the integer {index}")

        from_value = data.get("from")
        if from_value is not None and not isinstance(from_value, str):
            raise RunError(f"{field}.from must be a run state or null")
        from_state = from_value
        if from_state is not None and from_state not in RUN_STATES:
            raise RunError(f"{field}.from is not a known run state: {from_state}")

        to_state = _text(data.get("to"), f"{field}.to")
        if to_state not in RUN_STATES:
            raise RunError(f"{field}.to is not a known run state: {to_state}")

        transition = RunTransition(
            sequence=sequence,
            at=_timestamp(data.get("at"), f"{field}.at"),
            from_state=from_state,
            to_state=to_state,
            reason=_text(data.get("reason"), f"{field}.reason"),
            recorded_by=_text(data.get("recorded_by"), f"{field}.recorded_by"),
        )

        if previous is None:
            if transition.from_state is not None or transition.to_state != "initialized":
                raise RunError("the initial transition must be null -> initialized")
        else:
            if previous.to_state in TERMINAL_RUN_STATES:
                raise RunError(f"no transition may follow terminal state {previous.to_state}")
            if transition.from_state != previous.to_state:
                raise RunError(
                    f"{field}.from must match prior state {previous.to_state}"
                )
            if transition.to_state not in RUN_TRANSITIONS[previous.to_state]:
                raise RunError(
                    f"transition {previous.to_state} -> {transition.to_state} is not allowed"
                )
            if transition.at < previous.at:
                raise RunError(f"{field}.at cannot predate the prior transition")

        transitions.append(transition)
        previous = transition

    return tuple(transitions)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RunError(f"{field} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise RunError(f"{field} keys must be strings")
    return cast(dict[str, object], value)


def _require_fields(data: dict[str, object], expected: set[str], field: str) -> None:
    missing = expected - set(data)
    if missing:
        raise RunError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    unknown = set(data) - expected
    if unknown:
        raise RunError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunError(f"{field} must be a non-empty string")
    return value.strip()


def _text_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RunError(f"{field} must be a list")
    return tuple(
        _text(item, f"{field}[{index}]")
        for index, item in enumerate(cast(list[object], value))
    )


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    if not _TIMESTAMP_PATTERN.fullmatch(text):
        raise RunError(f"{field} must be UTC ISO-8601 with seconds, such as 2026-08-30T12:00:00Z")
    try:
        _ = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RunError(f"{field} is not a valid timestamp") from error
    return text


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunError("now must include a UTC offset")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
