from __future__ import annotations

from .readiness import ReadinessReport, evaluate_readiness
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
