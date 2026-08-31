from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .controller_claims import (
    ControllerClaimHistory,
    controller_claim_path,
    default_controller_claim_root,
    read_controller_claim_with_current_bytes,
    require_claim_for_publication,
    require_claim_for_run,
)
from .json_records import DuplicateJsonMemberError, parse_json
from .preflight import (
    EXECUTION_PREFLIGHT_EVALUATOR,
    ExecutionEnvironment,
    PreflightReport,
    evaluate_preflight,
)
from .run_coordination import (
    RunPublication,
    activation_attempt_path,
    default_run_coordination_root,
    publish_immutable_json,
    read_stable_file,
    replace_canonical_run,
    require_canonical_run,
    run_publication_path,
    run_transaction,
    worker_ready_path,
)
from .runs import ActivationBinding, RunRecord, RunTransition, serialize_run


_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class ActivationError(ValueError):
    """Activation state is unreadable or violates the protocol."""


class ActivationConflictError(ActivationError):
    """Current run, claim, preflight, or worker state blocks activation."""


@dataclass(frozen=True)
class ActivationAttempt:
    schema_version: int
    id: str
    created_at: str
    recorded_by: str
    run_id: str
    publication_id: str
    publication_digest: str
    run_record_digest: str
    expected_state: str
    expected_transition_sequence: int
    claim_id: str
    claim_sequence: int
    controller_id: str
    claim_event_digest: str
    packet_id: str
    packet_digest: str
    environment_digest: str
    environment: ExecutionEnvironment
    preflight_evaluator: str
    preflight_required_controller_state: str
    preflight_evaluated_at: str
    preflight_max_age_seconds: int
    preflight_ready: bool
    preflight_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_attempt(self)

    @classmethod
    def from_path(cls, path: str | Path) -> ActivationAttempt:
        raw, _ = _read_json_record(Path(path), "activation attempt")
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> ActivationAttempt:
        data = _mapping(raw, "activation attempt")
        _require_fields(
            data,
            {
                "schema_version",
                "id",
                "created_at",
                "recorded_by",
                "run",
                "claim",
                "packet",
                "environment",
                "preflight",
            },
            "activation attempt",
        )
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ActivationError("activation attempt.schema_version must be the integer 1")

        run = _mapping(data.get("run"), "activation attempt.run")
        _require_fields(
            run,
            {
                "id",
                "publication_id",
                "publication_digest",
                "record_digest",
                "expected_state",
                "expected_transition_sequence",
            },
            "activation attempt.run",
        )
        expected_sequence = run.get("expected_transition_sequence")
        if type(expected_sequence) is not int or expected_sequence < 1:
            raise ActivationError(
                "activation attempt.run.expected_transition_sequence must be a positive integer"
            )

        claim = _mapping(data.get("claim"), "activation attempt.claim")
        _require_fields(
            claim,
            {"id", "sequence", "controller_id", "event_digest"},
            "activation attempt.claim",
        )
        claim_sequence = claim.get("sequence")
        if type(claim_sequence) is not int or claim_sequence < 0:
            raise ActivationError(
                "activation attempt.claim.sequence must be a non-negative integer"
            )

        packet = _mapping(data.get("packet"), "activation attempt.packet")
        _require_fields(packet, {"id", "digest"}, "activation attempt.packet")

        environment = _mapping(
            data.get("environment"), "activation attempt.environment"
        )
        _require_fields(
            environment,
            {"source_digest", "snapshot"},
            "activation attempt.environment",
        )

        preflight = _mapping(data.get("preflight"), "activation attempt.preflight")
        _require_fields(
            preflight,
            {
                "evaluator",
                "required_controller_state",
                "evaluated_at",
                "max_age_seconds",
                "ready",
                "blockers",
            },
            "activation attempt.preflight",
        )
        max_age = preflight.get("max_age_seconds")
        if type(max_age) is not int or max_age < 1:
            raise ActivationError(
                "activation attempt.preflight.max_age_seconds must be an integer of at least 1"
            )
        ready = preflight.get("ready")
        if type(ready) is not bool:
            raise ActivationError("activation attempt.preflight.ready must be a boolean")
        blockers = _text_list(
            preflight.get("blockers"), "activation attempt.preflight.blockers"
        )

        attempt = cls(
            schema_version=schema_version,
            id=_text(data.get("id"), "activation attempt.id"),
            created_at=_timestamp(
                data.get("created_at"), "activation attempt.created_at"
            ),
            recorded_by=_text(
                data.get("recorded_by"), "activation attempt.recorded_by"
            ),
            run_id=_text(run.get("id"), "activation attempt.run.id"),
            publication_id=_text(
                run.get("publication_id"), "activation attempt.run.publication_id"
            ),
            publication_digest=_digest(
                run.get("publication_digest"),
                "activation attempt.run.publication_digest",
            ),
            run_record_digest=_digest(
                run.get("record_digest"), "activation attempt.run.record_digest"
            ),
            expected_state=_text(
                run.get("expected_state"), "activation attempt.run.expected_state"
            ),
            expected_transition_sequence=expected_sequence,
            claim_id=_text(claim.get("id"), "activation attempt.claim.id"),
            claim_sequence=claim_sequence,
            controller_id=_text(
                claim.get("controller_id"),
                "activation attempt.claim.controller_id",
            ),
            claim_event_digest=_digest(
                claim.get("event_digest"),
                "activation attempt.claim.event_digest",
            ),
            packet_id=_text(packet.get("id"), "activation attempt.packet.id"),
            packet_digest=_digest(
                packet.get("digest"), "activation attempt.packet.digest"
            ),
            environment_digest=_digest(
                environment.get("source_digest"),
                "activation attempt.environment.source_digest",
            ),
            environment=ExecutionEnvironment.from_mapping(environment.get("snapshot")),
            preflight_evaluator=_text(
                preflight.get("evaluator"), "activation attempt.preflight.evaluator"
            ),
            preflight_required_controller_state=_text(
                preflight.get("required_controller_state"),
                "activation attempt.preflight.required_controller_state",
            ),
            preflight_evaluated_at=_timestamp(
                preflight.get("evaluated_at"),
                "activation attempt.preflight.evaluated_at",
            ),
            preflight_max_age_seconds=max_age,
            preflight_ready=ready,
            preflight_blockers=blockers,
        )
        return attempt

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "recorded_by": self.recorded_by,
            "run": {
                "id": self.run_id,
                "publication_id": self.publication_id,
                "publication_digest": self.publication_digest,
                "record_digest": self.run_record_digest,
                "expected_state": self.expected_state,
                "expected_transition_sequence": self.expected_transition_sequence,
            },
            "claim": {
                "id": self.claim_id,
                "sequence": self.claim_sequence,
                "controller_id": self.controller_id,
                "event_digest": self.claim_event_digest,
            },
            "packet": {"id": self.packet_id, "digest": self.packet_digest},
            "environment": {
                "source_digest": self.environment_digest,
                "snapshot": self.environment.to_mapping(),
            },
            "preflight": {
                "evaluator": self.preflight_evaluator,
                "required_controller_state": self.preflight_required_controller_state,
                "evaluated_at": self.preflight_evaluated_at,
                "max_age_seconds": self.preflight_max_age_seconds,
                "ready": self.preflight_ready,
                "blockers": list(self.preflight_blockers),
            },
        }


