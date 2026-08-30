from __future__ import annotations

import copy
import hashlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from software_factory.cli import main
from software_factory.presentation import render_run
from software_factory.runs import (
    READINESS_EVALUATOR,
    RunError,
    RunPersistenceError,
    RunPublicationError,
    RunRecord,
    initialize_run,
    persist_run,
)


READY_PATH = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"
BLOCKED_PATH = PROJECT_ROOT / "examples" / "blocked-change" / "packet.json"
FIXED_NOW = datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc)
FIXED_RUN_ID = "RUN-TEST-1"


def initialized_record() -> RunRecord:
    result = initialize_run(
        READY_PATH.read_bytes(),
        run_id=FIXED_RUN_ID,
        initiated_by="test-operator",
        now=FIXED_NOW,
        source=f"packet {READY_PATH}",
    )
    if result.record is None:
        raise AssertionError("ready example did not initialize")
    return result.record


def run_data() -> dict[str, Any]:
    return copy.deepcopy(initialized_record().to_mapping())


class RunInitializationTests(unittest.TestCase):
    def test_ready_packet_initializes_a_run_bound_to_exact_bytes(self) -> None:
        packet_content = READY_PATH.read_bytes()

        result = initialize_run(
            packet_content,
            run_id=FIXED_RUN_ID,
            initiated_by="test-operator",
            now=FIXED_NOW,
        )

        self.assertTrue(result.initialized)
        self.assertIsNotNone(result.record)
        record = result.record
        if record is None:
            self.fail("initialized result did not contain a record")
        self.assertEqual(record.id, FIXED_RUN_ID)
        self.assertEqual(record.packet.id, "EXAMPLE-1")
        self.assertEqual(
            record.packet.digest,
            f"sha256:{hashlib.sha256(packet_content).hexdigest()}",
        )
        self.assertEqual(record.readiness.evaluator, READINESS_EVALUATOR)
        self.assertEqual(record.readiness.evaluated_at, "2026-08-30T12:34:56Z")
        self.assertEqual(record.current_state, "initialized")
        self.assertFalse(record.terminal)
        self.assertNotIn("current_state", record.to_mapping())

    def test_initial_transition_records_provenance(self) -> None:
        transition = initialized_record().transitions[0]

        self.assertEqual(transition.sequence, 0)
        self.assertIsNone(transition.from_state)
        self.assertEqual(transition.to_state, "initialized")
        self.assertEqual(transition.reason, "packet-ready")
        self.assertEqual(transition.recorded_by, "test-operator")

    def test_blocked_packet_does_not_produce_a_run_record(self) -> None:
        result = initialize_run(
            BLOCKED_PATH.read_bytes(),
            run_id=FIXED_RUN_ID,
            initiated_by="test-operator",
            now=FIXED_NOW,
        )

        self.assertFalse(result.initialized)
        self.assertIsNone(result.record)
        self.assertEqual(len(result.readiness.blockers), 3)

    def test_cosmetic_packet_change_produces_a_distinct_exact_digest(self) -> None:
        original = READY_PATH.read_bytes()
        reformatted = original + b"\n"

        first = initialize_run(
            original,
            run_id="RUN-1",
            initiated_by="test-operator",
            now=FIXED_NOW,
        )
        second = initialize_run(
            reformatted,
            run_id="RUN-2",
            initiated_by="test-operator",
            now=FIXED_NOW,
        )

        if first.record is None or second.record is None:
            self.fail("ready packet variants did not initialize")
        self.assertNotEqual(first.record.packet.digest, second.record.packet.digest)

    def test_naive_initialization_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunError, "now must include a UTC offset"):
            initialize_run(
                READY_PATH.read_bytes(),
                run_id=FIXED_RUN_ID,
                initiated_by="test-operator",
                now=FIXED_NOW.replace(tzinfo=None),
            )


