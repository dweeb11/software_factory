from __future__ import annotations

import hashlib
from dataclasses import dataclass
from json import JSONDecodeError
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .json_records import DuplicateJsonMemberError, parse_json
from .readiness import ReadinessReport, evaluate_readiness
from .runs import RunRecord
from .work_packets import AUTHORITY_ACTIONS, EVIDENCE_KINDS, WorkPacket


CONTROLLER_STATES = frozenset({"available", "contended", "unknown"})
AUTHORITY_STATES = frozenset({"current", "revoked", "unknown"})
EXECUTION_CAPABILITIES = frozenset(
    {"workspace-read", "workspace-write", "command-execution"}
)
WORKER_ACTIONS = frozenset({"edit", "commit"})
MUTATING_PACKET_KINDS = frozenset({"experiment", "change", "program"})


class PreflightError(ValueError):
    """A preflight input is unreadable or violates the protocol."""


@dataclass(frozen=True)
class ControllerObservation:
    id: str
    state: str
    observed_by: str


@dataclass(frozen=True)
class WorkspaceObservation:
    id: str
    available: bool
    isolated: bool
    clean: bool
    observed_by: str


@dataclass(frozen=True)
class CapabilityObservation:
    name: str
    available: bool
    observed_by: str


@dataclass(frozen=True)
class VerificationRoute:
    kind: str
    available: bool
    observed_by: str


@dataclass(frozen=True)
class AuthorityObservation:
    state: str
    observed_by: str


@dataclass(frozen=True)
class ExecutionEnvironment:
    schema_version: int
    generated_at: str
    run_id: str
    controller: ControllerObservation
    workspace: WorkspaceObservation
    capabilities: tuple[CapabilityObservation, ...]
    verification_routes: tuple[VerificationRoute, ...]
    requested_actions: frozenset[str]
    authority: AuthorityObservation

    def __post_init__(self) -> None:
        _validate_environment(self)

    @classmethod
    def from_path(cls, path: str | Path) -> ExecutionEnvironment:
        environment_path = Path(path)
        try:
            raw_bytes = environment_path.read_bytes()
        except OSError as error:
            raise PreflightError(
                f"cannot read environment snapshot {environment_path}: {error}"
            ) from error

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PreflightError(
                f"environment snapshot {environment_path} is not valid UTF-8: byte {error.start}"
            ) from error

        try:
            raw = parse_json(text)
        except JSONDecodeError as error:
            message = f"environment snapshot {environment_path} is not valid JSON: line {error.lineno}, column {error.colno}"
            raise PreflightError(message) from error
        except DuplicateJsonMemberError as error:
            raise PreflightError(
                f"environment snapshot {environment_path} is ambiguous: {error}"
            ) from error
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: object) -> ExecutionEnvironment:
        data = _mapping(raw, "environment")
        _require_fields(
            data,
            {
                "schema_version",
                "generated_at",
                "run_id",
                "controller",
                "workspace",
                "capabilities",
                "verification_routes",
                "requested_actions",
                "authority",
            },
            "environment",
        )

        schema_version = data.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise PreflightError("schema_version must be the integer 1")

        controller_data = _mapping(data.get("controller"), "controller")
        _require_fields(controller_data, {"id", "state", "observed_by"}, "controller")
        controller_state = _choice(
            controller_data.get("state"), "controller.state", CONTROLLER_STATES
        )
        controller = ControllerObservation(
            id=_text(controller_data.get("id"), "controller.id"),
            state=controller_state,
            observed_by=_text(
                controller_data.get("observed_by"), "controller.observed_by"
            ),
        )

        workspace_data = _mapping(data.get("workspace"), "workspace")
        _require_fields(
            workspace_data,
            {"id", "available", "isolated", "clean", "observed_by"},
            "workspace",
        )
        workspace = WorkspaceObservation(
            id=_text(workspace_data.get("id"), "workspace.id"),
            available=_boolean(workspace_data.get("available"), "workspace.available"),
            isolated=_boolean(workspace_data.get("isolated"), "workspace.isolated"),
            clean=_boolean(workspace_data.get("clean"), "workspace.clean"),
            observed_by=_text(
                workspace_data.get("observed_by"), "workspace.observed_by"
            ),
        )

        authority_data = _mapping(data.get("authority"), "authority")
        _require_fields(authority_data, {"state", "observed_by"}, "authority")
        authority = AuthorityObservation(
            state=_choice(
                authority_data.get("state"), "authority.state", AUTHORITY_STATES
            ),
            observed_by=_text(
                authority_data.get("observed_by"), "authority.observed_by"
            ),
        )

        return cls(
            schema_version=schema_version,
            generated_at=_timestamp(data.get("generated_at"), "generated_at"),
            run_id=_text(data.get("run_id"), "run_id"),
            controller=controller,
            workspace=workspace,
            capabilities=_capabilities(data.get("capabilities")),
            verification_routes=_verification_routes(data.get("verification_routes")),
            requested_actions=_requested_actions(data.get("requested_actions")),
            authority=authority,
        )


