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
from software_factory.presentation import render_work_packet
from software_factory.work_packets import PacketError, WorkPacket


EXAMPLE_PATH = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"


def example_data() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


class WorkPacketTests(unittest.TestCase):
    def test_example_packet_is_valid(self) -> None:
        packet = WorkPacket.from_path(EXAMPLE_PATH)

        self.assertEqual(packet.id, "EXAMPLE-1")
        self.assertEqual(packet.kind, "change")
        self.assertFalse(packet.unresolved_decisions)

    def test_missing_intent_is_rejected(self) -> None:
        data = example_data()
        del data["intent"]

        with self.assertRaisesRegex(PacketError, "intent must be a non-empty string"):
            WorkPacket.from_mapping(data)

    def test_unknown_packet_kind_is_rejected(self) -> None:
        data = example_data()
        data["kind"] = "miscellaneous"

        with self.assertRaisesRegex(PacketError, "kind must be one of"):
            WorkPacket.from_mapping(data)

    def test_duplicate_acceptance_ids_are_rejected(self) -> None:
        data = example_data()
        data["acceptance"].append(copy.deepcopy(data["acceptance"][0]))

        with self.assertRaisesRegex(PacketError, "duplicate acceptance criterion id"):
            WorkPacket.from_mapping(data)

    def test_unknown_evidence_kind_is_rejected(self) -> None:
        data = example_data()
        data["acceptance"][0]["evidence_required"] = ["confidence"]

        with self.assertRaisesRegex(PacketError, "unknown kinds: confidence"):
            WorkPacket.from_mapping(data)

    def test_unresolved_decision_cannot_claim_an_answer(self) -> None:
        data = example_data()
        data["decisions"][0]["state"] = "unresolved"

        with self.assertRaisesRegex(PacketError, "must not claim a decision"):
            WorkPacket.from_mapping(data)

    def test_explicit_denial_overrides_land_authority(self) -> None:
        data = example_data()
        data["authority"]["mode"] = "land"
        data["authority"]["deny"] = ["merge"]
        packet = WorkPacket.from_mapping(data)

        self.assertFalse(packet.authority.allows("merge"))
        self.assertTrue(packet.authority.allows("open-pr"))

    def test_unknown_authority_action_is_rejected(self) -> None:
        data = example_data()
        data["authority"]["allow"] = ["rewrite-history"]

        with self.assertRaisesRegex(PacketError, "unknown actions: rewrite-history"):
            WorkPacket.from_mapping(data)

    def test_plain_language_view_explains_authority_and_required_evidence(self) -> None:
        packet = WorkPacket.from_path(EXAMPLE_PATH)

        rendered = render_work_packet(packet)

        self.assertIn("AC-2: Restoration preserves project members and settings.", rendered)
        self.assertIn("Evidence required: test, runtime", rendered)
        self.assertIn("May: commit, edit, open-pr, push, update-pr, update-tracker", rendered)
        self.assertIn("Explicit denials: deploy, merge", rendered)
        self.assertIn("Human action required\n  None before execution.", rendered)

    def test_plain_language_view_surfaces_unresolved_decisions(self) -> None:
        data = example_data()
        data["decisions"] = [
            {
                "id": "D-2",
                "question": "Should recovery be visible to project members?",
                "state": "unresolved",
            }
        ]
        packet = WorkPacket.from_mapping(data)

        rendered = render_work_packet(packet)

        self.assertIn("? D-2: Should recovery be visible to project members?", rendered)
        self.assertIn("Resolve 1 decision(s) before execution.", rendered)


class CommandLineTests(unittest.TestCase):
    def test_validate_reports_a_valid_packet(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["packet", "validate", str(EXAMPLE_PATH)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "Valid work packet: EXAMPLE-1\n")

    def test_malformed_packet_blocks_with_a_plain_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packet.json"
            path.write_text("{}", encoding="utf-8")
            errors = io.StringIO()

            with redirect_stderr(errors):
                exit_code = main(["packet", "validate", str(path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("Blocked: schema_version must be the integer 1", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
