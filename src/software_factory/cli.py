from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .activation import (
    ActivationConflictError,
    ActivationError,
    ActivationAttempt,
    commit_activation,
    inspect_activation,
    prepare_activation_attempt,
    record_worker_ready,
)
from .controller_claims import (
    ControllerClaimConflictError,
    ControllerClaimError,
    ControllerClaimPersistenceError,
    ControllerClaimPublicationError,
    acquire_controller_claim,
    change_controller_claim,
    default_controller_claim_root,
    load_controller_claim,
)
from .preflight import ExecutionEnvironment, PreflightError, evaluate_preflight
from .presentation import (
    render_activation_attempt,
    render_activation_committed,
    render_activation_prepared,
    render_controller_claim,
    render_controller_claim_acquired,
    render_controller_claim_changed,
    render_preflight,
    render_readiness,
    render_run,
    render_run_initialized,
    render_work_packet,
    render_worker_ready_recorded,
)
from .readiness import evaluate_readiness
from .run_coordination import (
    RunCoordinationConflictError,
    RunCoordinationError,
    RunCoordinationPersistenceError,
    RunCoordinationPublicationError,
    activation_attempt_path,
    default_run_coordination_root,
    register_run_publication,
)
from .runs import (
    RunError,
    RunPersistenceError,
    RunPublicationError,
    RunRecord,
    initialize_run,
    persist_run,
)
from .work_packets import PacketError, WorkPacket


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory",
        description="Turn explicit intent and authority into verified outcomes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    packet = commands.add_parser("packet", help="Inspect versioned work packets.")
    packet_commands = packet.add_subparsers(dest="packet_command", required=True)

    validate = packet_commands.add_parser(
        "validate", help="Check whether a work packet is well formed."
    )
    validate.add_argument("path", help="Path to a work packet JSON file.")

    show = packet_commands.add_parser(
        "show", help="Explain a work packet and its authority in plain language."
    )
    show.add_argument("path", help="Path to a work packet JSON file.")

    readiness = packet_commands.add_parser(
        "readiness",
        help="Explain whether a valid work packet is ready to enter run preflight.",
    )
    readiness.add_argument("path", help="Path to a work packet JSON file.")

    run = commands.add_parser("run", help="Create and inspect durable factory runs.")
    run_commands = run.add_subparsers(dest="run_command", required=True)

    initialize = run_commands.add_parser(
        "init", help="Initialize a durable run from a ready work packet."
    )
    initialize.add_argument("packet_path", help="Path to a work packet JSON file.")
    initialize.add_argument("run_path", help="New path for the run JSON record.")
    initialize.add_argument(
        "--initiated-by",
        required=True,
        help="Identity recording the run initialization.",
    )

    run_show = run_commands.add_parser(
        "show", help="Explain a durable run record in plain language."
    )
    run_show.add_argument("path", help="Path to a run JSON record.")

    preflight = run_commands.add_parser(
        "preflight", help="Evaluate current execution facts without starting a worker."
    )
    preflight.add_argument("run_path", help="Path to an initialized run JSON record.")
    preflight.add_argument("packet_path", help="Path to the exact work packet JSON file.")
    preflight.add_argument(
        "environment_path", help="Path to a collected execution-environment snapshot."
    )
    preflight.add_argument(
        "--max-age-seconds",
        type=int,
        default=300,
        help="Maximum accepted environment snapshot age (default: 300).",
    )

    claim = run_commands.add_parser(
        "claim", help="Acquire and inspect exclusive controller ownership."
    )
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)

    claim_acquire = claim_commands.add_parser(
        "acquire", help="Acquire the initial controller claim without overwriting one."
    )
    claim_acquire.add_argument("run_path", help="Path to an initialized run JSON record.")
    claim_acquire.add_argument("--controller-id", required=True, help="Controller identity taking ownership.")
    claim_acquire.add_argument("--recorded-by", required=True, help="Identity recording the acquisition.")

    claim_show = claim_commands.add_parser(
        "show", help="Explain current ownership and its immutable history."
    )
    claim_show.add_argument("run_path", help="Path to the claimed run JSON record.")

    for command_name in ("transfer", "recover"):
        change = claim_commands.add_parser(
            command_name,
            help=f"{command_name.title()} ownership and record an immutable receipt.",
        )
        change.add_argument("run_path", help="Path to the claimed run JSON record.")
        change.add_argument(
            "--expected-claim-id",
            required=True,
            help="Exact current claim ID that may be replaced.",
        )
        change.add_argument(
            "--controller-id", required=True, help="Controller identity taking ownership."
        )
        change.add_argument("--reason", required=True, help="Why ownership is changing.")
        change.add_argument(
            "--recorded-by", required=True, help="Identity recording the ownership change."
        )

    activation = run_commands.add_parser(
        "activation", help="Prepare and inspect guarded worker handoff records."
    )
    activation_commands = activation.add_subparsers(
        dest="activation_command", required=True
    )
    activation_attempt = activation_commands.add_parser(
        "attempt",
        help="Recompute owned preflight and record an immutable activation attempt.",
    )
    activation_attempt.add_argument("run_path", help="Path to the canonical run record.")
    activation_attempt.add_argument("packet_path", help="Path to the exact work packet.")
    activation_attempt.add_argument(
        "environment_path", help="Path to an owned-controller environment snapshot."
    )
    activation_attempt.add_argument("--expected-claim-id", required=True)
    activation_attempt.add_argument("--recorded-by", required=True)
    activation_attempt.add_argument("--max-age-seconds", type=int, default=300)

    activation_ready = activation_commands.add_parser(
        "worker-ready",
        help="Record a controller-observed prepared idle worker without starting it.",
    )
    activation_ready.add_argument("run_path", help="Path to the canonical run record.")
    activation_ready.add_argument("attempt_id", help="Activation attempt identity.")
    activation_ready.add_argument("--expected-claim-id", required=True)
    activation_ready.add_argument("--worker-id", required=True)
    activation_ready.add_argument("--workspace-id", required=True)
    activation_ready.add_argument("--recorded-by", required=True)

    activation_commit = activation_commands.add_parser(
        "commit",
        help="Commit initialized to active for an exact prepared worker handoff.",
    )
    activation_commit.add_argument("run_path", help="Path to the canonical run record.")
    activation_commit.add_argument("packet_path", help="Path to the exact work packet.")
    activation_commit.add_argument("attempt_id", help="Activation attempt identity.")
    activation_commit.add_argument("--expected-claim-id", required=True)

    activation_show = activation_commands.add_parser(
        "show", help="Explain an activation attempt and worker-ready receipt."
    )
    activation_show.add_argument("run_path", help="Path to the canonical run record.")
    activation_show.add_argument("attempt_id", help="Activation attempt identity.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    run_id_factory: Callable[[], str] | None = None,
    publication_id_factory: Callable[[], str] | None = None,
    claim_id_factory: Callable[[], str] | None = None,
    attempt_id_factory: Callable[[], str] | None = None,
    receipt_id_factory: Callable[[], str] | None = None,
    claim_root: str | Path | None = None,
    coordination_root: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "packet":
        return _handle_packet(args)
    if args.command == "run":
        return _handle_run(
            args,
            run_id_factory=run_id_factory or _new_run_id,
            publication_id_factory=publication_id_factory or _new_publication_id,
            claim_id_factory=claim_id_factory or _new_claim_id,
            attempt_id_factory=attempt_id_factory or _new_attempt_id,
            receipt_id_factory=receipt_id_factory or _new_receipt_id,
            claim_root=Path(claim_root) if claim_root is not None else None,
            coordination_root=(
                Path(coordination_root) if coordination_root is not None else None
            ),
            clock=clock or _utc_now,
        )
    raise AssertionError(f"unhandled command: {args.command}")


def _handle_packet(args: argparse.Namespace) -> int:
    try:
        packet = WorkPacket.from_path(args.path)
    except PacketError as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return 2

    if args.packet_command == "validate":
        print(f"Valid work packet: {packet.id}")
        return 0
    if args.packet_command == "show":
        print(render_work_packet(packet), end="")
        return 0
    if args.packet_command == "readiness":
        report = evaluate_readiness(packet)
        print(render_readiness(report), end="")
        return 0 if report.ready else 1

    raise AssertionError(f"unhandled packet command: {args.packet_command}")


def _handle_run(
    args: argparse.Namespace,
    *,
    run_id_factory: Callable[[], str],
    publication_id_factory: Callable[[], str],
    claim_id_factory: Callable[[], str],
    attempt_id_factory: Callable[[], str],
    receipt_id_factory: Callable[[], str],
    claim_root: Path | None,
    coordination_root: Path | None,
    clock: Callable[[], datetime],
) -> int:
    if args.run_command == "claim":
        return _handle_controller_claim(
            args,
            claim_id_factory=claim_id_factory,
            claim_root=claim_root,
            coordination_root=coordination_root,
            clock=clock,
        )

    if args.run_command == "activation":
        return _handle_activation(
            args,
            attempt_id_factory=attempt_id_factory,
            receipt_id_factory=receipt_id_factory,
            claim_root=claim_root,
            coordination_root=coordination_root,
            clock=clock,
        )

    if args.run_command == "show":
        try:
            record = RunRecord.from_path(args.path)
        except RunError as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        print(render_run(record), end="")
        return 0

    if args.run_command == "preflight":
        packet_path = Path(args.packet_path)
        try:
            record = RunRecord.from_path(args.run_path)
            packet_content = packet_path.read_bytes()
            environment = ExecutionEnvironment.from_path(args.environment_path)
            report = evaluate_preflight(
                record,
                packet_content,
                environment,
                now=clock(),
                max_age_seconds=args.max_age_seconds,
            )
        except OSError as error:
            print(f"Blocked: cannot read packet {packet_path}: {error}", file=sys.stderr)
            return 2
        except (PacketError, PreflightError, RunError) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2

        print(render_preflight(report), end="")
        return 0 if report.ready else 1

    if args.run_command == "init":
        packet_path = Path(args.packet_path)
        try:
            packet_content = packet_path.read_bytes()
        except OSError as error:
            print(f"Blocked: cannot read packet {packet_path}: {error}", file=sys.stderr)
            return 2

        try:
            initialized_at = clock()
            initialization = initialize_run(
                packet_content,
                run_id=run_id_factory(),
                initiated_by=args.initiated_by,
                now=initialized_at,
                source=f"packet {packet_path}",
            )
        except (PacketError, RunError) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2

        if not initialization.initialized:
            print(render_readiness(initialization.readiness), end="")
            return 1

        record = initialization.record
        if record is None:
            raise AssertionError("initialized result must contain a run record")
        try:
            persist_run(record, args.run_path)
        except RunPublicationError as error:
            print(f"Initialization requires inspection: {error}", file=sys.stderr)
            return 3
        except RunPersistenceError as error:
            print(f"Not initialized: {error}", file=sys.stderr)
            return 3

        try:
            resolved_coordination_root = (
                coordination_root
                if coordination_root is not None
                else default_run_coordination_root()
            )
            publication = register_run_publication(
                record.id,
                args.run_path,
                publication_id=publication_id_factory(),
                recorded_by=args.initiated_by,
                now=initialized_at,
                coordination_root=resolved_coordination_root,
            )
        except (
            RunCoordinationError,
            RunCoordinationPersistenceError,
            RunCoordinationPublicationError,
        ) as error:
            print(
                f"Initialization requires inspection: run exists but its canonical "
                f"publication was not registered cleanly: {error}",
                file=sys.stderr,
            )
            return 3

        print(render_run_initialized(record, args.run_path, publication), end="")
        return 0

    raise AssertionError(f"unhandled run command: {args.run_command}")


def _handle_controller_claim(
    args: argparse.Namespace,
    *,
    claim_id_factory: Callable[[], str],
    claim_root: Path | None,
    coordination_root: Path | None,
    clock: Callable[[], datetime],
) -> int:
    try:
        resolved_claim_root = (
            claim_root if claim_root is not None else default_controller_claim_root()
        )
    except ControllerClaimPersistenceError as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return 2
    try:
        resolved_coordination_root = (
            coordination_root
            if coordination_root is not None
            else default_run_coordination_root()
        )
    except RunCoordinationPersistenceError as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return 2

    if args.claim_command == "show":
        try:
            history = load_controller_claim(
                args.run_path,
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
        except (
            ControllerClaimError,
            RunCoordinationError,
            RunCoordinationPersistenceError,
            RunError,
        ) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        print(render_controller_claim(history), end="")
        return 0

    if args.claim_command == "acquire":
        try:
            history, claim_path = acquire_controller_claim(
                args.run_path,
                claim_id=claim_id_factory(),
                controller_id=args.controller_id,
                recorded_by=args.recorded_by,
                now=clock(),
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
        except ControllerClaimConflictError as error:
            print(f"Not acquired: {error}", file=sys.stderr)
            return 1
        except (ControllerClaimError, RunCoordinationError, RunError) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        except (
            ControllerClaimPublicationError,
            RunCoordinationPublicationError,
        ) as error:
            print(f"Acquisition requires inspection: {error}", file=sys.stderr)
            return 3
        except (
            ControllerClaimPersistenceError,
            RunCoordinationPersistenceError,
        ) as error:
            print(f"Not acquired: {error}", file=sys.stderr)
            return 3
        print(render_controller_claim_acquired(history, str(claim_path)), end="")
        return 0

    if args.claim_command in {"transfer", "recover"}:
        kind = "transferred" if args.claim_command == "transfer" else "recovered"
        try:
            event, history, claim_path = change_controller_claim(
                args.run_path,
                kind=kind,
                expected_claim_id=args.expected_claim_id,
                claim_id=claim_id_factory(),
                controller_id=args.controller_id,
                reason=args.reason,
                recorded_by=args.recorded_by,
                now=clock(),
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
        except ControllerClaimConflictError as error:
            print(
                f"Requested ownership change was not published: {error}. "
                "Re-read the current claim.",
                file=sys.stderr,
            )
            return 1
        except (ControllerClaimError, RunCoordinationError, RunError) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        except (
            ControllerClaimPublicationError,
            RunCoordinationPublicationError,
        ) as error:
            print(f"Ownership change requires inspection: {error}", file=sys.stderr)
            return 3
        except (
            ControllerClaimPersistenceError,
            RunCoordinationPersistenceError,
        ) as error:
            print(
                f"Requested ownership change was not published: {error}",
                file=sys.stderr,
            )
            return 3
        print(
            render_controller_claim_changed(event, history, str(claim_path)),
            end="",
        )
        return 0

    raise AssertionError(f"unhandled claim command: {args.claim_command}")


def _handle_activation(
    args: argparse.Namespace,
    *,
    attempt_id_factory: Callable[[], str],
    receipt_id_factory: Callable[[], str],
    claim_root: Path | None,
    coordination_root: Path | None,
    clock: Callable[[], datetime],
) -> int:
    try:
        resolved_claim_root = (
            claim_root if claim_root is not None else default_controller_claim_root()
        )
        resolved_coordination_root = (
            coordination_root
            if coordination_root is not None
            else default_run_coordination_root()
        )
    except (ControllerClaimPersistenceError, RunCoordinationPersistenceError) as error:
        print(f"Blocked: {error}", file=sys.stderr)
        return 2

    if args.activation_command == "attempt":
        packet_path = Path(args.packet_path)
        environment_path = Path(args.environment_path)
        try:
            packet_content = packet_path.read_bytes()
            environment_content = environment_path.read_bytes()
        except OSError as error:
            print(f"Blocked: cannot read activation input: {error}", file=sys.stderr)
            return 2
        try:
            preparation = prepare_activation_attempt(
                args.run_path,
                packet_content,
                environment_content,
                expected_claim_id=args.expected_claim_id,
                attempt_id=attempt_id_factory(),
                recorded_by=args.recorded_by,
                now=clock(),
                max_age_seconds=args.max_age_seconds,
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
        except (ActivationConflictError, RunCoordinationConflictError) as error:
            print(f"Activation attempt was not recorded: {error}", file=sys.stderr)
            return 1
        except RunCoordinationPublicationError as error:
            print(f"Activation attempt requires inspection: {error}", file=sys.stderr)
            return 3
        except RunCoordinationPersistenceError as error:
            print(f"Activation attempt was not recorded: {error}", file=sys.stderr)
            return 3
        except (
            ActivationError,
            ControllerClaimError,
            PacketError,
            PreflightError,
            RunCoordinationError,
            RunError,
        ) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2

        if not preparation.prepared:
            print(render_preflight(preparation.preflight), end="")
            return 1
        attempt = preparation.attempt
        if attempt is None:
            raise AssertionError("prepared activation must contain an attempt")
        print(render_activation_prepared(attempt, preparation.preflight), end="")
        return 0

    if args.activation_command == "worker-ready":
        try:
            receipt = record_worker_ready(
                args.run_path,
                attempt_id=args.attempt_id,
                expected_claim_id=args.expected_claim_id,
                receipt_id=receipt_id_factory(),
                worker_id=args.worker_id,
                workspace_id=args.workspace_id,
                recorded_by=args.recorded_by,
                now=clock(),
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
            attempt = ActivationAttempt.from_path(
                activation_attempt_path(
                    receipt.run_id, receipt.attempt_id, resolved_coordination_root
                )
            )
        except (ActivationConflictError, RunCoordinationConflictError) as error:
            print(f"Worker-ready observation was not recorded: {error}", file=sys.stderr)
            return 1
        except RunCoordinationPublicationError as error:
            print(f"Worker-ready observation requires inspection: {error}", file=sys.stderr)
            return 3
        except RunCoordinationPersistenceError as error:
            print(f"Worker-ready observation was not recorded: {error}", file=sys.stderr)
            return 3
        except (
            ActivationError,
            ControllerClaimError,
            RunCoordinationError,
            RunError,
        ) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        print(render_worker_ready_recorded(attempt, receipt), end="")
        return 0

    if args.activation_command == "commit":
        packet_path = Path(args.packet_path)
        try:
            packet_content = packet_path.read_bytes()
        except OSError as error:
            print(f"Blocked: cannot read packet {packet_path}: {error}", file=sys.stderr)
            return 2
        try:
            record = commit_activation(
                args.run_path,
                packet_content,
                attempt_id=args.attempt_id,
                expected_claim_id=args.expected_claim_id,
                now=clock(),
                claim_root=resolved_claim_root,
                coordination_root=resolved_coordination_root,
            )
        except (ActivationConflictError, RunCoordinationConflictError) as error:
            print(f"Activation was not committed: {error}", file=sys.stderr)
            return 1
        except RunCoordinationPublicationError as error:
            print(f"Activation commit requires inspection: {error}", file=sys.stderr)
            return 3
        except RunCoordinationPersistenceError as error:
            print(f"Activation was not committed: {error}", file=sys.stderr)
            return 3
        except (
            ActivationError,
            ControllerClaimError,
            PacketError,
            PreflightError,
            RunCoordinationError,
            RunError,
        ) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        print(render_activation_committed(record), end="")
        return 0

    if args.activation_command == "show":
        try:
            run, attempt, ready = inspect_activation(
                args.run_path,
                args.attempt_id,
                coordination_root=resolved_coordination_root,
            )
        except (
            ActivationError,
            RunCoordinationError,
            RunCoordinationPersistenceError,
            RunError,
        ) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
        print(render_activation_attempt(attempt, ready, run), end="")
        return 0

    raise AssertionError(f"unhandled activation command: {args.activation_command}")


def _new_run_id() -> str:
    return f"RUN-{uuid.uuid4()}"


def _new_publication_id() -> str:
    return f"PUBLICATION-{uuid.uuid4()}"


def _new_claim_id() -> str:
    return f"CLAIM-{uuid.uuid4()}"


def _new_attempt_id() -> str:
    return f"ACTIVATION-{uuid.uuid4()}"


def _new_receipt_id() -> str:
    return f"WORKER-READY-{uuid.uuid4()}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
