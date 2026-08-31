from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Optional, Tuple, Union, cast

from .json_records import DuplicateJsonMemberError, parse_json
from .runs import RunError, RunRecord


_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_PUBLICATION_FIELDS = {
    "schema_version",
    "id",
    "run_id",
    "canonical_path",
    "initialized_record_digest",
    "registered_at",
    "recorded_by",
}


class RunCoordinationError(ValueError):
    """Local run-coordination state is invalid or unreadable."""


class RunCoordinationMalformedError(RunCoordinationError):
    """Local run-coordination state violates its schema or invariants."""


class RunCoordinationConflictError(RunCoordinationError):
    """Observed state conflicts with the requested coordination operation."""


class RunCoordinationPersistenceError(OSError):
    """The requested coordination publication definitely did not occur."""


class RunCoordinationPublicationError(OSError):
    """A visible publication or replacement requires inspection."""


@dataclass(frozen=True)
class RunPublication:
    schema_version: int
    id: str
    run_id: str
    canonical_path: str
    initialized_record_digest: str
    registered_at: str
    recorded_by: str

    @classmethod
    def from_path(cls, path: Union[str, Path]) -> "RunPublication":
        publication_path = Path(path)
        raw_bytes, _ = _read_single_link_regular_file(
            publication_path,
            RunCoordinationMalformedError,
            "run publication",
        )
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunCoordinationMalformedError(
                f"run publication {publication_path} is not valid UTF-8: byte {error.start}"
            ) from error
        try:
            raw = parse_json(text)
        except json.JSONDecodeError as error:
            raise RunCoordinationMalformedError(
                f"run publication {publication_path} is not valid JSON: "
                f"line {error.lineno}, column {error.colno}"
            ) from error
        except DuplicateJsonMemberError as error:
            raise RunCoordinationMalformedError(
                f"run publication {publication_path} is ambiguous: {error}"
            ) from error
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> "RunPublication":
        data = _mapping(raw, "run publication")
        _require_fields(data, _PUBLICATION_FIELDS, "run publication")
        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise RunCoordinationMalformedError(
                "run publication.schema_version must be the integer 1"
            )
        digest = _digest(
            data.get("initialized_record_digest"),
            "run publication.initialized_record_digest",
        )
        return cls(
            schema_version=schema_version,
            id=_text(data.get("id"), "run publication.id"),
            run_id=_text(data.get("run_id"), "run publication.run_id"),
            canonical_path=_absolute_canonical_path_text(
                data.get("canonical_path"), "run publication.canonical_path"
            ),
            initialized_record_digest=digest,
            registered_at=_timestamp(
                data.get("registered_at"), "run publication.registered_at"
            ),
            recorded_by=_text(
                data.get("recorded_by"), "run publication.recorded_by"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "run_id": self.run_id,
            "canonical_path": self.canonical_path,
            "initialized_record_digest": self.initialized_record_digest,
            "registered_at": self.registered_at,
            "recorded_by": self.recorded_by,
        }


def default_run_coordination_root() -> Path:
    if not hasattr(os, "getuid"):
        raise RunCoordinationPersistenceError(
            "the local run-coordination store requires a stable POSIX user ID"
        )
    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid)
    except (KeyError, OSError) as error:
        raise RunCoordinationPersistenceError(
            f"no POSIX account entry is available for user ID {uid}"
        ) from error
    account_home_value = getattr(account, "pw_dir", None)
    if not isinstance(account_home_value, str) or not account_home_value.strip():
        raise RunCoordinationPersistenceError(
            f"the POSIX account entry for user ID {uid} has no home directory"
        )
    account_home = Path(account_home_value)
    if not account_home.is_absolute():
        raise RunCoordinationPersistenceError(
            f"the POSIX account home for user ID {uid} is not absolute: {account_home}"
        )
    return account_home / ".software-factory" / "run-coordination"


def default_coordination_root() -> Path:
    """Compatibility spelling for callers that already name the domain in context."""

    return default_run_coordination_root()


