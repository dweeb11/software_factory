from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import queue
import stat
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from software_factory.run_coordination import (
    RunCoordinationConflictError,
    RunCoordinationMalformedError,
    RunCoordinationPersistenceError,
    RunCoordinationPublicationError,
    RunPublication,
    activation_attempt_path,
    default_run_coordination_root,
    publish_immutable_json,
    register_run_publication,
    replace_canonical_run,
    require_canonical_run,
    run_coordination_path,
    run_key,
    run_publication_path,
    run_transaction,
    run_transaction_lock_path,
    worker_ready_path,
)
from software_factory.runs import RunRecord, initialize_run, persist_run


READY_PATH = PROJECT_ROOT / "examples" / "basic-change" / "packet.json"
FIXED_NOW = datetime(2026, 8, 30, 12, 34, 56, tzinfo=timezone.utc)
RUN_ID = "RUN-COORDINATION-1"


def initialized_record(run_id: str = RUN_ID) -> RunRecord:
    result = initialize_run(
        READY_PATH.read_bytes(),
        run_id=run_id,
        initiated_by="test-operator",
        now=FIXED_NOW,
    )
    if result.record is None:
        raise AssertionError("ready packet did not initialize a run")
    return result.record


def write_run(directory: Path, run_id: str = RUN_ID) -> Path:
    path = directory / "run.json"
    persist_run(initialized_record(run_id), path)
    return path


def publication_mapping(canonical_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "PUBLICATION-1",
        "run_id": RUN_ID,
        "canonical_path": str(canonical_path),
        "initialized_record_digest": "sha256:" + "a" * 64,
        "registered_at": "2026-08-30T12:34:56Z",
        "recorded_by": "test-operator",
    }


def register(run_path: Path, root: Path) -> RunPublication:
    return register_run_publication(
        RUN_ID,
        run_path,
        publication_id="PUBLICATION-1",
        recorded_by="test-operator",
        now=FIXED_NOW,
        coordination_root=root,
    )


def activation_binding() -> dict[str, str]:
    return {
        "claim_id": "CLAIM-1",
        "attempt_id": "ACTIVATION-1",
        "attempt_digest": "sha256:" + "a" * 64,
        "worker_id": "worker-1",
        "worker_ready_digest": "sha256:" + "b" * 64,
    }


def changed_run_content(record: RunRecord, state: str = "active") -> bytes:
    data = copy.deepcopy(record.to_mapping())
    transitions = data["transitions"]
    if not isinstance(transitions, list):
        raise AssertionError("run transitions were not a list")
    if state == "active":
        transitions.append(
            {
                "sequence": 1,
                "at": "2026-08-30T12:35:00Z",
                "from": "initialized",
                "to": "active",
                "reason": "worker-handoff-committed",
                "recorded_by": "controller-1",
                "activation": activation_binding(),
            }
        )
    elif state == "waiting":
        transitions.extend(
            [
                {
                    "sequence": 1,
                    "at": "2026-08-30T12:35:00Z",
                    "from": "initialized",
                    "to": "active",
                    "reason": "worker-handoff-committed",
                    "recorded_by": "controller-1",
                    "activation": activation_binding(),
                },
                {
                    "sequence": 2,
                    "at": "2026-08-30T12:36:00Z",
                    "from": "active",
                    "to": "waiting",
                    "reason": "awaiting-worker",
                    "recorded_by": "controller-1",
                    "activation": None,
                },
            ]
        )
    else:
        raise AssertionError(f"unsupported test state: {state}")
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def lock_worker(run_id: str, root: str, messages: Any) -> None:
    messages.put("attempting")
    with run_transaction(run_id, Path(root)):
        messages.put("acquired")


