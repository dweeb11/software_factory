from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from software_factory.cli import main
from software_factory.preflight import (
    ExecutionEnvironment,
    PreflightError,
    WorkspaceObservation,
    evaluate_preflight,
)
from software_factory.presentation import render_preflight
from software_factory.runs import RunRecord, initialize_run


RUN_PATH = PROJECT_ROOT / "examples" / "initialized-run" / "run.json"
PACKET_PATH = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"
READY_ENVIRONMENT_PATH = (
    PROJECT_ROOT / "examples" / "preflight-ready" / "environment.json"
)
BLOCKED_ENVIRONMENT_PATH = (
    PROJECT_ROOT / "examples" / "preflight-blocked" / "environment.json"
)
FIXED_NOW = datetime(2026, 8, 30, 12, 6, 0, tzinfo=timezone.utc)


def load_mapping(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} did not contain an object")
    return cast(dict[str, object], value)


def mapping_field(data: dict[str, object], key: str) -> dict[str, object]:
    value = data[key]
    if not isinstance(value, dict):
        raise AssertionError(f"{key} was not an object")
    return cast(dict[str, object], value)


def list_field(data: dict[str, object], key: str) -> list[object]:
    value = data[key]
    if not isinstance(value, list):
        raise AssertionError(f"{key} was not a list")
    return cast(list[object], value)


def ready_environment() -> ExecutionEnvironment:
    return ExecutionEnvironment.from_path(READY_ENVIRONMENT_PATH)


def initialized_run() -> RunRecord:
    return RunRecord.from_path(RUN_PATH)


def activation_binding() -> dict[str, str]:
    return {
        "claim_id": "CLAIM-1",
        "attempt_id": "ACTIVATION-1",
        "attempt_digest": "sha256:" + "a" * 64,
        "worker_id": "worker-1",
        "worker_ready_digest": "sha256:" + "b" * 64,
    }