@dataclass(frozen=True)
class PreflightBlocker:
    code: str
    message: str


@dataclass(frozen=True)
class PreflightReport:
    run: RunRecord
    packet: WorkPacket
    environment: ExecutionEnvironment
    packet_readiness: ReadinessReport
    blockers: tuple[PreflightBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def evaluate_preflight(
    run: RunRecord,
    packet_content: bytes,
    environment: ExecutionEnvironment,
    *,
    now: datetime,
    max_age_seconds: int = 300,
) -> PreflightReport:
    if type(max_age_seconds) is not int or max_age_seconds < 1:
        raise PreflightError("max_age_seconds must be an integer of at least 1")
    _validate_environment(environment)
    current_time = _aware_utc(now, "now")
    packet = WorkPacket.from_bytes(packet_content)
    packet_readiness = evaluate_readiness(packet)
    blockers: list[PreflightBlocker] = []

    if run.current_state != "initialized":
        blockers.append(
            PreflightBlocker(
                code="run-not-initialized",
                message=f"Run state is {run.current_state}; preflight requires initialized.",
            )
        )

    if environment.run_id != run.id:
        blockers.append(
            PreflightBlocker(
                code="environment-wrong-run",
                message=f"Environment snapshot names run {environment.run_id}, not {run.id}.",
            )
        )

    generated_at = _parse_timestamp(environment.generated_at)
    run_state_at = _parse_timestamp(run.transitions[-1].at)
    if generated_at < run_state_at:
        blockers.append(
            PreflightBlocker(
                code="environment-predates-run-state",
                message=(
                    f"Environment snapshot predates the run's current {run.current_state} state "
                    f"at {run.transitions[-1].at}."
                ),
            )
        )

    age_seconds = (current_time - generated_at).total_seconds()
    if age_seconds < 0:
        blockers.append(
            PreflightBlocker(
                code="environment-from-future",
                message="Environment snapshot was generated in the future.",
            )
        )
    elif age_seconds > max_age_seconds:
        blockers.append(
            PreflightBlocker(
                code="environment-stale",
                message=(
                    f"Environment snapshot is {_format_seconds(age_seconds)} seconds old; "
                    f"the limit is {max_age_seconds}."
                ),
            )
        )

    packet_digest = f"sha256:{hashlib.sha256(packet_content).hexdigest()}"
    if packet.id != run.packet.id:
        blockers.append(
            PreflightBlocker(
                code="packet-id-mismatch",
                message=f"Packet ID is {packet.id}, but the run is bound to {run.packet.id}.",
            )
        )
    if packet_digest != run.packet.digest:
        blockers.append(
            PreflightBlocker(
                code="packet-digest-mismatch",
                message="Current packet bytes do not match the exact packet version bound to the run.",
            )
        )

    for blocker in packet_readiness.blockers:
        blockers.append(
            PreflightBlocker(
                code=f"packet:{blocker.code}",
                message=f"Packet readiness: {blocker.message}",
            )
        )

    if environment.controller.state != "available":
        blockers.append(
            PreflightBlocker(
                code=f"controller-{environment.controller.state}",
                message=f"Controller ownership is {environment.controller.state}, not available.",
            )
        )

    if not environment.workspace.available:
        blockers.append(
            PreflightBlocker(
                code="workspace-unavailable",
                message=f"Workspace {environment.workspace.id} is not available.",
            )
        )
    if not environment.workspace.isolated:
        blockers.append(
            PreflightBlocker(
                code="workspace-not-isolated",
                message=f"Workspace {environment.workspace.id} is not isolated.",
            )
        )
    if not environment.workspace.clean:
        blockers.append(
            PreflightBlocker(
                code="workspace-not-clean",
                message=f"Workspace {environment.workspace.id} is not clean.",
            )
        )

    capabilities = {item.name: item for item in environment.capabilities}
    required_capabilities = {"workspace-read", "command-execution"}
    if packet.kind in MUTATING_PACKET_KINDS:
        required_capabilities.add("workspace-write")
    for name in sorted(required_capabilities):
        observation = capabilities.get(name)
        if observation is None:
            blockers.append(
                PreflightBlocker(
                    code=f"capability-missing:{name}",
                    message=f"Required execution capability {name} was not observed.",
                )
            )
        elif not observation.available:
            blockers.append(
                PreflightBlocker(
                    code=f"capability-unavailable:{name}",
                    message=f"Required execution capability {name} is unavailable.",
                )
            )

    routes = {route.kind: route for route in environment.verification_routes}
    required_routes = {
        evidence
        for criterion in packet.acceptance
        for evidence in criterion.evidence_required
    }
    for kind in sorted(required_routes):
        route = routes.get(kind)
        if route is None:
            blockers.append(
                PreflightBlocker(
                    code=f"verification-route-missing:{kind}",
                    message=f"Required {kind} evidence has no configured verification route.",
                )
            )
        elif not route.available:
            blockers.append(
                PreflightBlocker(
                    code=f"verification-route-unavailable:{kind}",
                    message=f"The configured {kind} verification route is unavailable.",
                )
            )

    if packet.kind in MUTATING_PACKET_KINDS and "edit" not in environment.requested_actions:
        blockers.append(
            PreflightBlocker(
                code="required-action-missing:edit",
                message=f"{packet.kind} execution must request the edit action.",
            )
        )
    for action in sorted(environment.requested_actions):
        if action not in WORKER_ACTIONS:
            blockers.append(
                PreflightBlocker(
                    code=f"action-outside-worker-boundary:{action}",
                    message=f"Action {action} is not allowed inside the restricted worker boundary.",
                )
            )
        if not packet.authority.allows(action):
            blockers.append(
                PreflightBlocker(
                    code=f"action-not-authorized:{action}",
                    message=f"The packet-bound authority does not allow {action}.",
                )
            )

    if environment.authority.state != "current":
        blockers.append(
            PreflightBlocker(
                code=f"authority-{environment.authority.state}",
                message=f"Authority was observed as {environment.authority.state}, not current.",
            )
        )

    return PreflightReport(
        run=run,
        packet=packet,
        environment=environment,
        packet_readiness=packet_readiness,
        blockers=tuple(blockers),
    )


def _validate_environment(environment: ExecutionEnvironment) -> None:
    if type(environment.schema_version) is not int or environment.schema_version != 1:
        raise PreflightError("schema_version must be the integer 1")
    _timestamp(environment.generated_at, "generated_at")
    _text(environment.run_id, "run_id")

    if not isinstance(environment.controller, ControllerObservation):
        raise PreflightError("controller must be a ControllerObservation")
    _text(environment.controller.id, "controller.id")
    _choice(environment.controller.state, "controller.state", CONTROLLER_STATES)
    _text(environment.controller.observed_by, "controller.observed_by")

    if not isinstance(environment.workspace, WorkspaceObservation):
        raise PreflightError("workspace must be a WorkspaceObservation")
    _text(environment.workspace.id, "workspace.id")
    _boolean(environment.workspace.available, "workspace.available")
    _boolean(environment.workspace.isolated, "workspace.isolated")
    _boolean(environment.workspace.clean, "workspace.clean")
    _text(environment.workspace.observed_by, "workspace.observed_by")

    if not isinstance(environment.capabilities, tuple):
        raise PreflightError("capabilities must be a tuple")
    capability_names: set[str] = set()
    for index, capability in enumerate(environment.capabilities):
        if not isinstance(capability, CapabilityObservation):
            raise PreflightError(f"capabilities[{index}] must be a CapabilityObservation")
        name = _choice(
            capability.name, f"capabilities[{index}].name", EXECUTION_CAPABILITIES
        )
        if name in capability_names:
            raise PreflightError(f"duplicate capability: {name}")
        capability_names.add(name)
        _boolean(capability.available, f"capabilities[{index}].available")
        _text(capability.observed_by, f"capabilities[{index}].observed_by")

    if not isinstance(environment.verification_routes, tuple):
        raise PreflightError("verification_routes must be a tuple")
    route_kinds: set[str] = set()
    for index, route in enumerate(environment.verification_routes):
        if not isinstance(route, VerificationRoute):
            raise PreflightError(
                f"verification_routes[{index}] must be a VerificationRoute"
            )
        kind = _choice(route.kind, f"verification_routes[{index}].kind", EVIDENCE_KINDS)
        if kind in route_kinds:
            raise PreflightError(f"duplicate verification route: {kind}")
        route_kinds.add(kind)
        _boolean(route.available, f"verification_routes[{index}].available")
        _text(route.observed_by, f"verification_routes[{index}].observed_by")

    if not isinstance(environment.requested_actions, frozenset):
        raise PreflightError("requested_actions must be a frozenset")
    for action in environment.requested_actions:
        _choice(action, "requested_actions item", AUTHORITY_ACTIONS)

    if not isinstance(environment.authority, AuthorityObservation):
        raise PreflightError("authority must be an AuthorityObservation")
    _choice(environment.authority.state, "authority.state", AUTHORITY_STATES)
    _text(environment.authority.observed_by, "authority.observed_by")


def _format_seconds(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _capabilities(value: object) -> tuple[CapabilityObservation, ...]:
    if not isinstance(value, list):
        raise PreflightError("capabilities must be a list")
    result: list[CapabilityObservation] = []
    seen: set[str] = set()
    for index, item in enumerate(cast(list[object], value)):
        field = f"capabilities[{index}]"
        data = _mapping(item, field)
        _require_fields(data, {"name", "available", "observed_by"}, field)
        name = _choice(data.get("name"), f"{field}.name", EXECUTION_CAPABILITIES)
        if name in seen:
            raise PreflightError(f"duplicate capability: {name}")
        seen.add(name)
        result.append(
            CapabilityObservation(
                name=name,
                available=_boolean(data.get("available"), f"{field}.available"),
                observed_by=_text(data.get("observed_by"), f"{field}.observed_by"),
            )
        )
    return tuple(result)


def _verification_routes(value: object) -> tuple[VerificationRoute, ...]:
    if not isinstance(value, list):
        raise PreflightError("verification_routes must be a list")
    result: list[VerificationRoute] = []
    seen: set[str] = set()
    for index, item in enumerate(cast(list[object], value)):
        field = f"verification_routes[{index}]"
        data = _mapping(item, field)
        _require_fields(data, {"kind", "available", "observed_by"}, field)
        kind = _choice(data.get("kind"), f"{field}.kind", EVIDENCE_KINDS)
        if kind in seen:
            raise PreflightError(f"duplicate verification route: {kind}")
        seen.add(kind)
        result.append(
            VerificationRoute(
                kind=kind,
                available=_boolean(data.get("available"), f"{field}.available"),
                observed_by=_text(data.get("observed_by"), f"{field}.observed_by"),
            )
        )
    return tuple(result)


def _requested_actions(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise PreflightError("requested_actions must be a list")
    actions = tuple(
        _text(item, f"requested_actions[{index}]")
        for index, item in enumerate(cast(list[object], value))
    )
    if len(set(actions)) != len(actions):
        raise PreflightError("requested_actions must not contain duplicates")
    unknown = set(actions) - AUTHORITY_ACTIONS
    if unknown:
        raise PreflightError(
            f"requested_actions contains unknown actions: {', '.join(sorted(unknown))}"
        )
    return frozenset(actions)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PreflightError(f"{field} must be an object")
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in items):
        raise PreflightError(f"{field} keys must be strings")
    return cast(dict[str, object], value)


def _require_fields(data: dict[str, object], expected: set[str], field: str) -> None:
    missing = expected - set(data)
    if missing:
        raise PreflightError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    unknown = set(data) - expected
    if unknown:
        raise PreflightError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightError(f"{field} must be a non-empty string")
    return value.strip()


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise PreflightError(f"{field} must be a boolean")
    return value


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    text = _text(value, field)
    if text not in choices:
        raise PreflightError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return text


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = _parse_timestamp(text)
    except ValueError as error:
        raise PreflightError(
            f"{field} must be UTC ISO-8601 with seconds, such as 2026-08-30T12:00:00Z"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise PreflightError(
            f"{field} must be UTC ISO-8601 with seconds, such as 2026-08-30T12:00:00Z"
        )
    return text


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PreflightError(f"{field} must include a UTC offset")
    return value.astimezone(timezone.utc)
