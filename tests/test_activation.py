from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from software_factory.activation import (
    ActivationAttempt,
    ActivationConflictError,
    ActivationError,
    WorkerReadyReceipt,
    commit_activation,
    prepare_activation_attempt,
    record_worker_ready,
)
from software_factory.cli import main
from software_factory.controller_claims import (
    acquire_controller_claim,
    change_controller_claim,
)
from software_factory.preflight import (
    EXECUTION_PREFLIGHT_EVALUATOR,
    ExecutionEnvironment,
)
from software_factory.run_coordination import (
    RunCoordinationConflictError,
    RunCoordinationError,
    RunCoordinationPublicationError,
    activation_attempt_path,
    register_run_publication,
    run_publication_path,
    worker_ready_path,
)
from software_factory.runs import RunRecord, initialize_run, persist_run


PACKET_EXAMPLE = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"
ENVIRONMENT_EXAMPLE = (
    PROJECT_ROOT / "examples" / "preflight-ready" / "environment.json"
)
RUN_ID = "RUN-ACTIVATION-1"
PUBLICATION_ID = "PUBLICATION-ACTIVATION-1"
CLAIM_ID = "CLAIM-ACTIVATION-1"
CONTROLLER_ID = "controller-1"
WORKSPACE_ID = "workspace-1"
INITIALIZED_AT = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
REGISTERED_AT = datetime(2026, 8, 30, 12, 1, 0, tzinfo=timezone.utc)
CLAIMED_AT = datetime(2026, 8, 30, 12, 2, 0, tzinfo=timezone.utc)
PREPARED_AT = datetime(2026, 8, 30, 12, 6, 0, tzinfo=timezone.utc)
READY_AT = datetime(2026, 8, 30, 12, 7, 0, tzinfo=timezone.utc)
RECOVERED_AT = datetime(2026, 8, 30, 12, 7, 30, tzinfo=timezone.utc)
COMMITTED_AT = datetime(2026, 8, 30, 12, 8, 0, tzinfo=timezone.utc)
STALE_COMMIT_AT = datetime(2026, 8, 30, 12, 10, 1, tzinfo=timezone.utc)


def digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def load_mapping(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} did not contain an object")
    return cast(dict[str, object], value)


def nested_mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data[key]
    if not isinstance(value, dict):
        raise AssertionError(f"{key} was not an object")
    return cast(dict[str, object], value)


def serialize_mapping(data: dict[str, object]) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


class ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.packet_content = PACKET_EXAMPLE.read_bytes()
        self.packet_path = self.root / "packet.json"
        _ = self.packet_path.write_bytes(self.packet_content)

        initialization = initialize_run(
            self.packet_content,
            run_id=RUN_ID,
            initiated_by="operator",
            now=INITIALIZED_AT,
            source=str(PACKET_EXAMPLE),
        )
        if initialization.record is None:
            self.fail("canonical example packet did not initialize a run")
        self.initialized = initialization.record
        self.run_path = self.root / "run.json"
        persist_run(self.initialized, self.run_path)

        self.coordination_root = self.root / "coordination"
        _ = register_run_publication(
            RUN_ID,
            self.run_path,
            publication_id=PUBLICATION_ID,
            recorded_by="operator",
            now=REGISTERED_AT,
            coordination_root=self.coordination_root,
        )
        self.claim_root = self.root / "claims"
        _, self.claim_path = acquire_controller_claim(
            self.run_path,
            publication_id=PUBLICATION_ID,
            claim_id=CLAIM_ID,
            controller_id=CONTROLLER_ID,
            recorded_by="operator",
            now=CLAIMED_AT,
            claim_root=self.claim_root,
            coordination_root=self.coordination_root,
        )

        environment_data = load_mapping(ENVIRONMENT_EXAMPLE)
        environment_data["schema_version"] = 2
        environment_data["run_id"] = RUN_ID
        environment_data["generated_at"] = "2026-08-30T12:05:00Z"
        nested_mapping(environment_data, "controller")["id"] = CONTROLLER_ID
        nested_mapping(environment_data, "controller")["state"] = "owned"
        self.environment_data = environment_data
        self.environment_content = serialize_mapping(environment_data)
        self.environment = ExecutionEnvironment.from_mapping(environment_data)
        self.environment_path = self.root / "environment.json"
        _ = self.environment_path.write_bytes(self.environment_content)

    def attempt_path(self, attempt_id: str) -> Path:
        return activation_attempt_path(RUN_ID, attempt_id, self.coordination_root)

    def ready_path(self, attempt_id: str) -> Path:
        return worker_ready_path(RUN_ID, attempt_id, self.coordination_root)

    def prepare(
        self,
        attempt_id: str = "ACTIVATION-1",
        *,
        run_path: Path | None = None,
        environment_content: bytes | None = None,
        expected_claim_id: str = CLAIM_ID,
        recorded_by: str = CONTROLLER_ID,
        now: datetime = PREPARED_AT,
    ):
        return prepare_activation_attempt(
            self.run_path if run_path is None else run_path,
            self.packet_content,
            self.environment_content
            if environment_content is None
            else environment_content,
            expected_claim_id=expected_claim_id,
            attempt_id=attempt_id,
            recorded_by=recorded_by,
            now=now,
            claim_root=self.claim_root,
            coordination_root=self.coordination_root,
        )

    def prepared_attempt(self, attempt_id: str = "ACTIVATION-1") -> ActivationAttempt:
        preparation = self.prepare(attempt_id)
        if preparation.attempt is None:
            self.fail("owned canonical fixture unexpectedly failed activation preflight")
        return preparation.attempt

    def record_ready(
        self,
        attempt_id: str = "ACTIVATION-1",
        *,
        expected_claim_id: str = CLAIM_ID,
        receipt_id: str = "WORKER-READY-1",
        worker_id: str = "worker-1",
        workspace_id: str = WORKSPACE_ID,
        recorded_by: str = CONTROLLER_ID,
        now: datetime = READY_AT,
    ) -> WorkerReadyReceipt:
        return record_worker_ready(
            self.run_path,
            attempt_id=attempt_id,
            expected_claim_id=expected_claim_id,
            receipt_id=receipt_id,
            worker_id=worker_id,
            workspace_id=workspace_id,
            recorded_by=recorded_by,
            now=now,
            claim_root=self.claim_root,
            coordination_root=self.coordination_root,
        )

    def recover_claim(
        self,
        *,
        claim_id: str = "CLAIM-ACTIVATION-2",
        controller_id: str = "controller-2",
    ) -> None:
        change_controller_claim(
            self.run_path,
            kind="recovered",
            expected_claim_id=CLAIM_ID,
            claim_id=claim_id,
            controller_id=controller_id,
            reason="controller process was replaced",
            recorded_by="operator",
            now=RECOVERED_AT,
            claim_root=self.claim_root,
            coordination_root=self.coordination_root,
        )

    def commit(
        self,
        attempt_id: str = "ACTIVATION-1",
        *,
        expected_claim_id: str = CLAIM_ID,
        now: datetime = COMMITTED_AT,
    ) -> RunRecord:
        return commit_activation(
            self.run_path,
            self.packet_content,
            attempt_id=attempt_id,
            expected_claim_id=expected_claim_id,
            now=now,
            claim_root=self.claim_root,
            coordination_root=self.coordination_root,
        )

    def test_blocked_preflight_creates_no_attempt(self) -> None:
        blocked = copy.deepcopy(self.environment_data)
        nested_mapping(blocked, "workspace")["clean"] = False

        preparation = self.prepare(
            "ACTIVATION-BLOCKED",
            environment_content=serialize_mapping(blocked),
        )

        self.assertFalse(preparation.prepared)
        self.assertIn(
            "workspace-not-clean",
            [blocker.code for blocker in preparation.preflight.blockers],
        )
        self.assertFalse(self.attempt_path("ACTIVATION-BLOCKED").exists())
        self.assertEqual(RunRecord.from_path(self.run_path).current_state, "initialized")

    def test_wrong_and_stale_expected_claim_create_no_attempt(self) -> None:
        with self.assertRaisesRegex(
            ActivationConflictError,
            "current claim is CLAIM-ACTIVATION-1, not expected claim CLAIM-WRONG",
        ):
            self.prepare(
                "ACTIVATION-WRONG-CLAIM",
                expected_claim_id="CLAIM-WRONG",
            )
        self.assertFalse(self.attempt_path("ACTIVATION-WRONG-CLAIM").exists())

        self.recover_claim(claim_id="CLAIM-ACTIVATION-2", controller_id=CONTROLLER_ID)
        with self.assertRaisesRegex(
            ActivationConflictError,
            "current claim is CLAIM-ACTIVATION-2, not expected claim CLAIM-ACTIVATION-1",
        ):
            self.prepare("ACTIVATION-STALE-CLAIM")
        self.assertFalse(self.attempt_path("ACTIVATION-STALE-CLAIM").exists())

    def test_persisted_attempt_has_exact_current_bindings(self) -> None:
        run_bytes = self.run_path.read_bytes()
        publication_bytes = run_publication_path(
            RUN_ID, self.coordination_root
        ).read_bytes()
        claim_event_bytes = (self.claim_path / "000000.json").read_bytes()

        attempt = self.prepared_attempt()
        attempt_path = self.attempt_path(attempt.id)
        persisted = load_mapping(attempt_path)

        self.assertEqual(ActivationAttempt.from_path(attempt_path), attempt)
        self.assertEqual(
            persisted,
            {
                "schema_version": 1,
                "id": "ACTIVATION-1",
                "created_at": "2026-08-30T12:06:00Z",
                "recorded_by": CONTROLLER_ID,
                "run": {
                    "id": RUN_ID,
                    "publication_id": PUBLICATION_ID,
                    "publication_digest": digest(publication_bytes),
                    "record_digest": digest(run_bytes),
                    "expected_state": "initialized",
                    "expected_transition_sequence": 1,
                },
                "claim": {
                    "id": CLAIM_ID,
                    "sequence": 0,
                    "controller_id": CONTROLLER_ID,
                    "event_digest": digest(claim_event_bytes),
                },
                "packet": {
                    "id": "EXAMPLE-1",
                    "digest": digest(self.packet_content),
                },
                "environment": {
                    "source_digest": digest(self.environment_content),
                    "snapshot": self.environment.to_mapping(),
                },
                "preflight": {
                    "evaluator": EXECUTION_PREFLIGHT_EVALUATOR,
                    "required_controller_state": "owned",
                    "evaluated_at": "2026-08-30T12:06:00Z",
                    "max_age_seconds": 300,
                    "ready": True,
                    "blockers": [],
                },
            },
        )

    def test_duplicate_attempt_id_conflicts_without_overwrite(self) -> None:
        self.prepared_attempt()
        attempt_path = self.attempt_path("ACTIVATION-1")
        original = attempt_path.read_bytes()

        with self.assertRaisesRegex(
            RunCoordinationConflictError,
            "immutable JSON destination already exists",
        ):
            self.prepare("ACTIVATION-1")

        self.assertEqual(attempt_path.read_bytes(), original)

    def test_only_one_worker_ready_receipt_can_exist_per_attempt(self) -> None:
        attempt = self.prepared_attempt()
        first = self.record_ready(attempt.id)
        ready_path = self.ready_path(attempt.id)
        original = ready_path.read_bytes()

        with self.assertRaisesRegex(
            RunCoordinationConflictError,
            "immutable JSON destination already exists",
        ):
            self.record_ready(
                attempt.id,
                receipt_id="WORKER-READY-2",
                worker_id="worker-2",
            )

        self.assertEqual(ready_path.read_bytes(), original)
        self.assertEqual(WorkerReadyReceipt.from_path(ready_path), first)

    def test_non_current_recorder_is_rejected_at_attempt_and_readiness(self) -> None:
        with self.assertRaisesRegex(
            ActivationConflictError,
            "activation recorder controller-2 is not current controller controller-1",
        ):
            self.prepare("ACTIVATION-WRONG-RECORDER", recorded_by="controller-2")
        self.assertFalse(self.attempt_path("ACTIVATION-WRONG-RECORDER").exists())

        attempt = self.prepared_attempt()
        with self.assertRaisesRegex(
            ActivationConflictError,
            "worker-ready recorder controller-2 is not current controller controller-1",
        ):
            self.record_ready(attempt.id, recorded_by="controller-2")
        self.assertFalse(self.ready_path(attempt.id).exists())

    def test_stale_attempt_claim_rejects_worker_readiness(self) -> None:
        attempt = self.prepared_attempt()
        self.recover_claim()

        with self.assertRaisesRegex(
            ActivationConflictError,
            "attempt claim is CLAIM-ACTIVATION-1, not CLAIM-ACTIVATION-2",
        ):
            self.record_ready(
                attempt.id,
                expected_claim_id="CLAIM-ACTIVATION-2",
                recorded_by="controller-2",
            )

        self.assertFalse(self.ready_path(attempt.id).exists())

    def test_workspace_mismatch_rejects_worker_readiness(self) -> None:
        attempt = self.prepared_attempt()

        with self.assertRaisesRegex(
            ActivationConflictError,
            "prepared workspace is workspace-other, not attempt workspace workspace-1",
        ):
            self.record_ready(attempt.id, workspace_id="workspace-other")

        self.assertFalse(self.ready_path(attempt.id).exists())

    def test_copy_symlink_hardlink_and_renamed_run_are_rejected(self) -> None:
        copied = self.root / "copied-run.json"
        copied.write_bytes(self.run_path.read_bytes())
        with self.assertRaisesRegex(
            RunCoordinationError, "not the registered canonical entry"
        ):
            self.prepare("ACTIVATION-COPY", run_path=copied)
        self.assertFalse(self.attempt_path("ACTIVATION-COPY").exists())

        symlink = self.root / "symlink-run.json"
        symlink.symlink_to(self.run_path)
        with self.assertRaisesRegex(RunCoordinationError, "symbolic-link alias"):
            self.prepare("ACTIVATION-SYMLINK", run_path=symlink)
        self.assertFalse(self.attempt_path("ACTIVATION-SYMLINK").exists())

        hardlink = self.root / "hardlink-run.json"
        os.link(self.run_path, hardlink)
        try:
            with self.assertRaisesRegex(RunCoordinationError, "exactly one hard link"):
                self.prepare("ACTIVATION-HARDLINK", run_path=hardlink)
        finally:
            hardlink.unlink()
        self.assertFalse(self.attempt_path("ACTIVATION-HARDLINK").exists())

        renamed = self.root / "renamed-run.json"
        self.run_path.rename(renamed)
        with self.assertRaisesRegex(
            RunCoordinationError, "not the registered canonical entry"
        ):
            self.prepare("ACTIVATION-RENAMED", run_path=renamed)
        self.assertFalse(self.attempt_path("ACTIVATION-RENAMED").exists())

    def test_changed_initialized_bytes_block_activation_after_publication(self) -> None:
        self.run_path.write_bytes(self.run_path.read_bytes() + b"\n")

        with self.assertRaisesRegex(
            RunCoordinationConflictError,
            "canonical initialized run bytes no longer match",
        ):
            self.prepare("ACTIVATION-CHANGED-RUN")

        self.assertFalse(self.attempt_path("ACTIVATION-CHANGED-RUN").exists())

    def test_claim_recovery_before_commit_blocks_activation(self) -> None:
        attempt = self.prepared_attempt()
        self.record_ready(attempt.id)
        self.recover_claim()

        with self.assertRaisesRegex(
            ActivationConflictError,
            "attempt claim is CLAIM-ACTIVATION-1, not CLAIM-ACTIVATION-2",
        ):
            self.commit(attempt.id, expected_claim_id="CLAIM-ACTIVATION-2")

        self.assertEqual(RunRecord.from_path(self.run_path).current_state, "initialized")

    def test_matching_prepare_ready_commit_persists_exact_transition_binding(self) -> None:
        attempt = self.prepared_attempt()
        receipt = self.record_ready(attempt.id)
        attempt_bytes = self.attempt_path(attempt.id).read_bytes()
        ready_bytes = self.ready_path(attempt.id).read_bytes()

        activated = self.commit(attempt.id)
        persisted = RunRecord.from_path(self.run_path)
        transition = activated.transitions[-1]
        binding = transition.activation
        if binding is None:
            self.fail("active transition did not contain an activation binding")

        self.assertEqual(activated, persisted)
        self.assertEqual(activated.current_state, "active")
        self.assertEqual(transition.sequence, 1)
        self.assertEqual(transition.at, "2026-08-30T12:08:00Z")
        self.assertEqual(transition.from_state, "initialized")
        self.assertEqual(transition.to_state, "active")
        self.assertEqual(transition.reason, "worker-handoff-committed")
        self.assertEqual(transition.recorded_by, CONTROLLER_ID)
        self.assertEqual(
            {
                "claim_id": binding.claim_id,
                "attempt_id": binding.attempt_id,
                "attempt_digest": binding.attempt_digest,
                "worker_id": binding.worker_id,
                "worker_ready_digest": binding.worker_ready_digest,
            },
            {
                "claim_id": CLAIM_ID,
                "attempt_id": attempt.id,
                "attempt_digest": digest(attempt_bytes),
                "worker_id": receipt.worker_id,
                "worker_ready_digest": digest(ready_bytes),
            },
        )

    def test_exact_commit_retry_is_idempotent(self) -> None:
        attempt = self.prepared_attempt()
        self.record_ready(attempt.id)
        first = self.commit(attempt.id)
        first_bytes = self.run_path.read_bytes()

        retried = self.commit(attempt.id, now=STALE_COMMIT_AT)

        self.assertEqual(retried, first)
        self.assertEqual(self.run_path.read_bytes(), first_bytes)
        self.assertEqual(len(retried.transitions), 2)

    def test_active_retry_rejects_wrong_claim_and_changed_or_malformed_packet(self) -> None:
        attempt = self.prepared_attempt()
        self.record_ready(attempt.id)
        activated = self.commit(attempt.id)
        active_bytes = self.run_path.read_bytes()

        cases = (
            ("wrong claim", self.packet_content, "CLAIM-WRONG", "activation claim is"),
            (
                "changed packet",
                self.packet_content + b"\n",
                CLAIM_ID,
                "current packet digest is",
            ),
            ("malformed packet", b"{\n", CLAIM_ID, "current packet digest is"),
        )
        for label, packet_content, expected_claim_id, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ActivationConflictError, message):
                    commit_activation(
                        self.run_path,
                        packet_content,
                        attempt_id=attempt.id,
                        expected_claim_id=expected_claim_id,
                        now=STALE_COMMIT_AT,
                        claim_root=self.claim_root,
                        coordination_root=self.coordination_root,
                    )

        self.assertEqual(RunRecord.from_path(self.run_path), activated)
        self.assertEqual(self.run_path.read_bytes(), active_bytes)

    def test_exact_retry_confirms_durability_after_visible_commit_sync_failure(self) -> None:
        attempt = self.prepared_attempt()
        self.record_ready(attempt.id)

        with patch(
            "software_factory.run_coordination._fsync_directory",
            side_effect=OSError("injected directory fsync failure"),
        ):
            with self.assertRaisesRegex(
                RunCoordinationPublicationError,
                "replacement is visible.*durability could not be confirmed",
            ):
                self.commit(attempt.id)

        visible = RunRecord.from_path(self.run_path)
        visible_bytes = self.run_path.read_bytes()
        self.assertEqual(visible.current_state, "active")

        with patch(
            "software_factory.run_coordination._fsync_directory"
        ) as sync_directory:
            retried = self.commit(attempt.id)

        sync_directory.assert_called_once_with(self.run_path.parent)
        self.assertEqual(retried, visible)
        self.assertEqual(self.run_path.read_bytes(), visible_bytes)

    def test_another_attempt_cannot_activate_an_already_active_run(self) -> None:
        first = self.prepared_attempt("ACTIVATION-1")
        second = self.prepared_attempt("ACTIVATION-2")
        self.record_ready(first.id, receipt_id="WORKER-READY-1", worker_id="worker-1")
        self.record_ready(second.id, receipt_id="WORKER-READY-2", worker_id="worker-2")
        activated = self.commit(first.id)
        activated_bytes = self.run_path.read_bytes()

        with self.assertRaisesRegex(
            ActivationConflictError,
            "already active with a different activation binding",
        ):
            self.commit(second.id)

        self.assertEqual(RunRecord.from_path(self.run_path), activated)
        self.assertEqual(self.run_path.read_bytes(), activated_bytes)

    def test_stale_environment_blocks_commit(self) -> None:
        attempt = self.prepared_attempt()
        self.record_ready(attempt.id)

        with self.assertRaisesRegex(
            ActivationConflictError,
            "activation preflight no longer passes:.*snapshot is 301 seconds old",
        ):
            self.commit(attempt.id, now=STALE_COMMIT_AT)

        self.assertEqual(RunRecord.from_path(self.run_path).current_state, "initialized")

    def test_malformed_and_duplicate_attempt_json_fail_closed(self) -> None:
        malformed = self.prepared_attempt("ACTIVATION-MALFORMED")
        duplicate = self.prepared_attempt("ACTIVATION-DUPLICATE")
        self.attempt_path(malformed.id).write_text("{\n", encoding="utf-8")
        self.attempt_path(duplicate.id).write_text(
            '{"schema_version": 1, "schema_version": 1}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ActivationError, "is not valid JSON"):
            self.record_ready(malformed.id)
        with self.assertRaisesRegex(
            ActivationError, "is ambiguous: duplicate JSON member: schema_version"
        ):
            self.record_ready(duplicate.id)

        self.assertFalse(self.ready_path(malformed.id).exists())
        self.assertFalse(self.ready_path(duplicate.id).exists())

    def test_malformed_and_duplicate_worker_ready_json_fail_closed(self) -> None:
        malformed = self.prepared_attempt("ACTIVATION-MALFORMED-READY")
        duplicate = self.prepared_attempt("ACTIVATION-DUPLICATE-READY")
        self.record_ready(
            malformed.id,
            receipt_id="WORKER-READY-MALFORMED",
        )
        self.record_ready(
            duplicate.id,
            receipt_id="WORKER-READY-DUPLICATE",
        )
        self.ready_path(malformed.id).write_text("{\n", encoding="utf-8")
        self.ready_path(duplicate.id).write_text(
            '{"schema_version": 1, "schema_version": 1}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ActivationError, "is not valid JSON"):
            self.commit(malformed.id)
        with self.assertRaisesRegex(
            ActivationError, "is ambiguous: duplicate JSON member: schema_version"
        ):
            self.commit(duplicate.id)

        self.assertEqual(RunRecord.from_path(self.run_path).current_state, "initialized")

    def test_commit_rejects_tampered_worker_ready_actor_and_time(self) -> None:
        wrong_actor = self.prepared_attempt("ACTIVATION-WRONG-READY-ACTOR")
        predating = self.prepared_attempt("ACTIVATION-PREDATING-READY")
        self.record_ready(
            wrong_actor.id,
            receipt_id="WORKER-READY-WRONG-ACTOR",
        )
        self.record_ready(
            predating.id,
            receipt_id="WORKER-READY-PREDATING",
        )

        wrong_actor_data = load_mapping(self.ready_path(wrong_actor.id))
        wrong_actor_data["recorded_by"] = "controller-other"
        self.ready_path(wrong_actor.id).write_bytes(serialize_mapping(wrong_actor_data))

        predating_data = load_mapping(self.ready_path(predating.id))
        predating_data["recorded_at"] = "2026-08-30T12:05:59Z"
        self.ready_path(predating.id).write_bytes(serialize_mapping(predating_data))

        with self.assertRaisesRegex(
            ActivationConflictError,
            "worker-ready recorder does not match the attempt controller",
        ):
            self.commit(wrong_actor.id)
        with self.assertRaisesRegex(
            ActivationConflictError,
            "worker-ready receipt predates the activation attempt",
        ):
            self.commit(predating.id)

        self.assertEqual(RunRecord.from_path(self.run_path).current_state, "initialized")

    def test_cli_full_flow_and_plain_language_boundaries(self) -> None:
        attempt_output = io.StringIO()
        with redirect_stdout(attempt_output):
            attempted = main(
                [
                    "run",
                    "activation",
                    "attempt",
                    str(self.run_path),
                    str(self.packet_path),
                    str(self.environment_path),
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                attempt_id_factory=lambda: "ACTIVATION-CLI",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: PREPARED_AT,
            )

        ready_output = io.StringIO()
        with redirect_stdout(ready_output):
            readied = main(
                [
                    "run",
                    "activation",
                    "worker-ready",
                    str(self.run_path),
                    "ACTIVATION-CLI",
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--worker-id",
                    "worker-cli",
                    "--workspace-id",
                    WORKSPACE_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                receipt_id_factory=lambda: "WORKER-READY-CLI",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: READY_AT,
            )

        prepared_show_output = io.StringIO()
        with redirect_stdout(prepared_show_output):
            prepared_shown = main(
                [
                    "run",
                    "activation",
                    "show",
                    str(self.run_path),
                    "ACTIVATION-CLI",
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
            )

        commit_output = io.StringIO()
        with redirect_stdout(commit_output):
            committed = main(
                [
                    "run",
                    "activation",
                    "commit",
                    str(self.run_path),
                    str(self.packet_path),
                    "ACTIVATION-CLI",
                    "--expected-claim-id",
                    CLAIM_ID,
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: COMMITTED_AT,
            )

        retry_output = io.StringIO()
        with redirect_stdout(retry_output):
            retried = main(
                [
                    "run",
                    "activation",
                    "commit",
                    str(self.run_path),
                    str(self.packet_path),
                    "ACTIVATION-CLI",
                    "--expected-claim-id",
                    CLAIM_ID,
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: STALE_COMMIT_AT,
            )

        active_show_output = io.StringIO()
        with redirect_stdout(active_show_output):
            active_shown = main(
                [
                    "run",
                    "activation",
                    "show",
                    str(self.run_path),
                    "ACTIVATION-CLI",
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
            )

        self.assertEqual(
            (attempted, readied, prepared_shown, committed, retried, active_shown),
            (0, 0, 0, 0, 0, 0),
        )
        self.assertIn("Required controller state: owned", attempt_output.getvalue())
        self.assertIn(
            "No worker-ready observation has been recorded.",
            attempt_output.getvalue(),
        )
        self.assertIn(
            "The run remains initialized; no worker was told to begin.",
            attempt_output.getvalue(),
        )
        self.assertIn(
            "grants no external authority and is not completion",
            attempt_output.getvalue(),
        )
        self.assertIn(
            "controller's durable record of an adapter-reported idle worker",
            ready_output.getvalue(),
        )
        self.assertIn("It is not independent proof", ready_output.getvalue())
        self.assertIn("does not grant the worker authority", ready_output.getvalue())
        self.assertIn(
            "controller-recorded adapter observation, not independent proof",
            prepared_show_output.getvalue(),
        )
        self.assertIn("Current state: initialized", prepared_show_output.getvalue())
        self.assertIn("It is not work completion", commit_output.getvalue())
        self.assertIn(
            "authority to perform an external action", commit_output.getvalue()
        )
        self.assertIn(
            "did not tell the prepared worker to begin", retry_output.getvalue()
        )
        self.assertIn("Current state: active", active_show_output.getvalue())
        self.assertIn(
            "does not mean the\n  work is complete and grants no merge, deploy",
            active_show_output.getvalue(),
        )

    def test_activation_show_rejects_dangling_worker_ready_symlink(self) -> None:
        attempt = self.prepared_attempt("ACTIVATION-DANGLING-READY")
        self.ready_path(attempt.id).symlink_to(self.root / "missing-worker-ready.json")
        output = io.StringIO()
        errors = io.StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = main(
                [
                    "run",
                    "activation",
                    "show",
                    str(self.run_path),
                    attempt.id,
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("must not be a symbolic link", errors.getvalue())
        self.assertNotIn(
            "No worker-ready observation has been recorded.",
            errors.getvalue(),
        )

    def test_duplicate_attempt_and_worker_ready_cli_conflicts_return_one(self) -> None:
        attempt = self.prepared_attempt("ACTIVATION-CLI-DUPLICATE")
        attempt_errors = io.StringIO()
        with redirect_stderr(attempt_errors):
            duplicate_attempt = main(
                [
                    "run",
                    "activation",
                    "attempt",
                    str(self.run_path),
                    str(self.packet_path),
                    str(self.environment_path),
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                attempt_id_factory=lambda: attempt.id,
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: PREPARED_AT,
            )

        self.record_ready(attempt.id, receipt_id="WORKER-READY-CLI-DUPLICATE")
        ready_errors = io.StringIO()
        with redirect_stderr(ready_errors):
            duplicate_ready = main(
                [
                    "run",
                    "activation",
                    "worker-ready",
                    str(self.run_path),
                    attempt.id,
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--worker-id",
                    "worker-duplicate",
                    "--workspace-id",
                    WORKSPACE_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                receipt_id_factory=lambda: "WORKER-READY-CLI-CONTENDER",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: READY_AT,
            )

        self.assertEqual((duplicate_attempt, duplicate_ready), (1, 1))
        self.assertIn("Activation attempt was not recorded", attempt_errors.getvalue())
        self.assertIn("immutable JSON destination already exists", attempt_errors.getvalue())
        self.assertIn(
            "Worker-ready observation was not recorded",
            ready_errors.getvalue(),
        )
        self.assertIn("immutable JSON destination already exists", ready_errors.getvalue())

    def test_cli_blocked_conflict_and_malformed_exit_codes(self) -> None:
        blocked_data = copy.deepcopy(self.environment_data)
        nested_mapping(blocked_data, "workspace")["available"] = False
        blocked_path = self.root / "blocked-environment.json"
        blocked_path.write_bytes(serialize_mapping(blocked_data))
        blocked_output = io.StringIO()
        with redirect_stdout(blocked_output):
            blocked = main(
                [
                    "run",
                    "activation",
                    "attempt",
                    str(self.run_path),
                    str(self.packet_path),
                    str(blocked_path),
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                attempt_id_factory=lambda: "ACTIVATION-CLI-BLOCKED",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: PREPARED_AT,
            )

        conflict_error = io.StringIO()
        with redirect_stderr(conflict_error):
            conflict = main(
                [
                    "run",
                    "activation",
                    "attempt",
                    str(self.run_path),
                    str(self.packet_path),
                    str(self.environment_path),
                    "--expected-claim-id",
                    "CLAIM-WRONG",
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                attempt_id_factory=lambda: "ACTIVATION-CLI-CONFLICT",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: PREPARED_AT,
            )

        malformed_path = self.root / "malformed-environment.json"
        malformed_path.write_text("{\n", encoding="utf-8")
        malformed_error = io.StringIO()
        with redirect_stderr(malformed_error):
            malformed = main(
                [
                    "run",
                    "activation",
                    "attempt",
                    str(self.run_path),
                    str(self.packet_path),
                    str(malformed_path),
                    "--expected-claim-id",
                    CLAIM_ID,
                    "--recorded-by",
                    CONTROLLER_ID,
                ],
                attempt_id_factory=lambda: "ACTIVATION-CLI-MALFORMED",
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: PREPARED_AT,
            )

        self.prepared_attempt("ACTIVATION-NOT-READY")
        no_ready_error = io.StringIO()
        with redirect_stderr(no_ready_error):
            no_ready = main(
                [
                    "run",
                    "activation",
                    "commit",
                    str(self.run_path),
                    str(self.packet_path),
                    "ACTIVATION-NOT-READY",
                    "--expected-claim-id",
                    CLAIM_ID,
                ],
                claim_root=self.claim_root,
                coordination_root=self.coordination_root,
                clock=lambda: COMMITTED_AT,
            )

        self.assertEqual((blocked, conflict, malformed, no_ready), (1, 1, 2, 2))
        self.assertIn("is blocked from execution", blocked_output.getvalue())
        self.assertIn(
            "Activation attempt was not recorded: current claim is",
            conflict_error.getvalue(),
        )
        self.assertIn("Blocked: environment snapshot is not valid JSON", malformed_error.getvalue())
        self.assertIn(
            "Blocked: cannot inspect worker-ready receipt",
            no_ready_error.getvalue(),
        )
        self.assertFalse(self.attempt_path("ACTIVATION-CLI-BLOCKED").exists())
        self.assertFalse(self.attempt_path("ACTIVATION-CLI-CONFLICT").exists())
        self.assertFalse(self.attempt_path("ACTIVATION-CLI-MALFORMED").exists())


if __name__ == "__main__":
    unittest.main()