class RunPublicationRecordTests(unittest.TestCase):
    def test_round_trip_is_strict_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            canonical = Path(directory).resolve() / "run.json"
            expected = RunPublication.from_mapping(publication_mapping(canonical))
            path = Path(directory) / "publication.json"
            published = publish_immutable_json(expected.to_mapping(), path)

            observed = RunPublication.from_path(path)

        self.assertEqual(observed, expected)
        self.assertEqual(json.loads(published), expected.to_mapping())
        with self.assertRaises(FrozenInstanceError):
            setattr(observed, "id", "changed")

    def test_duplicate_member_is_rejected_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publication.json"
            path.write_text(
                '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
            )

            with self.assertRaisesRegex(
                RunCoordinationMalformedError, "ambiguous: duplicate JSON member"
            ):
                RunPublication.from_path(path)

    def test_missing_and_unknown_members_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = publication_mapping(Path(directory).resolve() / "run.json")
            del data["recorded_by"]
            with self.assertRaisesRegex(
                RunCoordinationMalformedError, "missing fields: recorded_by"
            ):
                RunPublication.from_mapping(data)

            data = publication_mapping(Path(directory).resolve() / "run.json")
            data["current_digest"] = "competing truth"
            with self.assertRaisesRegex(
                RunCoordinationMalformedError, "unknown fields: current_digest"
            ):
                RunPublication.from_mapping(data)

    def test_schema_rejects_relative_path_digest_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = publication_mapping(Path(directory).resolve() / "run.json")
            cases = (
                ("canonical_path", "relative/run.json", "absolute path"),
                ("initialized_record_digest", "SHA256:" + "A" * 64, "lowercase"),
                ("registered_at", "2026-08-30T12:34Z", "with seconds"),
            )
            for field, value, message in cases:
                with self.subTest(field=field):
                    data = dict(valid)
                    data[field] = value
                    with self.assertRaisesRegex(
                        RunCoordinationMalformedError, message
                    ):
                        RunPublication.from_mapping(data)


class CoordinationLocationTests(unittest.TestCase):
    def test_home_environment_is_ignored(self) -> None:
        expected = default_run_coordination_root()

        with patch.dict(os.environ, {"HOME": "/tmp/untrusted-home"}):
            observed = default_run_coordination_root()

        self.assertEqual(observed, expected)
        self.assertEqual(
            observed.parts[-2:], (".software-factory", "run-coordination")
        )

    def test_missing_account_entry_and_home_fail_closed(self) -> None:
        with patch(
            "software_factory.run_coordination.pwd.getpwuid",
            side_effect=KeyError("missing"),
        ):
            with self.assertRaisesRegex(
                RunCoordinationPersistenceError, "no POSIX account entry"
            ):
                default_run_coordination_root()

        with patch(
            "software_factory.run_coordination.pwd.getpwuid",
            return_value=SimpleNamespace(pw_dir=""),
        ):
            with self.assertRaisesRegex(
                RunCoordinationPersistenceError, "has no home directory"
            ):
                default_run_coordination_root()

    def test_run_and_opaque_ids_are_hashed_into_safe_paths(self) -> None:
        root = Path("coordination")
        run_directory = run_coordination_path("../opaque/run", root)
        attempt = activation_attempt_path(
            "../opaque/run", "../../attempt", root
        )
        ready = worker_ready_path("../opaque/run", "worker/one", root)

        self.assertEqual(run_key("../opaque/run"), hashlib.sha256(b"../opaque/run").hexdigest())
        self.assertEqual(run_directory.parent, root)
        self.assertEqual(run_directory.name, f"run-{run_key('../opaque/run')}")
        self.assertEqual(attempt.parent, run_directory / "activation-attempts")
        self.assertEqual(ready.parent, run_directory / "worker-ready")
        self.assertNotIn("..", attempt.name)
        self.assertNotIn("/", ready.name)


class ImmutableJsonPublicationTests(unittest.TestCase):
    def test_publication_is_deterministic_durable_and_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "record.json"
            first_mapping = {"z": 1, "a": {"value": True}}
            expected = b'{\n  "a": {\n    "value": true\n  },\n  "z": 1\n}\n'

            observed = publish_immutable_json(first_mapping, destination)

            self.assertEqual(observed, expected)
            self.assertEqual(destination.read_bytes(), expected)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            with self.assertRaisesRegex(
                RunCoordinationConflictError, "already exists"
            ):
                publish_immutable_json({"replacement": True}, destination)
            self.assertEqual(destination.read_bytes(), expected)


