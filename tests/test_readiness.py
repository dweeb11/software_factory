from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from software_factory.cli import main
from software_factory.presentation import render_readiness
from software_factory.readiness import evaluate_readiness
from software_factory.work_packets import PacketError, WorkPacket


READY_PATH = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"
BLOCKED_PATH = PROJECT_ROOT / "examples" / "blocked-change" / "packet.json"


def ready_data() -> dict:
    return json.loads(READY_PATH.read_text(encoding="utf-8"))


def blocked_data() -> dict:
    return json.loads(BLOCKED_PATH.read_text(encoding="utf-8"))


class ReadinessTests(unittest.TestCase):
    def test_complete_packet_is_ready(self) -> None:
        report = evaluate_readiness(WorkPacket.from_path(READY_PATH))

        self.assertTrue(report.ready)
        self.assertEqual(report.blockers, ())

    def test_all_readiness_blockers_are_reported_together(self) -> None:
        data = blocked_data()
        data["dependencies"].append({"id": "PACKET-13", "state": "unsatisfied"})

        report = evaluate_readiness(WorkPacket.from_mapping(data))

        self.assertFalse(report.ready)
        self.assertEqual(
            [blocker.code for blocker in report.blockers],
            [
                "included-scope-empty",
                "decision-unresolved:D-2",
                "dependency-unknown:PACKET-12",
                "dependency-unsatisfied:PACKET-13",
            ],
        )

    def test_every_unresolved_decision_is_reported(self) -> None:
        data = blocked_data()
        data["decisions"].append(
            {
                "id": "D-3",
                "question": "How long should recovery remain available?",
                "state": "unresolved",
            }
        )

        report = evaluate_readiness(WorkPacket.from_mapping(data))

        messages = [blocker.message for blocker in report.blockers]
        self.assertTrue(any("D-2 is unresolved" in message for message in messages))
        self.assertTrue(any("D-3 is unresolved" in message for message in messages))

    def test_satisfied_dependency_passes(self) -> None:
        data = ready_data()
        data["dependencies"] = [{"id": "PACKET-12", "state": "satisfied"}]

        report = evaluate_readiness(WorkPacket.from_mapping(data))

        self.assertTrue(report.ready)

    def test_unknown_dependency_state_is_malformed(self) -> None:
        data = ready_data()
        data["dependencies"] = [{"id": "PACKET-12", "state": "pending"}]

        with self.assertRaisesRegex(PacketError, "state must be one of"):
            WorkPacket.from_mapping(data)

    def test_duplicate_dependency_ids_are_malformed(self) -> None:
        data = ready_data()
        dependency = {"id": "PACKET-12", "state": "satisfied"}
        data["dependencies"] = [dependency, copy.deepcopy(dependency)]

        with self.assertRaisesRegex(PacketError, "duplicate dependency id"):
            WorkPacket.from_mapping(data)

    def test_readable_ready_verdict_explains_authority(self) -> None:
        report = evaluate_readiness(WorkPacket.from_path(READY_PATH))

        rendered = render_readiness(report)

        self.assertIn("EXAMPLE-1 is ready for run preflight.", rendered)
        self.assertIn("Every criterion names required evidence", rendered)
        self.assertIn("Mode: deliver", rendered)
        self.assertIn("May: commit, edit, open-pr, push, update-pr, update-tracker", rendered)
        self.assertIn("May not: deploy, merge", rendered)
        self.assertIn("Explicit denials: deploy, merge", rendered)
        self.assertIn("No run was started", rendered)

    def test_readable_blocked_verdict_lists_each_blocker(self) -> None:
        report = evaluate_readiness(WorkPacket.from_path(BLOCKED_PATH))

        rendered = render_readiness(report)

        self.assertIn("EXAMPLE-2 is not ready for run preflight.", rendered)
        self.assertIn("change work must name at least one included scope item", rendered)
        self.assertIn("D-2 is unresolved", rendered)
        self.assertIn("Dependency PACKET-12 has unknown state", rendered)

    def test_readiness_evaluation_does_not_modify_the_packet_file(self) -> None:
        before_bytes = READY_PATH.read_bytes()
        before_modified = READY_PATH.stat().st_mtime_ns

        evaluate_readiness(WorkPacket.from_path(READY_PATH))

        self.assertEqual(READY_PATH.read_bytes(), before_bytes)
        self.assertEqual(READY_PATH.stat().st_mtime_ns, before_modified)


class ReadinessCommandTests(unittest.TestCase):
    def test_ready_packet_returns_exit_zero(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["packet", "readiness", str(READY_PATH)])

        self.assertEqual(exit_code, 0)
        self.assertIn("EXAMPLE-1 is ready for run preflight", output.getvalue())

    def test_blocked_packet_returns_exit_one(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["packet", "readiness", str(BLOCKED_PATH)])

        self.assertEqual(exit_code, 1)
        self.assertIn("EXAMPLE-2 is not ready for run preflight", output.getvalue())

    def test_invalid_utf8_packet_returns_exit_two_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packet.json"
            path.write_bytes(b"\xff\xfe")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(["packet", "readiness", str(path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("is not valid UTF-8", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_malformed_packet_returns_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packet.json"
            path.write_text("{}", encoding="utf-8")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(["packet", "readiness", str(path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("Blocked: schema_version must be the integer 1", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