@dataclass(frozen=True)
class WorkerReadyReceipt:
    schema_version: int
    id: str
    recorded_at: str
    recorded_by: str
    run_id: str
    publication_id: str
    attempt_id: str
    attempt_digest: str
    worker_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        _validate_worker_ready(self)

    @classmethod
    def from_path(cls, path: str | Path) -> WorkerReadyReceipt:
        raw, _ = _read_json_record(Path(path), "worker-ready receipt")
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> WorkerReadyReceipt:
        data = _mapping(raw, "worker-ready receipt")
        _require_fields(
            data,
            {
                "schema_version",
                "id",
                "recorded_at",
                "recorded_by",
                "run_id",
                "publication_id",
                "attempt_id",
                "attempt_digest",
                "worker_id",
                "workspace_id",
            },
            "worker-ready receipt",
        )
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ActivationError("worker-ready receipt.schema_version must be the integer 1")
        return cls(
            schema_version=schema_version,
            id=_text(data.get("id"), "worker-ready receipt.id"),
            recorded_at=_timestamp(
                data.get("recorded_at"), "worker-ready receipt.recorded_at"
            ),
            recorded_by=_text(
                data.get("recorded_by"), "worker-ready receipt.recorded_by"
            ),
            run_id=_text(data.get("run_id"), "worker-ready receipt.run_id"),
            publication_id=_text(
                data.get("publication_id"), "worker-ready receipt.publication_id"
            ),
            attempt_id=_text(
                data.get("attempt_id"), "worker-ready receipt.attempt_id"
            ),
            attempt_digest=_digest(
                data.get("attempt_digest"), "worker-ready receipt.attempt_digest"
            ),
            worker_id=_text(data.get("worker_id"), "worker-ready receipt.worker_id"),
            workspace_id=_text(
                data.get("workspace_id"), "worker-ready receipt.workspace_id"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "recorded_at": self.recorded_at,
            "recorded_by": self.recorded_by,
            "run_id": self.run_id,
            "publication_id": self.publication_id,
            "attempt_id": self.attempt_id,
            "attempt_digest": self.attempt_digest,
            "worker_id": self.worker_id,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True)
class ActivationPreparation:
    preflight: PreflightReport
    attempt: ActivationAttempt | None

    @property
    def prepared(self) -> bool:
        return self.attempt is not None


def prepare_activation_attempt(
    run_path: str | Path,
    packet_content: bytes,
    environment_content: bytes,
    *,
    expected_claim_id: str,
    attempt_id: str,
    recorded_by: str,
    now: datetime,
    max_age_seconds: int = 300,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> ActivationPreparation:
    supplied_run = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied_run.id, resolved_coordination_root):
        publication, publication_bytes = _load_publication(
            supplied_run.id, resolved_coordination_root
        )
        run, run_bytes = require_canonical_run(publication, run_path)
        claim, claim_event_bytes = _load_claim(
            run, publication, resolved_claim_root
        )
        _require_current_claim(claim, expected_claim_id)
        if claim.current.controller_id != _text(recorded_by, "recorded_by"):
            raise ActivationConflictError(
                f"activation recorder {recorded_by} is not current controller "
                f"{claim.current.controller_id}"
            )
        environment = _parse_environment(environment_content)
        if environment.controller.id != claim.current.controller_id:
            raise ActivationConflictError(
                f"environment controller is {environment.controller.id}, not current "
                f"controller {claim.current.controller_id}"
            )
        report = evaluate_preflight(
            run,
            packet_content,
            environment,
            now=now,
            max_age_seconds=max_age_seconds,
            required_controller_state="owned",
        )
        if not report.ready:
            return ActivationPreparation(preflight=report, attempt=None)

        timestamp = _format_utc(now)
        attempt = ActivationAttempt(
            schema_version=1,
            id=_text(attempt_id, "attempt_id"),
            created_at=timestamp,
            recorded_by=claim.current.controller_id,
            run_id=run.id,
            publication_id=publication.id,
            publication_digest=_content_digest(publication_bytes),
            run_record_digest=_content_digest(run_bytes),
            expected_state="initialized",
            expected_transition_sequence=len(run.transitions),
            claim_id=claim.current.claim_id,
            claim_sequence=claim.current.sequence,
            controller_id=claim.current.controller_id,
            claim_event_digest=_content_digest(claim_event_bytes),
            packet_id=run.packet.id,
            packet_digest=run.packet.digest,
            environment_digest=_content_digest(environment_content),
            environment=environment,
            preflight_evaluator=EXECUTION_PREFLIGHT_EVALUATOR,
            preflight_required_controller_state=report.required_controller_state,
            preflight_evaluated_at=timestamp,
            preflight_max_age_seconds=max_age_seconds,
            preflight_ready=True,
            preflight_blockers=(),
        )
        publish_immutable_json(
            attempt.to_mapping(),
            activation_attempt_path(run.id, attempt.id, resolved_coordination_root),
        )
        return ActivationPreparation(preflight=report, attempt=attempt)


def record_worker_ready(
    run_path: str | Path,
    *,
    attempt_id: str,
    expected_claim_id: str,
    receipt_id: str,
    worker_id: str,
    workspace_id: str,
    recorded_by: str,
    now: datetime,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> WorkerReadyReceipt:
    supplied_run = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied_run.id, resolved_coordination_root):
        publication, publication_bytes = _load_publication(
            supplied_run.id, resolved_coordination_root
        )
        run, run_bytes = require_canonical_run(publication, run_path)
        claim, claim_event_bytes = _load_claim(
            run, publication, resolved_claim_root
        )
        _require_current_claim(claim, expected_claim_id)
        recorder = _text(recorded_by, "recorded_by")
        if recorder != claim.current.controller_id:
            raise ActivationConflictError(
                f"worker-ready recorder {recorder} is not current controller "
                f"{claim.current.controller_id}"
            )
        attempt, attempt_bytes = _load_attempt(
            run.id, attempt_id, resolved_coordination_root
        )
        _require_attempt_context(
            attempt,
            publication,
            publication_bytes,
            run,
            run_bytes,
            claim,
            claim_event_bytes,
        )
        if workspace_id != attempt.environment.workspace.id:
            raise ActivationConflictError(
                f"prepared workspace is {workspace_id}, not attempt workspace "
                f"{attempt.environment.workspace.id}"
            )
        recorded_at = _format_utc(now)
        if recorded_at < attempt.created_at:
            raise ActivationError("worker-ready receipt cannot predate its attempt")
        receipt = WorkerReadyReceipt(
            schema_version=1,
            id=_text(receipt_id, "receipt_id"),
            recorded_at=recorded_at,
            recorded_by=recorder,
            run_id=run.id,
            publication_id=publication.id,
            attempt_id=attempt.id,
            attempt_digest=_content_digest(attempt_bytes),
            worker_id=_text(worker_id, "worker_id"),
            workspace_id=_text(workspace_id, "workspace_id"),
        )
        publish_immutable_json(
            receipt.to_mapping(),
            worker_ready_path(run.id, attempt.id, resolved_coordination_root),
        )
        return receipt


def commit_activation(
    run_path: str | Path,
    packet_content: bytes,
    *,
    attempt_id: str,
    expected_claim_id: str,
    now: datetime,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> RunRecord:
    supplied_run = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied_run.id, resolved_coordination_root):
        publication, publication_bytes = _load_publication(
            supplied_run.id, resolved_coordination_root
        )
        run, run_bytes = require_canonical_run(publication, run_path)
        attempt, attempt_bytes = _load_attempt(
            run.id, attempt_id, resolved_coordination_root
        )
        ready, ready_bytes = _load_worker_ready(
            run.id, attempt.id, resolved_coordination_root
        )

        if run.current_state == "active":
            _require_completed_attempt_context(
                attempt,
                publication,
                publication_bytes,
                run,
                packet_content,
                expected_claim_id,
            )
            _require_ready_context(ready, attempt, attempt_bytes)
            expected_binding = _activation_binding(attempt, attempt_bytes, ready, ready_bytes)
            activation = run.transitions[-1].activation
            if activation != expected_binding:
                raise ActivationConflictError(
                    f"run {run.id} is already active with a different activation binding"
                )
            return replace_canonical_run(
                publication, _content_digest(run_bytes), run_bytes
            )

        claim, claim_event_bytes = _load_claim(
            run, publication, resolved_claim_root
        )
        _require_current_claim(claim, expected_claim_id)
        _require_attempt_context(
            attempt,
            publication,
            publication_bytes,
            run,
            run_bytes,
            claim,
            claim_event_bytes,
        )
        _require_ready_context(ready, attempt, attempt_bytes)
        packet_digest = _content_digest(packet_content)
        if packet_digest != attempt.packet_digest:
            raise ActivationConflictError(
                f"current packet digest is {packet_digest}, not attempt packet "
                f"{attempt.packet_digest}"
            )
        report = evaluate_preflight(
            run,
            packet_content,
            attempt.environment,
            now=now,
            max_age_seconds=attempt.preflight_max_age_seconds,
            required_controller_state="owned",
        )
        if not report.ready:
            messages = "; ".join(blocker.message for blocker in report.blockers)
            raise ActivationConflictError(
                f"activation preflight no longer passes: {messages}"
            )

        transition_at = _format_utc(now)
        if transition_at < ready.recorded_at:
            raise ActivationError("activation transition cannot predate worker readiness")
        binding = _activation_binding(attempt, attempt_bytes, ready, ready_bytes)
        updated = RunRecord(
            schema_version=2,
            id=run.id,
            packet=run.packet,
            initiated_by=run.initiated_by,
            readiness=run.readiness,
            transitions=run.transitions
            + (
                RunTransition(
                    sequence=len(run.transitions),
                    at=transition_at,
                    from_state="initialized",
                    to_state="active",
                    reason="worker-handoff-committed",
                    recorded_by=attempt.controller_id,
                    activation=binding,
                ),
            ),
        )
        new_content = serialize_run(updated)
        return replace_canonical_run(
            publication, attempt.run_record_digest, new_content
        )


def inspect_activation(
    run_path: str | Path,
    attempt_id: str,
    *,
    coordination_root: str | Path | None = None,
) -> tuple[RunRecord, ActivationAttempt, WorkerReadyReceipt | None]:
    supplied_run = RunRecord.from_path(run_path)
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied_run.id, resolved_coordination_root):
        publication, _ = _load_publication(
            supplied_run.id, resolved_coordination_root
        )
        run, _ = require_canonical_run(publication, run_path)
        attempt, attempt_bytes = _load_attempt(
            run.id, attempt_id, resolved_coordination_root
        )
        conflicts: list[str] = []
        if attempt.run_id != run.id:
            conflicts.append(f"attempt run is {attempt.run_id}, not {run.id}")
        if attempt.publication_id != publication.id:
            conflicts.append(
                f"attempt publication is {attempt.publication_id}, not {publication.id}"
            )
        if conflicts:
            raise ActivationConflictError("; ".join(conflicts))

        ready_path = worker_ready_path(
            run.id, attempt.id, resolved_coordination_root
        )
        try:
            os.lstat(ready_path)
        except FileNotFoundError:
            ready = None
        except OSError as error:
            raise ActivationError(
                f"cannot inspect worker-ready receipt {ready_path}: {error}"
            ) from error
        else:
            ready, _ = _load_worker_ready(
                run.id, attempt.id, resolved_coordination_root
            )
            _require_ready_context(ready, attempt, attempt_bytes)
        return run, attempt, ready