class RunRecordTests(unittest.TestCase):
    def test_current_state_is_derived_from_last_valid_transition(self) -> None:
        data = run_data()
        data["transitions"].append(
            {
                "sequence": 1,
                "at": "2026-08-30T12:35:00Z",
                "from": "initialized",
                "to": "active",
                "reason": "preflight-passed",
                "recorded_by": "controller-1",
            }
        )

        record = RunRecord.from_mapping(data)

        self.assertEqual(record.current_state, "active")
        self.assertFalse(record.terminal)

    def test_transition_from_must_match_prior_state(self) -> None:
        data = run_data()
        data["transitions"].append(
            {
                "sequence": 1,
                "at": "2026-08-30T12:35:00Z",
                "from": "waiting",
                "to": "active",
                "reason": "resumed",
                "recorded_by": "controller-1",
            }
        )

        with self.assertRaisesRegex(RunError, "from must match prior state initialized"):
            RunRecord.from_mapping(data)

    def test_transition_sequence_must_be_contiguous(self) -> None:
        data = run_data()
        data["transitions"][0]["sequence"] = 1

        with self.assertRaisesRegex(RunError, "sequence must be the integer 0"):
            RunRecord.from_mapping(data)

    def test_transition_after_terminal_state_is_rejected(self) -> None:
        data = run_data()
        data["transitions"].extend(
            [
                {
                    "sequence": 1,
                    "at": "2026-08-30T12:35:00Z",
                    "from": "initialized",
                    "to": "blocked",
                    "reason": "preflight-blocked",
                    "recorded_by": "controller-1",
                },
                {
                    "sequence": 2,
                    "at": "2026-08-30T12:36:00Z",
                    "from": "blocked",
                    "to": "active",
                    "reason": "unsafe-resume",
                    "recorded_by": "controller-1",
                },
            ]
        )

        with self.assertRaisesRegex(RunError, "no transition may follow terminal state blocked"):
            RunRecord.from_mapping(data)

    def test_stored_current_state_is_rejected_as_competing_truth(self) -> None:
        data = run_data()
        data["current_state"] = "initialized"

        with self.assertRaisesRegex(RunError, "contains unknown fields: current_state"):
            RunRecord.from_mapping(data)

    def test_initial_transition_actor_must_match_initiator(self) -> None:
        data = run_data()
        data["transitions"][0]["recorded_by"] = "someone-else"

        with self.assertRaisesRegex(RunError, "must be recorded by initiated_by"):
            RunRecord.from_mapping(data)

    def test_timestamp_must_be_strict_utc_with_seconds(self) -> None:
        data = run_data()
        data["readiness"]["evaluated_at"] = "2026-08-30T12:34Z"

        with self.assertRaisesRegex(RunError, "must be UTC ISO-8601 with seconds"):
            RunRecord.from_mapping(data)

    def test_plain_language_view_explains_state_and_boundaries(self) -> None:
        rendered = render_run(initialized_record())

        self.assertIn("RUN-TEST-1 is initialized", rendered)
        self.assertIn("Exact version: sha256:", rendered)
        self.assertIn("Current state: initialized", rendered)
        self.assertIn("Terminal: no", rendered)
        self.assertIn("nothing → initialized", rendered)
        self.assertIn("did not start a worker", rendered)
        self.assertIn("does not grant authority", rendered)


class RunPersistenceTests(unittest.TestCase):
    def test_persisted_run_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"

            persist_run(initialized_record(), path)
            loaded = RunRecord.from_path(path)

        self.assertEqual(loaded, initialized_record())

    def test_existing_record_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text("existing state\n", encoding="utf-8")

            with self.assertRaisesRegex(RunPersistenceError, "already exists"):
                persist_run(initialized_record(), path)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing state\n")

    def test_parent_directory_is_synced_after_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "run.json"

            with patch("software_factory.runs._fsync_directory") as sync_directory:
                persist_run(initialized_record(), path)

            sync_directory.assert_called_once_with(parent)

    def test_link_failure_leaves_no_destination_or_temporary_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "run.json"

            with patch(
                "software_factory.runs.os.link", side_effect=OSError("link failed")
            ):
                with self.assertRaisesRegex(RunPersistenceError, "link failed"):
                    persist_run(initialized_record(), path)

            self.assertFalse(path.exists())
            self.assertEqual(list(parent.iterdir()), [])

    def test_missing_run_directory_is_a_persistence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "run.json"

            with self.assertRaisesRegex(RunPersistenceError, "directory does not exist"):
                persist_run(initialized_record(), path)

            self.assertFalse(path.exists())

    def test_cleanup_failure_reports_that_canonical_record_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "run.json"

            with patch(
                "software_factory.runs.Path.unlink",
                side_effect=OSError("cleanup failed"),
            ):
                with self.assertRaisesRegex(
                    RunPublicationError, "durable run record exists"
                ):
                    persist_run(initialized_record(), path)

            self.assertEqual(RunRecord.from_path(path), initialized_record())
            temporary_records = list(parent.glob(".run.json.*.tmp"))
            self.assertEqual(len(temporary_records), 1)