def run_key(run_id: str) -> str:
    identity = _text(run_id, "run_id")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def run_coordination_path(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Path:
    root = _coordination_root(coordination_root)
    return root / f"run-{run_key(run_id)}"


def run_publication_path(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Path:
    return run_coordination_path(run_id, coordination_root) / "publication.json"


def run_transaction_lock_path(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Path:
    return run_coordination_path(run_id, coordination_root) / "transaction.lock"


def activation_attempts_directory(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Path:
    return run_coordination_path(run_id, coordination_root) / "activation-attempts"


def worker_ready_directory(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Path:
    return run_coordination_path(run_id, coordination_root) / "worker-ready"


def activation_attempt_path(
    run_id: str,
    attempt_id: str,
    coordination_root: Optional[Union[str, Path]] = None,
) -> Path:
    return activation_attempts_directory(run_id, coordination_root) / _opaque_filename(
        "attempt", attempt_id
    )


def worker_ready_path(
    run_id: str,
    attempt_id: str,
    coordination_root: Optional[Union[str, Path]] = None,
) -> Path:
    return worker_ready_directory(run_id, coordination_root) / _opaque_filename(
        "attempt", attempt_id
    )


def ensure_run_coordination_root(path: Union[str, Path]) -> Path:
    root = Path(path)
    missing: list[Path] = []
    cursor = root
    while True:
        try:
            status = os.lstat(cursor)
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RunCoordinationPersistenceError(
                    f"cannot find an existing parent for run-coordination root {root}"
                )
            cursor = cursor.parent
            continue
        except OSError as error:
            raise RunCoordinationPersistenceError(
                f"cannot inspect run-coordination root ancestor {cursor}: {error}"
            ) from error
        if not stat.S_ISDIR(status.st_mode):
            raise RunCoordinationPersistenceError(
                f"run-coordination root ancestor is not a directory: {cursor}"
            )
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700)
        except FileExistsError:
            try:
                status = os.lstat(directory)
            except OSError as error:
                raise RunCoordinationPersistenceError(
                    f"cannot inspect run-coordination root {directory}: {error}"
                ) from error
            if not stat.S_ISDIR(status.st_mode):
                raise RunCoordinationPersistenceError(
                    f"run-coordination root path is not a directory: {directory}"
                )
        except OSError as error:
            raise RunCoordinationPersistenceError(
                f"cannot create run-coordination root {directory}: {error}"
            ) from error
        try:
            _fsync_directory(directory.parent)
        except OSError as error:
            raise RunCoordinationPublicationError(
                f"run-coordination directory {directory} exists, but durable publication "
                f"could not be confirmed: {error}; inspect it before retrying"
            ) from error
    return root


def register_run_publication(
    run_id: str,
    supplied_path: Union[str, Path],
    *,
    publication_id: str,
    recorded_by: str,
    now: datetime,
    coordination_root: Optional[Union[str, Path]] = None,
) -> RunPublication:
    identity = _text(run_id, "run_id")
    canonical_path = _canonical_entry_for_registration(Path(supplied_path))
    content, _ = _read_single_link_regular_file(
        canonical_path,
        RunCoordinationMalformedError,
        "supplied run",
    )
    record = _parse_run_record(content, canonical_path)
    if record.id != identity:
        raise RunCoordinationConflictError(
            f"supplied run belongs to {record.id}, not requested run {identity}"
        )
    if record.current_state != "initialized":
        raise RunCoordinationConflictError(
            f"run publication registration requires initialized, found "
            f"{record.current_state}"
        )

    publication = RunPublication(
        schema_version=1,
        id=_text(publication_id, "publication_id"),
        run_id=identity,
        canonical_path=str(canonical_path),
        initialized_record_digest=_content_digest(content),
        registered_at=_format_utc(now),
        recorded_by=_text(recorded_by, "recorded_by"),
    )
    publication = RunPublication.from_mapping(publication.to_mapping())

    root = ensure_run_coordination_root(_coordination_root(coordination_root))
    run_directory = run_coordination_path(identity, root)
    try:
        os.mkdir(run_directory, 0o700)
        os.chmod(run_directory, 0o700)
    except FileExistsError as error:
        raise RunCoordinationConflictError(
            f"run publication namespace already exists: {run_directory}; it was not adopted"
        ) from error
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot create run publication namespace {run_directory}: {error}"
        ) from error

    try:
        _fsync_directory(root)
        _create_registration_directory(run_directory / "activation-attempts")
        _create_registration_directory(run_directory / "worker-ready")
        _create_transaction_lock(run_directory / "transaction.lock")
        publish_immutable_json(
            publication.to_mapping(), run_directory / "publication.json"
        )
    except RunCoordinationPublicationError:
        raise
    except (RunCoordinationError, RunCoordinationPersistenceError, OSError) as error:
        raise RunCoordinationPublicationError(
            f"run publication namespace {run_directory} exists, but registration did "
            f"not complete cleanly: {error}; inspect it before retrying"
        ) from error
    return publication


def require_canonical_run(
    publication: RunPublication, supplied_path: Union[str, Path]
) -> Tuple[RunRecord, bytes]:
    validated = _validated_publication(publication)
    supplied = Path(supplied_path)
    _require_regular_entry_shape(supplied, "supplied run")
    try:
        resolved_parent = supplied.parent.resolve(strict=True)
    except OSError as error:
        raise RunCoordinationConflictError(
            f"cannot resolve supplied run parent {supplied.parent}: {error}"
        ) from error
    supplied_entry = resolved_parent / supplied.name
    canonical = Path(validated.canonical_path)
    if supplied_entry != canonical:
        raise RunCoordinationConflictError(
            f"supplied run {supplied_entry} is not the registered canonical entry {canonical}"
        )

    content, _ = _read_single_link_regular_file(
        canonical,
        RunCoordinationConflictError,
        "canonical run",
    )
    record = _parse_run_record(content, canonical)
    if record.id != validated.run_id:
        raise RunCoordinationConflictError(
            f"canonical run belongs to {record.id}, not registered run {validated.run_id}"
        )
    if (
        record.current_state == "initialized"
        and _content_digest(content) != validated.initialized_record_digest
    ):
        raise RunCoordinationConflictError(
            "canonical initialized run bytes no longer match the version registered "
            "for this publication"
        )
    return record, content


@contextmanager
def run_transaction(
    run_id: str, coordination_root: Optional[Union[str, Path]] = None
) -> Iterator[None]:
    lock_path = run_transaction_lock_path(run_id, coordination_root)
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags)
    except OSError as error:
        raise RunCoordinationMalformedError(
            f"cannot open permanent run transaction lock {lock_path}: {error}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise RunCoordinationMalformedError(
                f"run transaction lock {lock_path} must be a regular single-link file"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as error:
            raise RunCoordinationPersistenceError(
                f"cannot acquire run transaction lock {lock_path}: {error}"
            ) from error
        try:
            yield None
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def publish_immutable_json(
    mapping: Mapping[str, object], destination: Union[str, Path]
) -> bytes:
    if not isinstance(mapping, Mapping):
        raise RunCoordinationMalformedError("immutable JSON content must be an object")
    try:
        content = (
            json.dumps(
                dict(mapping),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RunCoordinationMalformedError(
            f"immutable JSON content cannot be encoded strictly: {error}"
        ) from error

    destination_path = Path(destination)
    parent = destination_path.parent
    try:
        parent_status = os.lstat(parent)
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot inspect immutable publication directory {parent}: {error}"
        ) from error
    if not stat.S_ISDIR(parent_status.st_mode):
        raise RunCoordinationPersistenceError(
            f"immutable publication directory is not a directory: {parent}"
        )

    staging_directory = _immutable_staging_directory(destination_path)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=staging_directory,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            _ = temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot stage immutable JSON publication {destination_path}: {error}"
        if cleanup_error is not None and temporary_path is not None:
            message += (
                f"; temporary publication {temporary_path} could not be removed: "
                f"{cleanup_error}"
            )
        raise RunCoordinationPersistenceError(message) from error

    try:
        os.link(temporary_path, destination_path)
    except FileExistsError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"immutable JSON destination already exists: {destination_path}"
        if cleanup_error is not None:
            message += (
                f"; temporary publication {temporary_path} could not be removed: "
                f"{cleanup_error}"
            )
        raise RunCoordinationConflictError(message) from error
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot publish immutable JSON at {destination_path}: {error}"
        if cleanup_error is not None:
            message += (
                f"; temporary publication {temporary_path} could not be removed: "
                f"{cleanup_error}"
            )
        raise RunCoordinationPersistenceError(message) from error

    cleanup_error = _remove_temporary(temporary_path)
    if cleanup_error is not None:
        raise RunCoordinationPublicationError(
            f"immutable JSON {destination_path} exists, but temporary publication "
            f"{temporary_path} could not be removed: {cleanup_error}; inspect both paths"
        ) from cleanup_error
    try:
        _fsync_directory(parent)
        if staging_directory != parent:
            _fsync_directory(staging_directory)
    except OSError as error:
        raise RunCoordinationPublicationError(
            f"immutable JSON {destination_path} exists, but durable publication could "
            f"not be confirmed: {error}; inspect it before retrying"
        ) from error
    return content


def replace_canonical_run(
    publication: RunPublication, expected_digest: str, new_content: bytes
) -> RunRecord:
    validated = _validated_publication(publication)
    expected = _digest(expected_digest, "expected_digest")
    if not isinstance(new_content, bytes):
        raise RunCoordinationMalformedError("new_content must be bytes")
    canonical = Path(validated.canonical_path)
    new_record = _parse_run_record(new_content, canonical)
    if new_record.id != validated.run_id:
        raise RunCoordinationConflictError(
            f"replacement run belongs to {new_record.id}, not registered run {validated.run_id}"
        )

    current_record, current_content = require_canonical_run(validated, canonical)
    if current_content == new_content:
        try:
            _fsync_directory(canonical.parent)
        except OSError as error:
            raise RunCoordinationPublicationError(
                f"the requested run bytes are visible at {canonical}, but durable "
                f"replacement could not be confirmed: {error}; inspect it before retrying"
            ) from error
        return current_record
    current_digest = _content_digest(current_content)
    if current_digest != expected:
        raise RunCoordinationConflictError(
            f"canonical run digest is {current_digest}, not expected digest {expected}"
        )

    _, current_status = _read_single_link_regular_file(
        canonical,
        RunCoordinationConflictError,
        "canonical run",
    )
    mode = stat.S_IMODE(current_status.st_mode)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{canonical.name}.",
            suffix=".replacement.tmp",
            dir=canonical.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), mode)
            _ = temporary.write(new_content)
            temporary.flush()
            os.fsync(temporary.fileno())
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot stage canonical run replacement for {canonical}: {error}"
        if cleanup_error is not None and temporary_path is not None:
            message += (
                f"; temporary replacement {temporary_path} could not be removed: "
                f"{cleanup_error}"
            )
        raise RunCoordinationPersistenceError(message) from error

    try:
        staged_content = temporary_path.read_bytes()
    except OSError as error:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot reread staged canonical run replacement {temporary_path}: {error}"
        if cleanup_error is not None:
            message += f"; cleanup also failed: {cleanup_error}"
        raise RunCoordinationPersistenceError(message) from error
    if staged_content != new_content:
        cleanup_error = _remove_temporary(temporary_path)
        message = f"staged canonical run replacement {temporary_path} failed its exact-byte check"
        if cleanup_error is not None:
            message += f"; cleanup also failed: {cleanup_error}"
        raise RunCoordinationPersistenceError(message)

    try:
        final_record, final_content = require_canonical_run(validated, canonical)
    except RunCoordinationError:
        _ = _remove_temporary(temporary_path)
        raise
    if final_content == new_content:
        cleanup_error = _remove_temporary(temporary_path)
        if cleanup_error is not None:
            raise RunCoordinationPublicationError(
                f"the requested run bytes are already visible at {canonical}, but temporary "
                f"replacement {temporary_path} could not be removed: {cleanup_error}; inspect both paths"
            ) from cleanup_error
        try:
            _fsync_directory(canonical.parent)
        except OSError as error:
            raise RunCoordinationPublicationError(
                f"the requested run bytes are visible at {canonical}, but durable "
                f"replacement could not be confirmed: {error}; inspect it before retrying"
            ) from error
        return final_record
    final_digest = _content_digest(final_content)
    if final_digest != expected:
        cleanup_error = _remove_temporary(temporary_path)
        message = (
            f"canonical run changed before replacement: digest is {final_digest}, "
            f"not expected digest {expected}"
        )
        if cleanup_error is not None:
            message += f"; temporary replacement cleanup failed: {cleanup_error}"
        raise RunCoordinationConflictError(message)

    replaced = False
    try:
        os.replace(temporary_path, canonical)
        replaced = True
        visible_record, visible_content = require_canonical_run(validated, canonical)
        if visible_content != new_content:
            raise RunCoordinationPublicationError(
                f"replacement at {canonical} is visible but failed its exact-byte reread; "
                f"inspect it before continuing"
            )
        _fsync_directory(canonical.parent)
        return visible_record
    except RunCoordinationPublicationError:
        raise
    except RunCoordinationError as error:
        if replaced:
            raise RunCoordinationPublicationError(
                f"replacement is visible at {canonical}, but it could not be validated: "
                f"{error}; inspect it before continuing"
            ) from error
        _ = _remove_temporary(temporary_path)
        raise
    except OSError as error:
        if replaced:
            raise RunCoordinationPublicationError(
                f"replacement is visible at {canonical}, but durability could not be "
                f"confirmed: {error}; inspect it before retrying"
            ) from error
        cleanup_error = _remove_temporary(temporary_path)
        message = f"cannot replace canonical run {canonical}: {error}"
        if cleanup_error is not None:
            message += (
                f"; temporary replacement {temporary_path} could not be removed: "
                f"{cleanup_error}"
            )
        raise RunCoordinationPersistenceError(message) from error


def _coordination_root(value: Optional[Union[str, Path]]) -> Path:
    return default_run_coordination_root() if value is None else Path(value)


def _opaque_filename(kind: str, identity: str) -> str:
    value = _text(identity, f"{kind}_id")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{kind}-{digest}.json"


def _validated_publication(publication: RunPublication) -> RunPublication:
    if not isinstance(publication, RunPublication):
        raise RunCoordinationMalformedError(
            "publication must be a RunPublication"
        )
    return RunPublication.from_mapping(publication.to_mapping())


def _canonical_entry_for_registration(supplied: Path) -> Path:
    initial_status = _require_regular_entry_shape(supplied, "supplied run")
    try:
        resolved_parent = supplied.parent.resolve(strict=True)
    except OSError as error:
        raise RunCoordinationMalformedError(
            f"cannot resolve supplied run parent {supplied.parent}: {error}"
        ) from error
    canonical = resolved_parent / supplied.name
    try:
        canonical_status = os.lstat(canonical)
    except OSError as error:
        raise RunCoordinationMalformedError(
            f"cannot inspect canonical run entry {canonical}: {error}"
        ) from error
    if (initial_status.st_dev, initial_status.st_ino) != (
        canonical_status.st_dev,
        canonical_status.st_ino,
    ):
        raise RunCoordinationConflictError(
            f"supplied run entry changed while resolving its canonical path: {supplied}"
        )
    return canonical


def _require_regular_entry_shape(path: Path, label: str) -> os.stat_result:
    try:
        status = os.lstat(path)
    except OSError as error:
        raise RunCoordinationConflictError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(status.st_mode):
        raise RunCoordinationConflictError(
            f"{label} {path} is a symbolic-link alias"
        )
    if not stat.S_ISREG(status.st_mode):
        raise RunCoordinationConflictError(
            f"{label} {path} is not a regular file"
        )
    if status.st_nlink != 1:
        raise RunCoordinationConflictError(
            f"{label} {path} must have exactly one hard link, found {status.st_nlink}"
        )
    return status


def read_stable_file(
    path: Union[str, Path],
    error_type: type[Exception],
    label: str,
) -> Tuple[bytes, os.stat_result]:
    """Read one exact regular-file version while rejecting links and replacements."""

    return _read_single_link_regular_file(Path(path), error_type, label)


def _read_single_link_regular_file(
    path: Path,
    error_type: type[Exception],
    label: str,
) -> Tuple[bytes, os.stat_result]:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise error_type(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise error_type(f"{label} {path} must not be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise error_type(f"{label} {path} must be a regular file")
    if before.st_nlink != 1:
        raise error_type(
            f"{label} {path} must have exactly one hard link, found {before.st_nlink}"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise error_type(f"cannot open {label} {path}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise error_type(f"{label} {path} is not a regular single-link file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise error_type(f"{label} {path} changed while it was opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
    except OSError as error:
        raise error_type(f"cannot read {label} {path}: {error}") from error
    finally:
        os.close(descriptor)

    try:
        after_path = os.lstat(path)
    except OSError as error:
        raise error_type(f"cannot reinspect {label} {path}: {error}") from error
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if any(getattr(opened, field) != getattr(after_read, field) for field in stable_fields):
        raise error_type(f"{label} {path} changed while it was read")
    if (after_read.st_dev, after_read.st_ino) != (
        after_path.st_dev,
        after_path.st_ino,
    ):
        raise error_type(f"{label} {path} was replaced while it was read")
    if not stat.S_ISREG(after_path.st_mode) or after_path.st_nlink != 1:
        raise error_type(f"{label} {path} is no longer a regular single-link file")
    return b"".join(chunks), after_path


def _parse_run_record(content: bytes, path: Path) -> RunRecord:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunCoordinationMalformedError(
            f"run {path} is not valid UTF-8: byte {error.start}"
        ) from error
    try:
        raw = parse_json(text)
    except json.JSONDecodeError as error:
        raise RunCoordinationMalformedError(
            f"run {path} is not valid JSON: line {error.lineno}, column {error.colno}"
        ) from error
    except DuplicateJsonMemberError as error:
        raise RunCoordinationMalformedError(
            f"run {path} is ambiguous: {error}"
        ) from error
    try:
        return RunRecord.from_mapping(raw)
    except RunError as error:
        raise RunCoordinationMalformedError(
            f"run {path} violates the run schema: {error}"
        ) from error


def _create_registration_directory(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
        _fsync_directory(path.parent)
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot create registration directory {path}: {error}"
        ) from error


def _create_transaction_lock(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot create permanent transaction lock {path}: {error}"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot durably create permanent transaction lock {path}: {error}"
        ) from error
    finally:
        os.close(descriptor)
    try:
        _fsync_directory(path.parent)
    except OSError as error:
        raise RunCoordinationPersistenceError(
            f"cannot confirm permanent transaction lock directory entry {path}: {error}"
        ) from error


def _immutable_staging_directory(destination: Path) -> Path:
    cursor = destination.parent
    while cursor.parent != cursor:
        if cursor.name.startswith("run-") and len(cursor.name) == 68:
            candidate = cursor.parent.parent
            if candidate.is_dir():
                return candidate
            break
        cursor = cursor.parent
    return destination.parent


def _remove_temporary(path: Optional[Path]) -> Optional[OSError]:
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
        raise RunCoordinationMalformedError(f"{field} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise RunCoordinationMalformedError(f"{field} keys must be strings")
    return cast(dict[str, object], value)


def _require_fields(data: dict[str, object], expected: set[str], field: str) -> None:
    missing = expected - set(data)
    if missing:
        raise RunCoordinationMalformedError(
            f"{field} is missing fields: {', '.join(sorted(missing))}"
        )
    unknown = set(data) - expected
    if unknown:
        raise RunCoordinationMalformedError(
            f"{field} contains unknown fields: {', '.join(sorted(unknown))}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunCoordinationMalformedError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: object, field: str) -> str:
    text = _text(value, field)
    if not _DIGEST_PATTERN.fullmatch(text):
        raise RunCoordinationMalformedError(
            f"{field} must be sha256 followed by 64 lowercase hexadecimal characters"
        )
    return text


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    if not _TIMESTAMP_PATTERN.fullmatch(text):
        raise RunCoordinationMalformedError(
            f"{field} must be UTC ISO-8601 with seconds, such as 2026-08-30T12:00:00Z"
        )
    try:
        _ = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RunCoordinationMalformedError(f"{field} is not a valid timestamp") from error
    return text


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunCoordinationMalformedError("now must include a UTC offset")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _absolute_canonical_path_text(value: object, field: str) -> str:
    text = _text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise RunCoordinationMalformedError(f"{field} must be an absolute path")
    if str(path) != text or ".." in path.parts:
        raise RunCoordinationMalformedError(
            f"{field} must name a normalized absolute directory entry"
        )
    return text


def _content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
