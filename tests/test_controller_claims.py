from __future__ import annotations

import copy
import io
import json
import os
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
from software_factory.controller_claims import (
    ControllerClaimConflictError,
    ControllerClaimError,
    ControllerClaimHistory,
    ControllerClaimPersistenceError,
    ControllerClaimPublicationError,
    controller_claim_path,
    create_controller_claim,
    create_controller_claim_change,
    default_controller_claim_root,
    persist_controller_claim_change,
    persist_initial_controller_claim,
    require_claim_for_run,
)
from software_factory.presentation import render_controller_claim
from software_factory.runs import RunRecord


RUN_PATH = PROJECT_ROOT / "examples" / "initialized-run" / "run.json"
EXAMPLE_CLAIM_PATH = PROJECT_ROOT / "examples" / "controller-claim"
FIXED_NOW = datetime(2026, 8, 30, 12, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 30, 12, 20, 0, tzinfo=timezone.utc)


def initialized_run() -> RunRecord:
    return RunRecord.from_path(RUN_PATH)


def copy_run(directory: str) -> Path:
    path = Path(directory) / "run.json"
    path.write_bytes(RUN_PATH.read_bytes())
    return path


def initial_history() -> ControllerClaimHistory:
    return create_controller_claim(
        initialized_run(),
        claim_id="CLAIM-1",
        controller_id="controller-1",
        recorded_by="operator",
        now=FIXED_NOW,
    )


def event_data(history: ControllerClaimHistory, index: int = 0) -> dict[str, Any]:
    return copy.deepcopy(history.events[index].to_mapping())