def activation_record_digest(path: str | Path) -> str:
    _, content = _read_json_record(Path(path), "activation record")
    return _content_digest(content)


def _load_publication(
    run_id: str, coordination_root: Path
) -> tuple[RunPublication, bytes]:
    path = run_publication_path(run_id, coordination_root)
    raw, content = _read_json_record(path, "run publication")
    return RunPublication.from_mapping(raw), content


def _load_claim(
    run: RunRecord, publication: RunPublication, claim_root: Path
) -> tuple[ControllerClaimHistory, bytes]:
    path = controller_claim_path(run.id, claim_root)
    loaded, event_content = read_controller_claim_with_current_bytes(path)
    history = require_claim_for_publication(
        require_claim_for_run(run, loaded),
        publication.id,
    )
    return history, event_content


def _load_attempt(
    run_id: str, attempt_id: str, coordination_root: Path
) -> tuple[ActivationAttempt, bytes]:
    path = activation_attempt_path(run_id, attempt_id, coordination_root)
    raw, content = _read_json_record(path, "activation attempt")
    return ActivationAttempt.from_mapping(raw), content


def _load_worker_ready(
    run_id: str, attempt_id: str, coordination_root: Path
) -> tuple[WorkerReadyReceipt, bytes]:
    path = worker_ready_path(run_id, attempt_id, coordination_root)
    raw, content = _read_json_record(path, "worker-ready receipt")
    return WorkerReadyReceipt.from_mapping(raw), content