class RunCommandTests(unittest.TestCase):
    def test_init_creates_and_explains_a_deterministic_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / "run.json"
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "init",
                        str(READY_PATH),
                        str(run_path),
                        "--initiated-by",
                        "test-operator",
                    ],
                    run_id_factory=lambda: FIXED_RUN_ID,
                    clock=lambda: FIXED_NOW,
                )

            record = RunRecord.from_path(run_path)

        self.assertEqual(exit_code, 0)
        self.assertEqual(record.id, FIXED_RUN_ID)
        self.assertIn(f"Run initialized at {run_path}", output.getvalue())
        self.assertIn("did not start a worker", output.getvalue())
        self.assertIn("does not grant authority", output.getvalue())

    def test_blocked_packet_returns_one_and_creates_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / "run.json"
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = main(
                    [
                        "run",
                        "init",
                        str(BLOCKED_PATH),
                        str(run_path),
                        "--initiated-by",
                        "test-operator",
                    ]
                )

            exists = run_path.exists()

        self.assertEqual(exit_code, 1)
        self.assertFalse(exists)
        self.assertIn("not ready for run preflight", output.getvalue())
        self.assertIn("No run was started", output.getvalue())

    def test_malformed_packet_returns_two_and_creates_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet_path = Path(directory) / "packet.json"
            run_path = Path(directory) / "run.json"
            packet_path.write_text("{}", encoding="utf-8")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "run",
                        "init",
                        str(packet_path),
                        str(run_path),
                        "--initiated-by",
                        "test-operator",
                    ]
                )

            exists = run_path.exists()

        self.assertEqual(exit_code, 2)
        self.assertFalse(exists)
        self.assertIn("schema_version must be the integer 1", errors.getvalue())

    def test_existing_destination_returns_three_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / "run.json"
            run_path.write_text("existing state\n", encoding="utf-8")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "run",
                        "init",
                        str(READY_PATH),
                        str(run_path),
                        "--initiated-by",
                        "test-operator",
                    ]
                )

            existing = run_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 3)
        self.assertEqual(existing, "existing state\n")
        self.assertIn("Not initialized", errors.getvalue())
        self.assertIn("run record already exists", errors.getvalue())

    def test_directory_sync_failure_reports_indeterminate_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / "run.json"
            errors = io.StringIO()

            with patch(
                "software_factory.runs._fsync_directory",
                side_effect=OSError("directory sync failed"),
            ):
                with redirect_stderr(errors):
                    exit_code = main(
                        [
                            "run",
                            "init",
                            str(READY_PATH),
                            str(run_path),
                            "--initiated-by",
                            "test-operator",
                        ],
                        run_id_factory=lambda: FIXED_RUN_ID,
                        clock=lambda: FIXED_NOW,
                    )

            record = RunRecord.from_path(run_path)

        self.assertEqual(exit_code, 3)
        self.assertEqual(record.id, FIXED_RUN_ID)
        self.assertIn("Initialization requires inspection", errors.getvalue())
        self.assertIn("currently exists", errors.getvalue())
        self.assertNotIn("Not initialized", errors.getvalue())

    def test_show_rejects_invalid_utf8_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory) / "run.json"
            run_path.write_bytes(b"\xff\xfe")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(["run", "show", str(run_path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("is not valid UTF-8", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
