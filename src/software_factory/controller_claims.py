from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .json_records import DuplicateJsonMemberError, parse_json
from .runs import RunRecord


CLAIM_EVENT_KINDS = frozenset({"acquired", "transferred", "recovered"})
_CHANGE_KINDS = frozenset({"transferred", "recovered"})
_EVENT_NAME_PATTERN = re.compile(r"(\d{6})\.json\Z")
_MAX_EVENT_SEQUENCE = 999_999
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class ControllerClaimError(ValueError):
    """A controller claim is unreadable or violates the protocol."""


class ControllerClaimConflictError(ControllerClaimError):
    """Current controller ownership prevents the requested change."""


class ControllerClaimPersistenceError(OSError):
    """A controller-claim event was not published."""


class ControllerClaimPublicationError(OSError):
    """A claim event exists, but its durable publication requires inspection."""


@dataclass(frozen=True)
class ControllerClaimEvent:
    schema_version: int
    sequence: int
    kind: str
    at: str
    run_id: str
    publication_id: str | None
    claim_id: str
    controller_id: str
    previous_claim_id: str | None
    reason: str
    recorded_by: str

    @classmethod
    def from_mapping(cls, raw: object, *, field: str = "claim event") -> ControllerClaimEvent:
        data = _mapping(raw, field)
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2}:
            raise ControllerClaimError(
                f"{field}.schema_version must be the integer 1 or 2"
            )
        expected_fields = {
            "schema_version",
            "sequence",
            "kind",
            "at",
            "run_id",
            "claim_id",
            "controller_id",
            "previous_claim_id",
            "reason",
            "recorded_by",
        }
        if schema_version == 2:
            expected_fields.add("publication_id")
        _require_fields(data, expected_fields, field)
        sequence = data.get("sequence")
        if type(sequence) is not int or not 0 <= sequence <= _MAX_EVENT_SEQUENCE:
            raise ControllerClaimError(
                f"{field}.sequence must be an integer from 0 through {_MAX_EVENT_SEQUENCE}"
            )
        kind = _choice(data.get("kind"), f"{field}.kind", CLAIM_EVENT_KINDS)
        previous_value = data.get("previous_claim_id")
        if previous_value is not None and not isinstance(previous_value, str):
            raise ControllerClaimError(
                f"{field}.previous_claim_id must be a non-empty string or null"
            )
        previous_claim_id = (
            _text(previous_value, f"{field}.previous_claim_id")
            if previous_value is not None
            else None
        )
        return cls(
            schema_version=schema_version,
            sequence=sequence,
            kind=kind,
            at=_timestamp(data.get("at"), f"{field}.at"),
            run_id=_text(data.get("run_id"), f"{field}.run_id"),
            publication_id=(
                _text(data.get("publication_id"), f"{field}.publication_id")
                if schema_version == 2
                else None
            ),
            claim_id=_text(data.get("claim_id"), f"{field}.claim_id"),
            controller_id=_text(data.get("controller_id"), f"{field}.controller_id"),
            previous_claim_id=previous_claim_id,
            reason=_text(data.get("reason"), f"{field}.reason"),
            recorded_by=_text(data.get("recorded_by"), f"{field}.recorded_by"),
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "kind": self.kind,
            "at": self.at,
            "run_id": self.run_id,
            "claim_id": self.claim_id,
            "controller_id": self.controller_id,
            "previous_claim_id": self.previous_claim_id,
            "reason": self.reason,
            "recorded_by": self.recorded_by,
        }
        if self.schema_version == 2:
            result["publication_id"] = self.publication_id
        return result