def _require_current_claim(
    history: ControllerClaimHistory, expected_claim_id: str
) -> None:
    expected = _text(expected_claim_id, "expected_claim_id")
    if history.current.claim_id != expected:
        raise ActivationConflictError(
            f"current claim is {history.current.claim_id}, not expected claim {expected}"
        )


def _require_attempt_context(
    attempt: ActivationAttempt,
    publication: RunPublication,
    publication_bytes: bytes,
    run: RunRecord,
    run_bytes: bytes,
    claim: ControllerClaimHistory,
    claim_event_bytes: bytes,
) -> None:
    conflicts: list[str] = []
    if attempt.run_id != run.id:
        conflicts.append(f"attempt run is {attempt.run_id}, not {run.id}")
    if attempt.publication_id != publication.id:
        conflicts.append(
            f"attempt publication is {attempt.publication_id}, not {publication.id}"
        )
    if attempt.publication_digest != _content_digest(publication_bytes):
        conflicts.append("run publication bytes changed after the attempt")
    if attempt.run_record_digest != _content_digest(run_bytes):
        conflicts.append("canonical run bytes changed after the attempt")
    if run.current_state != attempt.expected_state:
        conflicts.append(
            f"run state is {run.current_state}, not {attempt.expected_state}"
        )
    if len(run.transitions) != attempt.expected_transition_sequence:
        conflicts.append(
            f"next run transition is {len(run.transitions)}, not "
            f"{attempt.expected_transition_sequence}"
        )
    if attempt.claim_id != claim.current.claim_id:
        conflicts.append(
            f"attempt claim is {attempt.claim_id}, not {claim.current.claim_id}"
        )
    if attempt.claim_sequence != claim.current.sequence:
        conflicts.append(
            f"attempt claim sequence is {attempt.claim_sequence}, not "
            f"{claim.current.sequence}"
        )
    if attempt.controller_id != claim.current.controller_id:
        conflicts.append(
            f"attempt controller is {attempt.controller_id}, not "
            f"{claim.current.controller_id}"
        )
    if attempt.claim_event_digest != _content_digest(claim_event_bytes):
        conflicts.append("current claim event bytes do not match the attempt")
    if attempt.packet_id != run.packet.id or attempt.packet_digest != run.packet.digest:
        conflicts.append("attempt packet binding does not match the run")
    if attempt.environment.run_id != run.id:
        conflicts.append("attempt environment names another run")
    if attempt.environment.controller.id != attempt.controller_id:
        conflicts.append("attempt environment names another controller")
    if conflicts:
        raise ActivationConflictError("; ".join(conflicts))