class ExecutionEnvironmentTests(unittest.TestCase):
    def test_ready_example_is_well_formed(self) -> None:
        environment = ready_environment()

        self.assertEqual(environment.run_id, "RUN-PREFLIGHT-1")
        self.assertEqual(environment.controller.state, "available")
        self.assertEqual(environment.requested_actions, frozenset({"edit"}))

    def test_owned_controller_state_requires_environment_schema_version_two(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        mapping_field(data, "controller")["state"] = "owned"

        data["schema_version"] = 1
        with self.assertRaisesRegex(
            PreflightError,
            "controller.state must be one of: available, contended, unknown",
        ):
            ExecutionEnvironment.from_mapping(data)

        data["schema_version"] = 2
        environment = ExecutionEnvironment.from_mapping(data)

        self.assertEqual(environment.schema_version, 2)
        self.assertEqual(environment.controller.state, "owned")

    def test_unknown_controller_state_is_malformed(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        mapping_field(data, "controller")["state"] = "probably-free"

        with self.assertRaisesRegex(PreflightError, "controller.state must be one of"):
            ExecutionEnvironment.from_mapping(data)

    def test_non_boolean_workspace_fact_is_malformed(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        mapping_field(data, "workspace")["isolated"] = "yes"

        with self.assertRaisesRegex(PreflightError, "workspace.isolated must be a boolean"):
            ExecutionEnvironment.from_mapping(data)

    def test_duplicate_capability_is_malformed(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        capabilities = list_field(data, "capabilities")
        capabilities.append(copy.deepcopy(capabilities[0]))

        with self.assertRaisesRegex(PreflightError, "duplicate capability"):
            ExecutionEnvironment.from_mapping(data)

    def test_unknown_requested_action_is_malformed(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        data["requested_actions"] = ["rewrite-history"]

        with self.assertRaisesRegex(PreflightError, "unknown actions: rewrite-history"):
            ExecutionEnvironment.from_mapping(data)

    def test_duplicate_json_member_is_malformed(self) -> None:
        text = READY_ENVIRONMENT_PATH.read_text(encoding="utf-8")
        ambiguous = text.replace(
            '"state": "current",',
            '"state": "revoked",\n    "state": "current",',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment.json"
            path.write_text(ambiguous, encoding="utf-8")

            with self.assertRaisesRegex(
                PreflightError, "ambiguous: duplicate JSON member: state"
            ):
                ExecutionEnvironment.from_path(path)

    def test_direct_construction_enforces_runtime_types(self) -> None:
        malformed_workspace = WorkspaceObservation(
            id="workspace-1",
            available=cast(bool, cast(object, "false")),
            isolated=cast(bool, cast(object, "false")),
            clean=cast(bool, cast(object, "false")),
            observed_by="unvalidated-constructor",
        )

        with self.assertRaisesRegex(PreflightError, "workspace.available must be a boolean"):
            replace(ready_environment(), workspace=malformed_workspace)


class PreflightEvaluationTests(unittest.TestCase):
    def test_fresh_complete_environment_passes(self) -> None:
        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=FIXED_NOW,
        )

        self.assertTrue(report.ready)
        self.assertEqual(report.blockers, ())
        self.assertTrue(report.packet_readiness.ready)

    def test_same_inputs_and_time_produce_same_report(self) -> None:
        arguments = (
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
        )

        first = evaluate_preflight(*arguments, now=FIXED_NOW)
        second = evaluate_preflight(*arguments, now=FIXED_NOW)

        self.assertEqual(first, second)

    def test_blocked_example_accumulates_every_environment_blocker(self) -> None:
        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ExecutionEnvironment.from_path(BLOCKED_ENVIRONMENT_PATH),
            now=FIXED_NOW,
        )

        self.assertFalse(report.ready)
        self.assertEqual(
            [blocker.code for blocker in report.blockers],
            [
                "controller-contended",
                "workspace-unavailable",
                "workspace-not-isolated",
                "workspace-not-clean",
                "capability-missing:command-execution",
                "capability-unavailable:workspace-read",
                "capability-missing:workspace-write",
                "verification-route-missing:runtime",
                "verification-route-unavailable:test",
                "required-action-missing:edit",
                "action-outside-worker-boundary:push",
                "authority-unknown",
            ],
        )

    def test_snapshot_cannot_predate_current_run_state(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        data["generated_at"] = "2026-08-30T11:59:59Z"

        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ExecutionEnvironment.from_mapping(data),
            now=datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
        )

        self.assertIn(
            "environment-predates-run-state",
            [item.code for item in report.blockers],
        )

    def test_stale_and_future_snapshots_block(self) -> None:
        stale = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=datetime(2026, 8, 30, 12, 10, 1, tzinfo=timezone.utc),
        )
        future = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=datetime(2026, 8, 30, 12, 4, 59, tzinfo=timezone.utc),
        )

        self.assertIn("environment-stale", [item.code for item in stale.blockers])
        self.assertIn("environment-from-future", [item.code for item in future.blockers])

    def test_fractional_stale_age_is_reported_without_truncation(self) -> None:
        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=datetime(2026, 8, 30, 12, 10, 0, 500000, tzinfo=timezone.utc),
        )

        stale = next(item for item in report.blockers if item.code == "environment-stale")
        self.assertIn("300.5 seconds old; the limit is 300", stale.message)

    def test_snapshot_for_another_run_blocks(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        data["run_id"] = "RUN-SOMEONE-ELSE"

        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ExecutionEnvironment.from_mapping(data),
            now=FIXED_NOW,
        )

        self.assertIn("environment-wrong-run", [item.code for item in report.blockers])

    def test_changed_packet_bytes_block_even_when_packet_remains_valid(self) -> None:
        changed_content = PACKET_PATH.read_bytes() + b"\n"

        report = evaluate_preflight(
            initialized_run(), changed_content, ready_environment(), now=FIXED_NOW
        )

        self.assertIn("packet-digest-mismatch", [item.code for item in report.blockers])

    def test_packet_readiness_is_recomputed(self) -> None:
        data = load_mapping(PACKET_PATH)
        mapping_field(data, "scope")["include"] = []
        changed_content = (json.dumps(data) + "\n").encode("utf-8")

        report = evaluate_preflight(
            initialized_run(), changed_content, ready_environment(), now=FIXED_NOW
        )

        codes = [item.code for item in report.blockers]
        self.assertIn("packet-digest-mismatch", codes)
        self.assertIn("packet:included-scope-empty", codes)

    def test_non_initialized_run_blocks(self) -> None:
        data = initialized_run().to_mapping()
        list_field(data, "transitions").append(
            {
                "sequence": 1,
                "at": "2026-08-30T12:01:00Z",
                "from": "initialized",
                "to": "active",
                "reason": "worker-handoff-committed",
                "recorded_by": "controller-1",
                "activation": activation_binding(),
            }
        )

        report = evaluate_preflight(
            RunRecord.from_mapping(data),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=FIXED_NOW,
        )

        self.assertIn("run-not-initialized", [item.code for item in report.blockers])

    def test_missing_verification_route_blocks(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        routes = list_field(data, "verification_routes")
        data["verification_routes"] = routes[:1]

        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ExecutionEnvironment.from_mapping(data),
            now=FIXED_NOW,
        )

        self.assertIn(
            "verification-route-missing:runtime",
            [item.code for item in report.blockers],
        )

    def test_revoked_authority_blocks(self) -> None:
        data = load_mapping(READY_ENVIRONMENT_PATH)
        mapping_field(data, "authority")["state"] = "revoked"

        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ExecutionEnvironment.from_mapping(data),
            now=FIXED_NOW,
        )

        self.assertIn("authority-revoked", [item.code for item in report.blockers])

    def test_packet_bound_authority_must_allow_requested_local_action(self) -> None:
        packet_data = load_mapping(PACKET_PATH)
        mapping_field(packet_data, "authority")["mode"] = "advise"
        packet_content = (json.dumps(packet_data) + "\n").encode("utf-8")
        initialization = initialize_run(
            packet_content,
            run_id="RUN-NO-EDIT",
            initiated_by="operator",
            now=FIXED_NOW,
        )
        if initialization.record is None:
            self.fail("authority-only packet change unexpectedly blocked readiness")
        environment_data = load_mapping(READY_ENVIRONMENT_PATH)
        environment_data["run_id"] = "RUN-NO-EDIT"

        report = evaluate_preflight(
            initialization.record,
            packet_content,
            ExecutionEnvironment.from_mapping(environment_data),
            now=FIXED_NOW,
        )

        self.assertIn("action-not-authorized:edit", [item.code for item in report.blockers])

    def test_plain_language_report_explains_collector_and_no_action_boundary(self) -> None:
        report = evaluate_preflight(
            initialized_run(),
            PACKET_PATH.read_bytes(),
            ready_environment(),
            now=FIXED_NOW,
        )

        rendered = render_preflight(report)

        self.assertIn("passes the current execution preflight", rendered)
        self.assertIn("Controller observed by: controller-probe", rendered)
        self.assertIn("workspace-write — observed by workspace-probe", rendered)
        self.assertIn("Authority state: current", rendered)
        self.assertIn("Collector observations report state; they do not grant authority", rendered)
        self.assertIn("passing preflight verdict is not an activation token", rendered)
        self.assertIn("no run transition was recorded", rendered)
        self.assertIn("no worker or external action was started", rendered)


