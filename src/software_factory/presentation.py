from __future__ import annotations

from .controller_claims import ControllerClaimEvent, ControllerClaimHistory
from .preflight import PreflightReport
from .readiness import ReadinessReport, evaluate_readiness
from .runs import RunRecord
from .work_packets import AUTHORITY_ACTIONS, WorkPacket


def render_work_packet(packet: WorkPacket) -> str:
    lines = [
        f"{packet.id}: {packet.intent}",
        "",
        "Desired outcome",
        f"  {packet.desired_outcome}",
        "",
        "Scope",
    ]
    lines.extend(_render_items("Included", packet.scope.include))
    lines.extend(_render_items("Excluded", packet.scope.exclude))

    lines.extend(["", "Acceptance"])
    for criterion in packet.acceptance:
        evidence = ", ".join(criterion.evidence_required)
        lines.append(f"  [ ] {criterion.id}: {criterion.statement}")
        lines.append(f"      Evidence required: {evidence}")

    lines.extend(["", "Decisions"])
    if not packet.decisions:
        lines.append("  No decisions are recorded.")
    for decision in packet.decisions:
        if decision.state == "resolved":
            lines.append(f"  ✓ {decision.id}: {decision.question}")
            lines.append(f"      {decision.decision} Decided by {decision.decided_by}.")
        else:
            lines.append(f"  ? {decision.id}: {decision.question}")

    lines.extend(["", "Dependencies"])
    if not packet.dependencies:
        lines.append("  No dependencies are recorded.")
    for dependency in packet.dependencies:
        lines.append(f"  - {dependency.id}: {dependency.state}")

    lines.extend(_render_authority(packet))

    readiness = evaluate_readiness(packet)
    lines.extend(["", "Packet readiness"])
    if readiness.ready:
        lines.append("  No packet-definition blockers were found.")
    else:
        lines.extend(f"  • {blocker.message}" for blocker in readiness.blockers)

    return "\n".join(lines) + "\n"