def _require_completed_attempt_context(
    attempt: ActivationAttempt,
    publication: RunPublication,
    publication_bytes: bytes,
    run: RunRecord,
    packet_content: bytes,
    expected_claim_id: str,
) -> None:
    conflicts: list[str] = []
    expected = _text(expected_claim_id, "expected_claim_id")
    if attempt.claim_id != expected:
        conflicts.append(
            f"activation claim is {attempt.claim_id}, not expected claim {expected}"
        )
    if attempt.run_id != run.id:
        conflicts.append(f"attempt run is {attempt.run_id}, not {run.id}")
    if attempt.publication_id != publication.id:
        conflicts.append(
            f"attempt publication is {attempt.publication_id}, not {publication.id}"
        )
    if attempt.publication_digest != _content_digest(publication_bytes):
        conflicts.append("run publication bytes do not match the activation attempt")
    packet_digest = _content_digest(packet_content)
    if packet_digest != attempt.packet_digest:
        conflicts.append(
            f"current packet digest is {packet_digest}, not attempt packet "
            f"{attempt.packet_digest}"
        )
    if attempt.packet_id != run.packet.id or attempt.packet_digest != run.packet.digest:
        conflicts.append("attempt packet binding does not match the active run")
    if conflicts:
        raise ActivationConflictError("; ".join(conflicts))