class PreflightCommandTests(unittest.TestCase):
    def test_ready_preflight_returns_zero(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "run",
                    "preflight",
                    str(RUN_PATH),
                    str(PACKET_PATH),
                    str(READY_ENVIRONMENT_PATH),
                ],
                clock=lambda: FIXED_NOW,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("passes the current execution preflight", output.getvalue())

    def test_blocked_preflight_returns_one(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(
                [
                    "run",
                    "preflight",
                    str(RUN_PATH),
                    str(PACKET_PATH),
                    str(BLOCKED_ENVIRONMENT_PATH),
                ],
                clock=lambda: FIXED_NOW,
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("is blocked from execution", output.getvalue())
        self.assertIn("Controller ownership is contended", output.getvalue())

    def test_malformed_environment_returns_two_without_traceback(self) -> None:
        errors = io.StringIO()

        with redirect_stderr(errors):
            exit_code = main(
                [
                    "run",
                    "preflight",
                    str(RUN_PATH),
                    str(PACKET_PATH),
                    str(PACKET_PATH),
                ],
                clock=lambda: FIXED_NOW,
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("environment is missing fields", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_preflight_does_not_modify_any_input(self) -> None:
        paths = (RUN_PATH, PACKET_PATH, READY_ENVIRONMENT_PATH)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "run",
                    "preflight",
                    str(RUN_PATH),
                    str(PACKET_PATH),
                    str(READY_ENVIRONMENT_PATH),
                ],
                clock=lambda: FIXED_NOW,
            )

        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
