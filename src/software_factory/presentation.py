from __future__ import annotations

from .activation import ActivationAttempt, WorkerReadyReceipt
from .controller_claims import ControllerClaimEvent, ControllerClaimHistory
from .preflight import PreflightReport
from .readiness import ReadinessReport, evaluate_readiness
from .run_coordination import RunPublication
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
        if transition.activation is not None:
            lines.append(f"     Claim: {transition.activation.claim_id}")
            lines.append(f"     Activation attempt: {transition.activation.attempt_id}")
            lines.append(f"     Prepared worker: {transition.activation.worker_id}")

    lines.extend(
        [
            "",
            "Execution and authority",
            "  Initializing this run did not start a worker or perform an external action.",
            "  This run record does not grant authority to act.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_run_initialized(
    record: RunRecord, path: str, publication: RunPublication
) -> str:
    return (
        f"Run initialized at {path}.\n"
        f"Canonical publication: {publication.id}.\n"
        f"Coordination is bound to this exact run entry; copies and aliases cannot act "
        f"for it.\n\n{render_run(record)}"
    )


def render_controller_claim(history: ControllerClaimHistory) -> str:
    current = history.current
    lines = [
        f"{history.run_id} is claimed by {current.controller_id}.",
        "",
        "Current controller ownership",
        f"  Claim ID: {current.claim_id}",
        f"  Run publication: {current.publication_id or 'legacy claim — unbound'}",
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
        f"  Required controller state: {report.required_controller_state}",
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


def render_activation_prepared(
    attempt: ActivationAttempt, report: PreflightReport
) -> str:
    lines = [
        f"Activation attempt {attempt.id} is prepared for {attempt.run_id}.",
        "",
        "Exact bindings",
        f"  Run publication: {attempt.publication_id}",
        f"  Run state: {attempt.expected_state}",
        f"  Controller claim: {attempt.claim_id}",
        f"  Controller: {attempt.controller_id}",
        f"  Packet: {attempt.packet_id} ({attempt.packet_digest})",
        f"  Workspace: {attempt.environment.workspace.id}",
        f"  Environment snapshot: {attempt.environment_digest}",
        f"  Preflight evaluator: {attempt.preflight_evaluator}",
        f"  Required controller state: {report.required_controller_state}",
        f"  Evaluated at: {attempt.preflight_evaluated_at}",
        "",
        "Handoff boundary",
        "  ✓ Current packet, run, claim, environment, and preflight were bound",
        "    into an immutable activation attempt.",
        "  No worker-ready observation has been recorded.",
        "  The run remains initialized; no worker was told to begin.",
        "  An activation attempt grants no external authority and is not completion.",
    ]
    return "\n".join(lines) + "\n"


def render_worker_ready_recorded(
    attempt: ActivationAttempt, receipt: WorkerReadyReceipt
) -> str:
    lines = [
        f"Worker-ready observation {receipt.id} was recorded for {attempt.id}.",
        "",
        "Prepared handoff",
        f"  Run: {receipt.run_id}",
        f"  Run publication: {receipt.publication_id}",
        f"  Controller claim: {attempt.claim_id}",
        f"  Worker: {receipt.worker_id}",
        f"  Workspace: {receipt.workspace_id}",
        f"  Recorded by controller: {receipt.recorded_by}",
        f"  Recorded at: {receipt.recorded_at}",
        "",
        "Observation boundary",
        "  This is the controller's durable record of an adapter-reported idle worker.",
        "  It is not independent proof, does not grant the worker authority, and did",
        "  not tell the worker to begin. The run remains initialized until commit.",
    ]
    return "\n".join(lines) + "\n"


def render_activation_attempt(
    attempt: ActivationAttempt,
    receipt: WorkerReadyReceipt | None,
    run: RunRecord,
) -> str:
    lines = [
        f"Activation attempt {attempt.id} targets {attempt.run_id}.",
        "",
        "Attempt",
        f"  Created at: {attempt.created_at}",
        f"  Controller claim: {attempt.claim_id}",
        f"  Controller: {attempt.controller_id}",
        f"  Workspace: {attempt.environment.workspace.id}",
        f"  Immutable preflight verdict: {'passed' if attempt.preflight_ready else 'blocked'}",
        "",
        "Worker readiness",
    ]
    if receipt is None:
        lines.append("  No worker-ready observation is recorded.")
    else:
        lines.extend(
            [
                f"  Worker: {receipt.worker_id}",
                f"  Workspace: {receipt.workspace_id}",
                f"  Recorded at: {receipt.recorded_at}",
                f"  Recorded by controller: {receipt.recorded_by}",
                "  This is a controller-recorded adapter observation, not independent proof.",
            ]
        )
    lines.extend(
        [
            "",
            "Run state",
            f"  Current state: {run.current_state}",
            "  Active means the guarded handoff was committed; it does not mean the",
            "  work is complete and grants no merge, deploy, or other external authority.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_activation_committed(record: RunRecord) -> str:
    activation = record.transitions[-1].activation
    if activation is None:
        raise ValueError("committed activation requires an activation binding")
    lines = [
        f"Run {record.id} is active from activation attempt {activation.attempt_id}.",
        "",
        "Committed handoff",
        f"  Controller claim: {activation.claim_id}",
        f"  Worker: {activation.worker_id}",
        f"  Attempt digest: {activation.attempt_digest}",
        f"  Worker-ready digest: {activation.worker_ready_digest}",
        "",
        "Authority boundary",
        "  Active records a guarded worker handoff. It is not work completion, proof",
        "  of acceptance, merge readiness, or authority to perform an external action.",
        "  This command did not tell the prepared worker to begin.",
    ]
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