def _require_ready_context(
    ready: WorkerReadyReceipt,
    attempt: ActivationAttempt,
    attempt_bytes: bytes,
) -> None:
    conflicts: list[str] = []
    if ready.run_id != attempt.run_id:
        conflicts.append("worker-ready run does not match the attempt")
    if ready.publication_id != attempt.publication_id:
        conflicts.append("worker-ready publication does not match the attempt")
    if ready.attempt_id != attempt.id:
        conflicts.append("worker-ready attempt ID does not match")
    if ready.attempt_digest != _content_digest(attempt_bytes):
        conflicts.append("worker-ready attempt digest does not match")
    if ready.workspace_id != attempt.environment.workspace.id:
        conflicts.append("worker-ready workspace does not match the attempt")
    if ready.recorded_by != attempt.controller_id:
        conflicts.append("worker-ready recorder does not match the attempt controller")
    if ready.recorded_at < attempt.created_at:
        conflicts.append("worker-ready receipt predates the activation attempt")
    if conflicts:
        raise ActivationConflictError("; ".join(conflicts))


def _activation_binding(
    attempt: ActivationAttempt,
    attempt_bytes: bytes,
    ready: WorkerReadyReceipt,
    ready_bytes: bytes,
) -> ActivationBinding:
    return ActivationBinding(
        claim_id=attempt.claim_id,
        attempt_id=attempt.id,
        attempt_digest=_content_digest(attempt_bytes),
        worker_id=ready.worker_id,
        worker_ready_digest=_content_digest(ready_bytes),
    )