class ControllerClaimRecordTests(unittest.TestCase):
    def test_checked_in_example_is_valid(self) -> None:
        history = ControllerClaimHistory.from_path(EXAMPLE_CLAIM_PATH)

        self.assertEqual(history.run_id, "RUN-PREFLIGHT-1")
        self.assertEqual(history.current.claim_id, "CLAIM-EXAMPLE-1")

    def test_default_claim_root_does_not_follow_mutable_home_environment(self) -> None:
        expected = default_controller_claim_root()

        with patch.dict(os.environ, {"HOME": "/tmp/different-home"}):
            observed = default_controller_claim_root()

        self.assertEqual(observed, expected)

    def test_missing_account_database_entry_fails_closed(self) -> None:
        with patch("pwd.getpwuid", side_effect=KeyError("missing")):
            with self.assertRaisesRegex(
                ControllerClaimPersistenceError, "no POSIX account entry exists"
            ):
                default_controller_claim_root()

    def test_claim_location_is_derived_from_run_identity(self) -> None:
        root = Path("claims")

        first = controller_claim_path("RUN-1", root)
        same = controller_claim_path("RUN-1", root)
        other = controller_claim_path("RUN-2", root)

        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertEqual(first.parent, root)
        self.assertTrue(first.name.startswith("run-"))
        self.assertTrue(first.name.endswith(".controller-claim"))

    def test_claim_for_another_run_is_rejected(self) -> None:
        event = initial_history().current
        other = ControllerClaimHistory(
            events=(
                type(event)(
                    schema_version=event.schema_version,
                    sequence=event.sequence,
                    kind=event.kind,
                    at=event.at,
                    run_id="RUN-OTHER",
                    claim_id=event.claim_id,
                    controller_id=event.controller_id,
                    previous_claim_id=event.previous_claim_id,
                    reason=event.reason,
                    recorded_by=event.recorded_by,
                ),
            )
        )

        with self.assertRaisesRegex(ControllerClaimError, "belongs to run RUN-OTHER"):
            require_claim_for_run(initialized_run(), other)

    def test_initial_claim_is_bound_to_initialized_run(self) -> None:
        history = initial_history()

        self.assertEqual(history.run_id, "RUN-PREFLIGHT-1")
        self.assertEqual(history.current.claim_id, "CLAIM-1")
        self.assertEqual(history.current.controller_id, "controller-1")
        self.assertEqual(history.current.kind, "acquired")
        self.assertIsNone(history.current.previous_claim_id)

    def test_non_initialized_run_cannot_receive_initial_claim(self) -> None:
        data = initialized_run().to_mapping()
        transitions = data["transitions"]
        if not isinstance(transitions, list):
            self.fail("run transitions were not a list")
        transitions.append(
            {
                "sequence": 1,
                "at": "2026-08-30T12:05:00Z",
                "from": "initialized",
                "to": "active",
                "reason": "test-only",
                "recorded_by": "controller-1",
            }
        )

        with self.assertRaisesRegex(
            ControllerClaimConflictError, "initial controller acquisition requires initialized"
        ):
            create_controller_claim(
                RunRecord.from_mapping(data),
                claim_id="CLAIM-1",
                controller_id="controller-1",
                recorded_by="operator",
                now=FIXED_NOW,
            )

    def test_transfer_and_recovery_derive_current_owner_from_history(self) -> None:
        initial = initial_history()
        transfer = create_controller_claim_change(
            initial,
            kind="transferred",
            expected_claim_id="CLAIM-1",
            claim_id="CLAIM-2",
            controller_id="controller-2",
            reason="planned handoff",
            recorded_by="operator",
            now=LATER,
        )
        transferred = ControllerClaimHistory(events=initial.events + (transfer,))
        recovery = create_controller_claim_change(
            transferred,
            kind="recovered",
            expected_claim_id="CLAIM-2",
            claim_id="CLAIM-3",
            controller_id="controller-3",
            reason="controller process exited unexpectedly",
            recorded_by="operator",
            now=datetime(2026, 8, 30, 12, 30, 0, tzinfo=timezone.utc),
        )
        recovered = ControllerClaimHistory(events=transferred.events + (recovery,))

        self.assertEqual(recovered.current.claim_id, "CLAIM-3")
        self.assertEqual(recovered.current.controller_id, "controller-3")
        self.assertEqual(recovered.current.previous_claim_id, "CLAIM-2")
        self.assertEqual([event.kind for event in recovered.events], ["acquired", "transferred", "recovered"])

    def test_change_requires_exact_current_claim_id(self) -> None:
        with self.assertRaisesRegex(
            ControllerClaimConflictError, "current claim is CLAIM-1, not expected claim CLAIM-OLD"
        ):
            create_controller_claim_change(
                initial_history(),
                kind="recovered",
                expected_claim_id="CLAIM-OLD",
                claim_id="CLAIM-2",
                controller_id="controller-2",
                reason="stale attempt",
                recorded_by="operator",
                now=LATER,
            )

    def test_event_time_cannot_predate_current_ownership(self) -> None:
        with self.assertRaisesRegex(ControllerClaimError, "cannot predate the prior event"):
            create_controller_claim_change(
                initial_history(),
                kind="transferred",
                expected_claim_id="CLAIM-1",
                claim_id="CLAIM-2",
                controller_id="controller-2",
                reason="planned handoff",
                recorded_by="operator",
                now=datetime(2026, 8, 30, 12, 9, 59, tzinfo=timezone.utc),
            )

    def test_plain_language_view_denies_expiration_and_implicit_authority(self) -> None:
        rendered = render_controller_claim(initial_history())

        self.assertIn("RUN-PREFLIGHT-1 is claimed by controller-1", rendered)
        self.assertIn("Claim ID: CLAIM-1", rendered)
        self.assertIn("does not expire automatically", rendered)
        self.assertIn("Claim age alone never authorizes takeover", rendered)
        self.assertIn("must name the exact current claim and leave a receipt", rendered)
        self.assertIn("ownership alone grants no authority to edit, commit, push, merge", rendered)
        self.assertIn("deploy, start a worker, or perform another action", rendered)