@dataclass(frozen=True)
class ControllerClaimHistory:
    events: tuple[ControllerClaimEvent, ...]

    def __post_init__(self) -> None:
        _validate_history(self.events)

    @property
    def run_id(self) -> str:
        return self.events[0].run_id

    @property
    def current(self) -> ControllerClaimEvent:
        return self.events[-1]

    @classmethod
    def from_path(cls, path: str | Path) -> ControllerClaimHistory:
        claim_path = Path(path)
        try:
            claim_status = os.lstat(claim_path)
        except OSError as error:
            raise ControllerClaimError(
                f"cannot inspect controller claim {claim_path}: {error}"
            ) from error
        if stat.S_ISLNK(claim_status.st_mode) or not stat.S_ISDIR(
            claim_status.st_mode
        ):
            raise ControllerClaimError(
                f"controller claim {claim_path} must be a non-symlink directory"
            )
        try:
            entries = sorted(claim_path.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ControllerClaimError(
                f"cannot read controller claim {claim_path}: {error}"
            ) from error

        event_paths: list[Path] = []
        unexpected: list[str] = []
        for entry in entries:
            match = _EVENT_NAME_PATTERN.fullmatch(entry.name)
            if match is None or not entry.is_file():
                unexpected.append(entry.name)
            else:
                event_paths.append(entry)
        if unexpected:
            raise ControllerClaimError(
                f"controller claim {claim_path} contains entries requiring inspection: "
                f"{', '.join(unexpected)}"
            )
        if not event_paths:
            raise ControllerClaimError(
                f"controller claim {claim_path} contains no ownership events"
            )

        events: list[ControllerClaimEvent] = []
        for expected_sequence, event_path in enumerate(event_paths):
            expected_name = _event_name(expected_sequence)
            if event_path.name != expected_name:
                raise ControllerClaimError(
                    f"controller claim event sequence is incomplete: expected {expected_name}, "
                    f"found {event_path.name}"
                )
            events.append(_read_event(event_path))
        return cls(events=tuple(events))


def read_controller_claim_with_current_bytes(
    path: str | Path,
) -> tuple[ControllerClaimHistory, bytes]:
    """Read one stable claim history and the exact bytes of its current event."""

    claim_path = Path(path)
    first = ControllerClaimHistory.from_path(claim_path)
    event_path = claim_path / _event_name(first.current.sequence)
    current, content = _read_event_with_bytes(event_path)
    if current != first.current:
        raise ControllerClaimError(
            f"controller claim {claim_path} changed while its current event was read"
        )
    second = ControllerClaimHistory.from_path(claim_path)
    if second != first:
        raise ControllerClaimError(
            f"controller claim {claim_path} changed while it was read"
        )
    return first, content


def default_controller_claim_root() -> Path:
    try:
        import pwd
    except ImportError as error:
        raise ControllerClaimPersistenceError(
            "the local controller-claim store requires a POSIX account database"
        ) from error
    if not hasattr(os, "getuid"):
        raise ControllerClaimPersistenceError(
            "the local controller-claim store requires a stable POSIX user ID"
        )
    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError as error:
        raise ControllerClaimPersistenceError(
            f"no POSIX account entry exists for user ID {os.getuid()}"
        ) from error
    return account_home / ".software-factory" / "controller-claims"


def controller_claim_path(run_id: str, claim_root: str | Path) -> Path:
    identity = _text(run_id, "run_id")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(claim_root) / f"run-{digest}.controller-claim"


def ensure_controller_claim_root(path: str | Path) -> Path:
    claim_root = Path(path)
    missing: list[Path] = []
    cursor = claim_root
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            raise ControllerClaimPersistenceError(
                f"cannot find an existing parent for controller claim root {claim_root}"
            )
        cursor = cursor.parent
    if not cursor.is_dir():
        raise ControllerClaimPersistenceError(
            f"controller claim root ancestor is not a directory: {cursor}"
        )

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise ControllerClaimPersistenceError(
                    f"controller claim root path is not a directory: {directory}"
                )
        except OSError as error:
            raise ControllerClaimPersistenceError(
                f"cannot create controller claim root {directory}: {error}"
            ) from error
        try:
            _fsync_directory(directory.parent)
        except OSError as error:
            raise ControllerClaimPublicationError(
                f"controller claim root {directory} exists, but durable publication "
                f"could not be confirmed: {error}; inspect it before retrying"
            ) from error
    return claim_root


def require_claim_for_run(
    run: RunRecord, history: ControllerClaimHistory
) -> ControllerClaimHistory:
    if history.run_id != run.id:
        raise ControllerClaimError(
            f"controller claim belongs to run {history.run_id}, not {run.id}"
        )
    return history


def require_claim_for_publication(
    history: ControllerClaimHistory, publication_id: str
) -> ControllerClaimHistory:
    expected = _text(publication_id, "publication_id")
    if history.current.publication_id is None:
        raise ControllerClaimError(
            "legacy controller claim has no canonical run-publication binding; "
            "it may be inspected but cannot authorize activation"
        )
    if history.current.publication_id != expected:
        raise ControllerClaimError(
            f"controller claim belongs to publication {history.current.publication_id}, "
            f"not {expected}"
        )
    return history


def load_controller_claim(
    run_path: str | Path,
    *,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> ControllerClaimHistory:
    from .run_coordination import (
        RunPublication,
        default_run_coordination_root,
        require_canonical_run,
        run_publication_path,
        run_transaction,
    )

    supplied = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied.id, resolved_coordination_root):
        publication = RunPublication.from_path(
            run_publication_path(supplied.id, resolved_coordination_root)
        )
        run, _ = require_canonical_run(publication, run_path)
        history = require_claim_for_run(
            run,
            ControllerClaimHistory.from_path(
                controller_claim_path(run.id, resolved_claim_root)
            ),
        )
        if history.current.publication_id is not None:
            require_claim_for_publication(history, publication.id)
        return history


def acquire_controller_claim(
    run_path: str | Path,
    *,
    publication_id: str | None = None,
    claim_id: str,
    controller_id: str,
    recorded_by: str,
    now: datetime,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> tuple[ControllerClaimHistory, Path]:
    from .run_coordination import (
        RunPublication,
        default_run_coordination_root,
        require_canonical_run,
        run_publication_path,
        run_transaction,
    )

    supplied = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied.id, resolved_coordination_root):
        publication = RunPublication.from_path(
            run_publication_path(supplied.id, resolved_coordination_root)
        )
        if publication_id is not None and publication.id != _text(
            publication_id, "publication_id"
        ):
            raise ControllerClaimConflictError(
                f"current run publication is {publication.id}, not expected "
                f"publication {publication_id}"
            )
        run, _ = require_canonical_run(publication, run_path)
        history = create_controller_claim(
            run,
            publication_id=publication.id,
            claim_id=claim_id,
            controller_id=controller_id,
            recorded_by=recorded_by,
            now=now,
        )
        root = ensure_controller_claim_root(resolved_claim_root)
        path = controller_claim_path(run.id, root)
        persist_initial_controller_claim(history, path)
        return history, path


def change_controller_claim(
    run_path: str | Path,
    *,
    kind: str,
    expected_claim_id: str,
    claim_id: str,
    controller_id: str,
    reason: str,
    recorded_by: str,
    now: datetime,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
) -> tuple[ControllerClaimEvent, ControllerClaimHistory, Path]:
    from .run_coordination import (
        RunPublication,
        default_run_coordination_root,
        require_canonical_run,
        run_publication_path,
        run_transaction,
    )

    supplied = RunRecord.from_path(run_path)
    resolved_claim_root = (
        default_controller_claim_root() if claim_root is None else Path(claim_root)
    )
    resolved_coordination_root = (
        default_run_coordination_root()
        if coordination_root is None
        else Path(coordination_root)
    )
    with run_transaction(supplied.id, resolved_coordination_root):
        publication = RunPublication.from_path(
            run_publication_path(supplied.id, resolved_coordination_root)
        )
        run, _ = require_canonical_run(publication, run_path)
        path = controller_claim_path(run.id, resolved_claim_root)
        prior = require_claim_for_run(run, ControllerClaimHistory.from_path(path))
        if prior.current.publication_id is not None:
            require_claim_for_publication(prior, publication.id)
        event = create_controller_claim_change(
            prior,
            kind=kind,
            expected_claim_id=expected_claim_id,
            claim_id=claim_id,
            controller_id=controller_id,
            reason=reason,
            recorded_by=recorded_by,
            now=now,
        )
        history = persist_controller_claim_change(event, path)
        return event, history, path


def create_controller_claim(
    run: RunRecord,
    *,
    publication_id: str,
    claim_id: str,
    controller_id: str,
    recorded_by: str,
    now: datetime,
) -> ControllerClaimHistory:
    if run.current_state != "initialized":
        raise ControllerClaimConflictError(
            f"run {run.id} is {run.current_state}; initial controller acquisition requires initialized"
        )
    event = ControllerClaimEvent(
        schema_version=2,
        sequence=0,
        kind="acquired",
        at=_format_utc(now),
        run_id=run.id,
        publication_id=_text(publication_id, "publication_id"),
        claim_id=_text(claim_id, "claim_id"),
        controller_id=_text(controller_id, "controller_id"),
        previous_claim_id=None,
        reason="initial-acquisition",
        recorded_by=_text(recorded_by, "recorded_by"),
    )
    return ControllerClaimHistory(events=(event,))


def persist_initial_controller_claim(
    history: ControllerClaimHistory, path: str | Path
) -> None:
    if len(history.events) != 1 or history.current.kind != "acquired":
        raise ControllerClaimError("initial persistence requires exactly one acquired event")
    claim_path = Path(path)
    parent = claim_path.parent
    if not parent.is_dir():
        raise ControllerClaimPersistenceError(
            f"controller claim directory does not exist: {parent}"
        )
    try:
        claim_path.mkdir()
    except FileExistsError as error:
        raise ControllerClaimConflictError(
            f"controller claim already exists: {claim_path}"
        ) from error
    except OSError as error:
        raise ControllerClaimPersistenceError(
            f"cannot create controller claim {claim_path}: {error}"
        ) from error

    try:
        _publish_event(history.current, claim_path)
        _fsync_directory(parent)
    except (ControllerClaimConflictError, ControllerClaimPersistenceError) as error:
        raise ControllerClaimPublicationError(
            f"controller claim directory {claim_path} exists but its initial ownership "
            f"event was not cleanly published: {error}; inspect it before retrying"
        ) from error
    except ControllerClaimPublicationError:
        raise
    except OSError as error:
        raise ControllerClaimPublicationError(
            f"controller claim {claim_path} exists, but durable directory publication "
            f"could not be confirmed: {error}; inspect it before retrying"
        ) from error


def create_controller_claim_change(
    history: ControllerClaimHistory,
    *,
    kind: str,
    expected_claim_id: str,
    claim_id: str,
    controller_id: str,
    reason: str,
    recorded_by: str,
    now: datetime,
) -> ControllerClaimEvent:
    change_kind = _choice(kind, "kind", _CHANGE_KINDS)
    expected = _text(expected_claim_id, "expected_claim_id")
    if history.current.claim_id != expected:
        raise ControllerClaimConflictError(
            f"current claim is {history.current.claim_id}, not expected claim {expected}"
        )
    new_claim_id = _text(claim_id, "claim_id")
    if any(event.claim_id == new_claim_id for event in history.events):
        raise ControllerClaimError(f"claim_id has already been used: {new_claim_id}")
    if len(history.events) > _MAX_EVENT_SEQUENCE:
        raise ControllerClaimError(
            f"controller claim history has reached its {_MAX_EVENT_SEQUENCE + 1}-event limit"
        )
    event = ControllerClaimEvent(
        schema_version=history.current.schema_version,
        sequence=len(history.events),
        kind=change_kind,
        at=_format_utc(now),
        run_id=history.run_id,
        publication_id=history.current.publication_id,
        claim_id=new_claim_id,
        controller_id=_text(controller_id, "controller_id"),
        previous_claim_id=history.current.claim_id,
        reason=_text(reason, "reason"),
        recorded_by=_text(recorded_by, "recorded_by"),
    )
    _validate_history(history.events + (event,))
    return event


def persist_controller_claim_change(
    event: ControllerClaimEvent, path: str | Path
) -> ControllerClaimHistory:
    claim_path = Path(path)
    current = ControllerClaimHistory.from_path(claim_path)
    if event.run_id != current.run_id:
        raise ControllerClaimConflictError(
            f"claim change names run {event.run_id}, but current claim belongs to {current.run_id}"
        )
    if event.sequence != len(current.events) or event.previous_claim_id != current.current.claim_id:
        raise ControllerClaimConflictError(
            f"controller ownership changed before this {event.kind} event could be published; "
            f"current claim is {current.current.claim_id}"
        )
    _validate_history(current.events + (event,))
    _publish_event(event, claim_path)
    try:
        return ControllerClaimHistory.from_path(claim_path)
    except ControllerClaimError as error:
        raise ControllerClaimPublicationError(
            f"claim event {_event_name(event.sequence)} was durably published in "
            f"{claim_path}, but current ownership could not be read back: {error}; "
            f"inspect the claim before continuing"
        ) from error


def _read_event(path: Path) -> ControllerClaimEvent:
    event, _ = _read_event_with_bytes(path)
    return event


def _read_event_with_bytes(path: Path) -> tuple[ControllerClaimEvent, bytes]:
    from .run_coordination import read_stable_file

    raw_bytes, _ = read_stable_file(path, ControllerClaimError, "claim event")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ControllerClaimError(
            f"claim event {path} is not valid UTF-8: byte {error.start}"
        ) from error
    try:
        raw = parse_json(text)
    except json.JSONDecodeError as error:
        raise ControllerClaimError(
            f"claim event {path} is not valid JSON: line {error.lineno}, column {error.colno}"
        ) from error
    except DuplicateJsonMemberError as error:
        raise ControllerClaimError(f"claim event {path} is ambiguous: {error}") from error
    return (
        ControllerClaimEvent.from_mapping(raw, field=f"claim event {path.name}"),
        raw_bytes,
    )


def _validate_history(events: tuple[ControllerClaimEvent, ...]) -> None:
    if not isinstance(events, tuple) or not events:
        raise ControllerClaimError("controller claim events must be a non-empty tuple")
    first = events[0]
    run_id = _text(first.run_id, "events[0].run_id")
    if first.schema_version == 2:
        publication_id = _text(first.publication_id, "events[0].publication_id")
    else:
        if first.publication_id is not None:
            raise ControllerClaimError(
                "events[0].publication_id must be absent from schema version 1"
            )
        publication_id = None
    seen_claim_ids: set[str] = set()
    previous: ControllerClaimEvent | None = None
    for index, event in enumerate(events):
        if not isinstance(event, ControllerClaimEvent):
            raise ControllerClaimError(f"events[{index}] must be a ControllerClaimEvent")
        field = f"events[{index}]"
        if type(event.schema_version) is not int or event.schema_version not in {1, 2}:
            raise ControllerClaimError(
                f"{field}.schema_version must be the integer 1 or 2"
            )
        if event.schema_version != first.schema_version:
            raise ControllerClaimError(
                f"{field}.schema_version must remain {first.schema_version}"
            )
        if index > _MAX_EVENT_SEQUENCE:
            raise ControllerClaimError(
                f"controller claim history exceeds its {_MAX_EVENT_SEQUENCE + 1}-event limit"
            )
        if type(event.sequence) is not int or event.sequence != index:
            raise ControllerClaimError(f"{field}.sequence must be the integer {index}")
        _choice(event.kind, f"{field}.kind", CLAIM_EVENT_KINDS)
        _timestamp(event.at, f"{field}.at")
        if _text(event.run_id, f"{field}.run_id") != run_id:
            raise ControllerClaimError(f"{field}.run_id must remain {run_id}")
        if publication_id is None:
            if event.publication_id is not None:
                raise ControllerClaimError(
                    f"{field}.publication_id must remain absent for schema version 1"
                )
        elif _text(event.publication_id, f"{field}.publication_id") != publication_id:
            raise ControllerClaimError(
                f"{field}.publication_id must remain {publication_id}"
            )
        claim_id = _text(event.claim_id, f"{field}.claim_id")
        if claim_id in seen_claim_ids:
            raise ControllerClaimError(f"claim_id is repeated: {claim_id}")
        seen_claim_ids.add(claim_id)
        _text(event.controller_id, f"{field}.controller_id")
        _text(event.reason, f"{field}.reason")
        _text(event.recorded_by, f"{field}.recorded_by")

        if previous is None:
            if event.kind != "acquired" or event.previous_claim_id is not None:
                raise ControllerClaimError(
                    "the first controller claim event must be acquired with no previous claim"
                )
        else:
            if event.kind not in _CHANGE_KINDS:
                raise ControllerClaimError(
                    f"{field}.kind must be transferred or recovered"
                )
            if event.previous_claim_id != previous.claim_id:
                raise ControllerClaimError(
                    f"{field}.previous_claim_id must be {previous.claim_id}"
                )
            if event.at < previous.at:
                raise ControllerClaimError(f"{field}.at cannot predate the prior event")
        previous = event


def _publish_event(event: ControllerClaimEvent, claim_path: Path) -> None:
    destination = claim_path / _event_name(event.sequence)
    content = (json.dumps(event.to_mapping(), indent=2) + "\n").encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=claim_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            _ = temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot stage controller claim event {destination}: {error}"
        if cleanup_error is not None and temporary_path is not None:
            message += f"; temporary event {temporary_path} could not be removed: {cleanup_error}"
        raise ControllerClaimPersistenceError(message) from error

    try:
        os.link(temporary_path, destination)
    except FileExistsError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"controller claim event already exists: {destination}"
        if cleanup_error is not None:
            message += f"; temporary event {temporary_path} could not be removed: {cleanup_error}"
        raise ControllerClaimConflictError(message) from error
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot publish controller claim event {destination}: {error}"
        if cleanup_error is not None:
            message += f"; temporary event {temporary_path} could not be removed: {cleanup_error}"
        raise ControllerClaimPersistenceError(message) from error

    cleanup_error = _remove_temporary(temporary_path)
    if cleanup_error is not None:
        try:
            _fsync_directory(claim_path)
        except OSError as sync_error:
            raise ControllerClaimPublicationError(
                f"claim event {destination} exists, but temporary event {temporary_path} "
                f"could not be removed ({cleanup_error}) and durable publication could not "
                f"be confirmed ({sync_error}); inspect both paths"
            ) from sync_error
        raise ControllerClaimPublicationError(
            f"durable claim event {destination} exists, but temporary event "
            f"{temporary_path} could not be removed: {cleanup_error}; inspect both paths"
        ) from cleanup_error

    try:
        _fsync_directory(claim_path)
    except OSError as error:
        raise ControllerClaimPublicationError(
            f"claim event {destination} exists, but durable publication could not be "
            f"confirmed: {error}; inspect it before retrying"
        ) from error


def _event_name(sequence: int) -> str:
    if type(sequence) is not int or not 0 <= sequence <= _MAX_EVENT_SEQUENCE:
        raise ControllerClaimError(
            f"claim event sequence must be an integer from 0 through {_MAX_EVENT_SEQUENCE}"
        )
    return f"{sequence:06d}.json"


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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ControllerClaimError(f"{field} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise ControllerClaimError(f"{field} keys must be strings")
    return cast(dict[str, object], value)


def _require_fields(data: dict[str, object], expected: set[str], field: str) -> None:
    missing = expected - set(data)
    if missing:
        raise ControllerClaimError(
            f"{field} is missing fields: {', '.join(sorted(missing))}"
        )
    unknown = set(data) - expected
    if unknown:
        raise ControllerClaimError(
            f"{field} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControllerClaimError(f"{field} must be a non-empty string")
    return value.strip()


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    text = _text(value, field)
    if text not in choices:
        raise ControllerClaimError(
            f"{field} must be one of: {', '.join(sorted(choices))}"
        )
    return text


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    if not _TIMESTAMP_PATTERN.fullmatch(text):
        raise ControllerClaimError(
            f"{field} must be UTC ISO-8601 with seconds, such as 2026-08-30T12:00:00Z"
        )
    try:
        _ = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControllerClaimError(f"{field} is not a valid timestamp") from error
    return text


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControllerClaimError("now must include a UTC offset")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