def _validate_attempt(attempt: ActivationAttempt) -> None:
    if type(attempt.schema_version) is not int or attempt.schema_version != 1:
        raise ActivationError("activation attempt.schema_version must be the integer 1")
    _text(attempt.id, "activation attempt.id")
    _timestamp(attempt.created_at, "activation attempt.created_at")
    _text(attempt.recorded_by, "activation attempt.recorded_by")
    _text(attempt.run_id, "activation attempt.run_id")
    _text(attempt.publication_id, "activation attempt.publication_id")
    _digest(attempt.publication_digest, "activation attempt.publication_digest")
    _digest(attempt.run_record_digest, "activation attempt.run_record_digest")
    if attempt.expected_state != "initialized":
        raise ActivationError("activation attempt.expected_state must be initialized")
    if (
        type(attempt.expected_transition_sequence) is not int
        or attempt.expected_transition_sequence < 1
    ):
        raise ActivationError(
            "activation attempt.expected_transition_sequence must be a positive integer"
        )
    _text(attempt.claim_id, "activation attempt.claim_id")
    if type(attempt.claim_sequence) is not int or attempt.claim_sequence < 0:
        raise ActivationError(
            "activation attempt.claim_sequence must be a non-negative integer"
        )
    _text(attempt.controller_id, "activation attempt.controller_id")
    _digest(attempt.claim_event_digest, "activation attempt.claim_event_digest")
    _text(attempt.packet_id, "activation attempt.packet_id")
    _digest(attempt.packet_digest, "activation attempt.packet_digest")
    _digest(attempt.environment_digest, "activation attempt.environment_digest")
    if not isinstance(attempt.environment, ExecutionEnvironment):
        raise ActivationError("activation attempt.environment must be an ExecutionEnvironment")
    ExecutionEnvironment.from_mapping(attempt.environment.to_mapping())
    if attempt.preflight_evaluator != EXECUTION_PREFLIGHT_EVALUATOR:
        raise ActivationError(
            f"activation attempt.preflight_evaluator must be {EXECUTION_PREFLIGHT_EVALUATOR}"
        )
    if attempt.preflight_required_controller_state != "owned":
        raise ActivationError(
            "activation attempt.preflight_required_controller_state must be owned"
        )
    _timestamp(
        attempt.preflight_evaluated_at,
        "activation attempt.preflight_evaluated_at",
    )
    if attempt.preflight_evaluated_at != attempt.created_at:
        raise ActivationError(
            "activation attempt preflight time must equal its creation time"
        )
    if (
        type(attempt.preflight_max_age_seconds) is not int
        or attempt.preflight_max_age_seconds < 1
    ):
        raise ActivationError(
            "activation attempt.preflight_max_age_seconds must be at least 1"
        )
    if type(attempt.preflight_ready) is not bool or not attempt.preflight_ready:
        raise ActivationError("activation attempt must record a passing preflight")
    if not isinstance(attempt.preflight_blockers, tuple) or attempt.preflight_blockers:
        raise ActivationError("a passing activation attempt must have no blockers")
    if attempt.recorded_by != attempt.controller_id:
        raise ActivationError(
            "activation attempt recorded_by must equal its controller_id"
        )