def render_readiness(report: ReadinessReport) -> str:
    packet = report.packet
    status = (
        "ready for run preflight"
        if report.ready
        else "not ready for run preflight"
    )
    lines = [
        f"{packet.id} is {status}.",
        "",
        "Outcome",
        f"  {packet.desired_outcome}",
    ]

    if report.ready:
        criterion_count = len(packet.acceptance)
        criterion_label = "criterion is" if criterion_count == 1 else "criteria are"
        lines.extend(
            [
                "",
                "Packet checks",
                f"  ✓ {criterion_count} acceptance {criterion_label} defined.",
                "  ✓ Every criterion names required evidence.",
                "  ✓ No consequential decisions remain unresolved.",
                "  ✓ Every dependency is satisfied.",
            ]
        )
    else:
        lines.extend(["", "Blockers"])
        lines.extend(f"  • {blocker.message}" for blocker in report.blockers)

    lines.extend(_render_authority(packet))
    lines.extend(
        [
            "",
            "No run was started and no external action was performed.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_run(record: RunRecord) -> str:
    terminal = "yes" if record.terminal else "no"
    lines = [
        f"{record.id} is {record.current_state}.",
        "",
        "Work packet",
        f"  ID: {record.packet.id}",
        f"  Exact version: {record.packet.digest}",
        "",
        "Readiness observation",
        "  ✓ The packet was ready when this run was initialized.",
        f"  Evaluator: {record.readiness.evaluator}",
        f"  Observed at: {record.readiness.evaluated_at}",
        "  This historical observation must not authorize a later action.",
        "",
        "Lifecycle",
        f"  Current state: {record.current_state}",
        f"  Terminal: {terminal}",
        f"  Initiated by: {record.initiated_by}",
        "",
        "Transition history",
    ]
    for transition in record.transitions:
        from_state = transition.from_state or "nothing"
        reason = transition.reason.replace("-", " ")
        transition_line = f"  {transition.sequence}. {from_state} → {transition.to_state} at {transition.at} by {transition.recorded_by}"
        lines.append(transition_line)
        lines.append(f"     Reason: {reason}")

    lines.extend(
        [
            "",
            "Execution and authority",
            "  Initializing this run did not start a worker or perform an external action.",
            "  This run record does not grant authority to act.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_run_initialized(record: RunRecord, path: str) -> str:
    return f"Run initialized at {path}.\n\n{render_run(record)}"


def render_controller_claim(history: ControllerClaimHistory) -> str:
    current = history.current
    lines = [
        f"{history.run_id} is claimed by {current.controller_id}.",
        "",
        "Current controller ownership",
        f"  Claim ID: {current.claim_id}",
        f"  Controller: {current.controller_id}",
        f"  Established at: {current.at}",
        f"  Recorded by: {current.recorded_by}",
        "",
        "Ownership history",
    ]
    for event in history.events:
        lines.append(
            f"  {event.sequence}. {event.kind} claim {event.claim_id} at {event.at}"
        )
        lines.append(f"     Controller: {event.controller_id}")
        lines.append(f"     Reason: {event.reason}")
        lines.append(f"     Recorded by: {event.recorded_by}")
        if event.previous_claim_id is not None:
            lines.append(f"     Replaced claim: {event.previous_claim_id}")

    lines.extend(
        [
            "",
            "Ownership boundary",
            "  This claim does not expire automatically.",
            "  Claim age alone never authorizes takeover.",
            "  A transfer or recovery must name the exact current claim and leave a receipt.",
            "  Controller ownership alone grants no authority to edit, commit, push, merge,",
            "  deploy, start a worker, or perform another action.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_controller_claim_acquired(
    history: ControllerClaimHistory, path: str
) -> str:
    return f"Controller claim acquired at {path}.\n\n{render_controller_claim(history)}"


def render_controller_claim_changed(
    event: ControllerClaimEvent,
    history: ControllerClaimHistory,
    path: str,
) -> str:
    action = "transferred" if event.kind == "transferred" else "recovered"
    return (
        f"Controller ownership {action}; receipt recorded at "
        f"{path}/{event.sequence:06d}.json.\n\n{render_controller_claim(history)}"
    )


def render_preflight(report: PreflightReport) -> str:
    status = "passes the current execution preflight" if report.ready else "is blocked from execution"
    environment = report.environment
    lines = [
        f"{report.run.id} {status}.",
        "",
        "Packet binding",
        f"  Run packet: {report.run.packet.id}",
        f"  Exact version: {report.run.packet.digest}",
        "  ✓ Current packet identity was checked against the run.",
        "",
        "Collected environment",
        f"  Snapshot generated at: {environment.generated_at}",
        f"  Controller: {environment.controller.id} ({environment.controller.state})",
        f"  Controller observed by: {environment.controller.observed_by}",
        f"  Workspace: {environment.workspace.id}",
        f"  Available: {'yes' if environment.workspace.available else 'no'}",
        f"  Isolated: {'yes' if environment.workspace.isolated else 'no'}",
        f"  Clean: {'yes' if environment.workspace.clean else 'no'}",
        f"  Workspace observed by: {environment.workspace.observed_by}",
        "",
        "Execution capabilities",
    ]
    for capability in environment.capabilities:
        marker = "✓" if capability.available else "×"
        lines.append(
            f"  {marker} {capability.name} — observed by {capability.observed_by}"
        )

    lines.extend(["", "Verification routes"])
    for route in environment.verification_routes:
        marker = "✓" if route.available else "×"
        lines.append(f"  {marker} {route.kind} — observed by {route.observed_by}")

    lines.extend(
        [
            "",
            "Requested worker actions",
            f"  {_joined(environment.requested_actions)}",
            f"  Packet authority mode: {report.packet.authority.mode}",
            f"  Authority state: {environment.authority.state}",
            f"  Authority observed by: {environment.authority.observed_by}",
        ]
    )

    if report.ready:
        lines.extend(
            [
                "",
                "Preflight checks",
                "  ✓ The run is initialized.",
                "  ✓ Packet readiness was recomputed.",
                "  ✓ Required capabilities and verification routes are available.",
                "  ✓ Requested worker actions remain inside the restricted boundary.",
            ]
        )
    else:
        lines.extend(["", "Blockers"])
        lines.extend(f"  • {blocker.message}" for blocker in report.blockers)

    lines.extend(
        [
            "",
            "Collector observations report state; they do not grant authority.",
            "A passing preflight verdict is not an activation token.",
            "No controller ownership was acquired, no run transition was recorded,",
            "and no worker or external action was started.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_authority(packet: WorkPacket) -> list[str]:
    effective = packet.authority.effective_actions
    unavailable = AUTHORITY_ACTIONS - effective
    lines = [
        "",
        "Authority",
        f"  Mode: {packet.authority.mode}",
        f"  Granted by: {packet.authority.granted_by}",
        f"  May: {_joined(effective)}",
        f"  May not: {_joined(unavailable)}",
    ]
    if packet.authority.deny:
        lines.append(f"  Explicit denials: {_joined(packet.authority.deny)}")
    return lines


def _render_items(label: str, values: tuple[str, ...]) -> list[str]:
    if not values:
        return [f"  {label}: none"]
    result = [f"  {label}:"]
    result.extend(f"    - {value}" for value in values)
    return result


def _joined(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "nothing"