class ControllerClaimPersistenceTests(unittest.TestCase):
    def test_initial_claim_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"

            persist_initial_controller_claim(initial_history(), claim_path)
            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(loaded, initial_history())

    def test_competing_initial_acquisition_cannot_overwrite_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            persist_initial_controller_claim(initial_history(), claim_path)
            contender = create_controller_claim(
                initialized_run(),
                claim_id="CLAIM-CONTENDER",
                controller_id="controller-2",
                recorded_by="operator",
                now=LATER,
            )

            with self.assertRaisesRegex(
                ControllerClaimConflictError, "controller claim already exists"
            ):
                persist_initial_controller_claim(contender, claim_path)

            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(loaded.current.claim_id, "CLAIM-1")
        self.assertEqual(len(loaded.events), 1)

    def test_transfer_publishes_next_immutable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            persist_initial_controller_claim(initial_history(), claim_path)
            event = create_controller_claim_change(
                ControllerClaimHistory.from_path(claim_path),
                kind="transferred",
                expected_claim_id="CLAIM-1",
                claim_id="CLAIM-2",
                controller_id="controller-2",
                reason="planned handoff",
                recorded_by="operator",
                now=LATER,
            )

            changed = persist_controller_claim_change(event, claim_path)

            self.assertEqual((claim_path / "000000.json").read_bytes(), (json.dumps(event_data(initial_history()), indent=2) + "\n").encode("utf-8"))
            self.assertTrue((claim_path / "000001.json").is_file())

        self.assertEqual(changed.current.claim_id, "CLAIM-2")

    def test_two_changes_for_same_sequence_cannot_both_win(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            persist_initial_controller_claim(initial_history(), claim_path)
            observed = ControllerClaimHistory.from_path(claim_path)
            first = create_controller_claim_change(
                observed,
                kind="transferred",
                expected_claim_id="CLAIM-1",
                claim_id="CLAIM-2",
                controller_id="controller-2",
                reason="planned handoff",
                recorded_by="operator",
                now=LATER,
            )
            contender = create_controller_claim_change(
                observed,
                kind="recovered",
                expected_claim_id="CLAIM-1",
                claim_id="CLAIM-3",
                controller_id="controller-3",
                reason="competing recovery",
                recorded_by="another-operator",
                now=LATER,
            )

            persist_controller_claim_change(first, claim_path)
            with self.assertRaisesRegex(
                ControllerClaimConflictError, "ownership changed before"
            ):
                persist_controller_claim_change(contender, claim_path)
            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(loaded.current.claim_id, "CLAIM-2")
        self.assertEqual(len(loaded.events), 2)

    def test_empty_partial_claim_directory_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            claim_path.mkdir()

            with self.assertRaisesRegex(ControllerClaimError, "contains no ownership events"):
                ControllerClaimHistory.from_path(claim_path)

    def test_sibling_writer_staging_file_is_not_claim_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            persist_initial_controller_claim(initial_history(), claim_path)
            sibling = claim_path.parent / ".000001.json.writer.tmp"
            sibling.write_text("staged", encoding="utf-8")

            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(loaded.current.claim_id, "CLAIM-1")

    def test_unexpected_temporary_record_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            persist_initial_controller_claim(initial_history(), claim_path)
            (claim_path / ".000001.json.partial.tmp").write_text("partial", encoding="utf-8")

            with self.assertRaisesRegex(ControllerClaimError, "entries requiring inspection"):
                ControllerClaimHistory.from_path(claim_path)

    def test_missing_sequence_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            claim_path.mkdir()
            (claim_path / "000001.json").write_text(
                json.dumps(event_data(initial_history())), encoding="utf-8"
            )

            with self.assertRaisesRegex(ControllerClaimError, "sequence is incomplete"):
                ControllerClaimHistory.from_path(claim_path)

    def test_duplicate_json_member_blocks_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"
            claim_path.mkdir()
            (claim_path / "000000.json").write_text(
                '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
            )

            with self.assertRaisesRegex(ControllerClaimError, "ambiguous: duplicate JSON member"):
                ControllerClaimHistory.from_path(claim_path)

    def test_post_creation_failure_leaves_blocking_state_for_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "controller-claim"

            with patch(
                "software_factory.controller_claims.tempfile.NamedTemporaryFile",
                side_effect=OSError("cannot stage"),
            ):
                with self.assertRaisesRegex(
                    ControllerClaimPublicationError, "inspect it before retrying"
                ):
                    persist_initial_controller_claim(initial_history(), claim_path)

            self.assertTrue(claim_path.is_dir())
            with self.assertRaises(ControllerClaimConflictError):
                persist_initial_controller_claim(initial_history(), claim_path)


class ControllerClaimCommandTests(unittest.TestCase):
    def test_unavailable_default_claim_namespace_blocks_without_traceback(self) -> None:
        errors = io.StringIO()
        with patch(
            "software_factory.cli.default_controller_claim_root",
            side_effect=ControllerClaimPersistenceError("account unavailable"),
        ):
            with redirect_stderr(errors):
                exit_code = main(["run", "claim", "show", str(RUN_PATH)])

        self.assertEqual(exit_code, 2)
        self.assertIn("Blocked: account unavailable", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_acquire_show_and_recover_explain_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            claim_root = Path(directory) / "claims"
            claim_path = controller_claim_path(initialized_run().id, claim_root)
            acquired_output = io.StringIO()
            with redirect_stdout(acquired_output):
                acquired = main(
                    [
                        "run",
                        "claim",
                        "acquire",
                        str(run_path),
                        "--controller-id",
                        "controller-1",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-1",
                    claim_root=claim_root,
                    clock=lambda: FIXED_NOW,
                )

            recovered_output = io.StringIO()
            with redirect_stdout(recovered_output):
                recovered = main(
                    [
                        "run",
                        "claim",
                        "recover",
                        str(run_path),
                        "--expected-claim-id",
                        "CLAIM-1",
                        "--controller-id",
                        "controller-2",
                        "--reason",
                        "controller process exited unexpectedly",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-2",
                    claim_root=claim_root,
                    clock=lambda: LATER,
                )

            show_output = io.StringIO()
            with redirect_stdout(show_output):
                shown = main(
                    ["run", "claim", "show", str(run_path)],
                    claim_root=claim_root,
                )

        self.assertEqual((acquired, recovered, shown), (0, 0, 0))
        self.assertIn(f"Controller claim acquired at {claim_path}", acquired_output.getvalue())
        self.assertIn("ownership recovered; receipt recorded", recovered_output.getvalue())
        self.assertIn("claimed by controller-2", show_output.getvalue())
        self.assertIn("Replaced claim: CLAIM-1", show_output.getvalue())

    def test_competing_acquire_returns_one_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            claim_root = Path(directory) / "claims"
            claim_root.mkdir()
            claim_path = controller_claim_path(initialized_run().id, claim_root)
            persist_initial_controller_claim(initial_history(), claim_path)
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "run",
                        "claim",
                        "acquire",
                        str(run_path),
                        "--controller-id",
                        "controller-2",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-2",
                    claim_root=claim_root,
                    clock=lambda: LATER,
                )
            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(exit_code, 1)
        self.assertEqual(loaded.current.claim_id, "CLAIM-1")
        self.assertIn("Not acquired", errors.getvalue())
        self.assertIn("already exists", errors.getvalue())

    def test_hard_link_run_alias_cannot_acquire_a_second_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            alias_path = Path(directory) / "run-alias.json"
            os.link(run_path, alias_path)
            claim_root = Path(directory) / "claims"

            output = io.StringIO()
            with redirect_stdout(output):
                first = main(
                    [
                        "run",
                        "claim",
                        "acquire",
                        str(run_path),
                        "--controller-id",
                        "controller-1",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-1",
                    claim_root=claim_root,
                    clock=lambda: FIXED_NOW,
                )
            errors = io.StringIO()
            with redirect_stderr(errors):
                contender = main(
                    [
                        "run",
                        "claim",
                        "acquire",
                        str(alias_path),
                        "--controller-id",
                        "controller-2",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-2",
                    claim_root=claim_root,
                    clock=lambda: LATER,
                )

        self.assertEqual(first, 0)
        self.assertEqual(contender, 1)
        self.assertIn("already exists", errors.getvalue())

    def test_stale_recovery_returns_one_and_leaves_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            claim_root = Path(directory) / "claims"
            claim_root.mkdir()
            claim_path = controller_claim_path(initialized_run().id, claim_root)
            persist_initial_controller_claim(initial_history(), claim_path)
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(
                    [
                        "run",
                        "claim",
                        "recover",
                        str(run_path),
                        "--expected-claim-id",
                        "CLAIM-OLD",
                        "--controller-id",
                        "controller-2",
                        "--reason",
                        "stale recovery",
                        "--recorded-by",
                        "operator",
                    ],
                    claim_id_factory=lambda: "CLAIM-2",
                    claim_root=claim_root,
                    clock=lambda: LATER,
                )
            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(loaded.events), 1)
        self.assertIn("Requested ownership change was not published", errors.getvalue())
        self.assertIn("Re-read the current claim", errors.getvalue())
        self.assertIn("not expected claim CLAIM-OLD", errors.getvalue())

    def test_show_of_partial_claim_returns_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            claim_root = Path(directory) / "claims"
            claim_root.mkdir()
            claim_path = controller_claim_path(initialized_run().id, claim_root)
            claim_path.mkdir()
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(
                    ["run", "claim", "show", str(run_path)],
                    claim_root=claim_root,
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("contains no ownership events", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_durability_failure_reports_inspection_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_path = copy_run(directory)
            claim_root = Path(directory) / "claims"
            claim_path = controller_claim_path(initialized_run().id, claim_root)
            errors = io.StringIO()

            with patch(
                "software_factory.controller_claims._fsync_directory",
                side_effect=[None, None, OSError("parent sync failed")],
            ):
                with redirect_stderr(errors):
                    exit_code = main(
                        [
                            "run",
                            "claim",
                            "acquire",
                            str(run_path),
                            "--controller-id",
                            "controller-1",
                            "--recorded-by",
                            "operator",
                        ],
                        claim_id_factory=lambda: "CLAIM-1",
                        claim_root=claim_root,
                        clock=lambda: FIXED_NOW,
                    )
            loaded = ControllerClaimHistory.from_path(claim_path)

        self.assertEqual(exit_code, 3)
        self.assertEqual(loaded.current.claim_id, "CLAIM-1")
        self.assertIn("Acquisition requires inspection", errors.getvalue())
        self.assertIn("durable directory publication could not be confirmed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