def _validate_worker_ready(receipt: WorkerReadyReceipt) -> None:
    if type(receipt.schema_version) is not int or receipt.schema_version != 1:
        raise ActivationError("worker-ready receipt.schema_version must be the integer 1")
    _text(receipt.id, "worker-ready receipt.id")
    _timestamp(receipt.recorded_at, "worker-ready receipt.recorded_at")
    _text(receipt.recorded_by, "worker-ready receipt.recorded_by")
    _text(receipt.run_id, "worker-ready receipt.run_id")
    _text(receipt.publication_id, "worker-ready receipt.publication_id")
    _text(receipt.attempt_id, "worker-ready receipt.attempt_id")
    _digest(receipt.attempt_digest, "worker-ready receipt.attempt_digest")
    _text(receipt.worker_id, "worker-ready receipt.worker_id")
    _text(receipt.workspace_id, "worker-ready receipt.workspace_id")


def _parse_environment(content: bytes) -> ExecutionEnvironment:
    if not isinstance(content, bytes):
        raise ActivationError("environment_content must be bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError(
            f"environment snapshot is not valid UTF-8: byte {error.start}"
        ) from error
    try:
        raw = parse_json(text)
    except json.JSONDecodeError as error:
        raise ActivationError(
            f"environment snapshot is not valid JSON: line {error.lineno}, "
            f"column {error.colno}"
        ) from error
    except DuplicateJsonMemberError as error:
        raise ActivationError(f"environment snapshot is ambiguous: {error}") from error
    try:
        return ExecutionEnvironment.from_mapping(raw)
    except ValueError as error:
        raise ActivationError(f"environment snapshot is malformed: {error}") from error


def _read_json_record(path: Path, label: str) -> tuple[object, bytes]:
    content, _ = read_stable_file(path, ActivationError, label)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ActivationError(
            f"{label} {path} is not valid UTF-8: byte {error.start}"
        ) from error
    try:
        return parse_json(text), content
    except json.JSONDecodeError as error:
        raise ActivationError(
            f"{label} {path} is not valid JSON: line {error.lineno}, "
            f"column {error.colno}"
        ) from error
    except DuplicateJsonMemberError as error:
        raise ActivationError(f"{label} {path} is ambiguous: {error}") from error


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ActivationError(f"{field} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise ActivationError(f"{field} keys must be strings")
    return cast(dict[str, object], value)


def _require_fields(data: dict[str, object], expected: set[str], field: str) -> None:
    missing = expected - set(data)
    if missing:
        raise ActivationError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    unknown = set(data) - expected
    if unknown:
        raise ActivationError(
            f"{field} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActivationError(f"{field} must be a non-empty string")
    return value.strip()


def _text_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ActivationError(f"{field} must be a list")
    return tuple(
        _text(item, f"{field}[{index}]")
        for index, item in enumerate(cast(list[object], value))
    )


def _digest(value: object, field: str) -> str:
    text = _text(value, field)
    if not _DIGEST_PATTERN.fullmatch(text):
        raise ActivationError(
            f"{field} must be sha256 followed by 64 lowercase hexadecimal characters"
        )
    return text


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    if not _TIMESTAMP_PATTERN.fullmatch(text):
        raise ActivationError(
            f"{field} must be UTC ISO-8601 with seconds, such as "
            f"2026-08-30T12:00:00Z"
        )
    try:
        _ = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ActivationError(f"{field} is not a valid timestamp") from error
    return text


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ActivationError("now must include a UTC offset")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