class RunRegistrationTests(unittest.TestCase):
    def test_registration_records_exact_entry_and_bytes_and_creates_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            actual_parent = base / "actual"
            actual_parent.mkdir()
            alias_parent = base / "alias"
            alias_parent.symlink_to(actual_parent, target_is_directory=True)
            run_path = write_run(actual_parent)
            supplied = alias_parent / run_path.name
            root = base / "coordination"
            exact_bytes = run_path.read_bytes()

            publication = register(supplied, root)
            loaded = RunPublication.from_path(run_publication_path(RUN_ID, root))
            run_directory = run_coordination_path(RUN_ID, root)

            self.assertEqual(publication, loaded)
            self.assertEqual(publication.canonical_path, str(run_path.resolve().parent / run_path.name))
            self.assertEqual(
                publication.initialized_record_digest,
                f"sha256:{hashlib.sha256(exact_bytes).hexdigest()}",
            )
            self.assertEqual(publication.registered_at, "2026-08-30T12:34:56Z")
            self.assertTrue((run_directory / "activation-attempts").is_dir())
            self.assertTrue((run_directory / "worker-ready").is_dir())
            self.assertTrue(run_transaction_lock_path(RUN_ID, root).is_file())
            self.assertEqual(stat.S_IMODE(run_directory.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(run_transaction_lock_path(RUN_ID, root).stat().st_mode),
                0o600,
            )

    def test_registration_never_overwrites_or_adopts_existing_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"
            first = register(run_path, root)
            publication_path = run_publication_path(RUN_ID, root)
            first_bytes = publication_path.read_bytes()

            with self.assertRaisesRegex(
                RunCoordinationConflictError, "was not adopted"
            ):
                register_run_publication(
                    RUN_ID,
                    run_path,
                    publication_id="PUBLICATION-2",
                    recorded_by="another-operator",
                    now=FIXED_NOW,
                    coordination_root=root,
                )

            self.assertEqual(RunPublication.from_path(publication_path), first)
            self.assertEqual(publication_path.read_bytes(), first_bytes)

    def test_invalid_or_wrong_run_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            malformed = base / "run.json"
            malformed.write_text("{}\n", encoding="utf-8")
            root = base / "coordination"

            with self.assertRaises(RunCoordinationMalformedError):
                register(malformed, root)
            self.assertFalse(root.exists())

            malformed.unlink()
            other = write_run(base, "RUN-OTHER")
            with self.assertRaisesRegex(
                RunCoordinationConflictError, "belongs to RUN-OTHER"
            ):
                register(other, root)
            self.assertFalse(root.exists())

    def test_partial_registration_requires_inspection_and_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"

            with patch(
                "software_factory.run_coordination.publish_immutable_json",
                side_effect=RunCoordinationPersistenceError("injected failure"),
            ):
                with self.assertRaisesRegex(
                    RunCoordinationPublicationError, "inspect it before retrying"
                ):
                    register(run_path, root)

            run_directory = run_coordination_path(RUN_ID, root)
            self.assertTrue(run_directory.is_dir())
            self.assertFalse((run_directory / "publication.json").exists())
            with self.assertRaises(RunCoordinationConflictError):
                register(run_path, root)


class CanonicalRunTests(unittest.TestCase):
    def test_registered_entry_returns_exact_record_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            exact = run_path.read_bytes()
            publication = register(run_path, base / "coordination")

            record, observed = require_canonical_run(publication, run_path)

            self.assertEqual(record, initialized_record())
            self.assertEqual(observed, exact)

    def test_changed_initialized_bytes_are_rejected_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            run_path.write_bytes(run_path.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                RunCoordinationConflictError,
                "canonical initialized run bytes no longer match the version registered",
            ):
                require_canonical_run(publication, run_path)

    def test_copy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            copied = base / "copied.json"
            copied.write_bytes(run_path.read_bytes())

            with self.assertRaisesRegex(
                RunCoordinationConflictError, "not the registered canonical entry"
            ):
                require_canonical_run(publication, copied)

    def test_symbolic_link_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            alias = base / "alias.json"
            alias.symlink_to(run_path)

            with self.assertRaisesRegex(
                RunCoordinationConflictError, "symbolic-link alias"
            ):
                require_canonical_run(publication, alias)

    def test_hard_link_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            alias = base / "alias.json"
            os.link(run_path, alias)

            with self.assertRaisesRegex(
                RunCoordinationConflictError, "exactly one hard link"
            ):
                require_canonical_run(publication, alias)
            with self.assertRaisesRegex(
                RunCoordinationConflictError, "exactly one hard link"
            ):
                require_canonical_run(publication, run_path)

    def test_renamed_or_missing_canonical_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            run_path.rename(base / "moved.json")

            with self.assertRaisesRegex(
                RunCoordinationConflictError, "cannot inspect supplied run"
            ):
                require_canonical_run(publication, run_path)

    def test_nonregular_entry_and_malformed_current_run_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            directory_entry = base / "run.json"
            directory_entry.mkdir()
            with self.assertRaisesRegex(
                RunCoordinationConflictError, "not a regular file"
            ):
                register(directory_entry, base / "coordination")

            directory_entry.rmdir()
            run_path = write_run(base)
            publication = register(run_path, base / "coordination")
            run_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RunCoordinationMalformedError, "violates the run schema"
            ):
                require_canonical_run(publication, run_path)


class TransactionTests(unittest.TestCase):
    def test_permanent_lock_serializes_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"
            register(run_path, root)
            context = multiprocessing.get_context("spawn")
            messages = context.Queue()
            process = context.Process(
                target=lock_worker, args=(RUN_ID, str(root), messages)
            )

            with run_transaction(RUN_ID, root):
                process.start()
                self.assertEqual(messages.get(timeout=3), "attempting")
                with self.assertRaises(queue.Empty):
                    messages.get(timeout=0.2)
            self.assertEqual(messages.get(timeout=3), "acquired")
            process.join(timeout=3)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(run_transaction_lock_path(RUN_ID, root).is_file())


class CanonicalReplacementTests(unittest.TestCase):
    def test_replacement_succeeds_preserves_mode_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            os.chmod(run_path, 0o640)
            root = base / "coordination"
            publication = register(run_path, root)
            new_content = changed_run_content(initialized_record())

            with run_transaction(RUN_ID, root):
                replaced = replace_canonical_run(
                    publication,
                    publication.initialized_record_digest,
                    new_content,
                )
            with run_transaction(RUN_ID, root):
                retried = replace_canonical_run(
                    publication,
                    publication.initialized_record_digest,
                    new_content,
                )

            self.assertEqual(replaced.current_state, "active")
            self.assertEqual(retried, replaced)
            self.assertEqual(run_path.read_bytes(), new_content)
            self.assertEqual(stat.S_IMODE(run_path.stat().st_mode), 0o640)

    def test_stale_expected_digest_cannot_replace_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"
            publication = register(run_path, root)
            active_content = changed_run_content(initialized_record(), "active")
            waiting_content = changed_run_content(initialized_record(), "waiting")
            with run_transaction(RUN_ID, root):
                replace_canonical_run(
                    publication,
                    publication.initialized_record_digest,
                    active_content,
                )

            with run_transaction(RUN_ID, root):
                with self.assertRaisesRegex(
                    RunCoordinationConflictError, "not expected digest"
                ):
                    replace_canonical_run(
                        publication,
                        publication.initialized_record_digest,
                        waiting_content,
                    )

            self.assertEqual(run_path.read_bytes(), active_content)

    def test_replacement_for_another_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"
            publication = register(run_path, root)
            other_content = changed_run_content(initialized_record("RUN-OTHER"))

            with run_transaction(RUN_ID, root):
                with self.assertRaisesRegex(
                    RunCoordinationConflictError, "belongs to RUN-OTHER"
                ):
                    replace_canonical_run(
                        publication,
                        publication.initialized_record_digest,
                        other_content,
                    )

            self.assertEqual(
                f"sha256:{hashlib.sha256(run_path.read_bytes()).hexdigest()}",
                publication.initialized_record_digest,
            )

    def test_replace_directory_fsync_uncertainty_requires_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_path = write_run(base)
            root = base / "coordination"
            publication = register(run_path, root)
            new_content = changed_run_content(initialized_record())

            with run_transaction(RUN_ID, root):
                with patch(
                    "software_factory.run_coordination._fsync_directory",
                    side_effect=OSError("injected fsync failure"),
                ):
                    with self.assertRaisesRegex(
                        RunCoordinationPublicationError,
                        "replacement is visible.*durability could not be confirmed",
                    ):
                        replace_canonical_run(
                            publication,
                            publication.initialized_record_digest,
                            new_content,
                        )

            self.assertEqual(run_path.read_bytes(), new_content)
            self.assertEqual(RunRecord.from_path(run_path).current_state, "active")


if __name__ == "__main__":
    unittest.main()
