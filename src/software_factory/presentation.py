from __future__ import annotations

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

    effective = packet.authority.effective_actions
    explicitly_denied = packet.authority.deny
    unavailable = AUTHORITY_ACTIONS - effective
    lines.extend(
        [
            "",
            "Authority",
            f"  Mode: {packet.authority.mode}",
            f"  Granted by: {packet.authority.granted_by}",
            f"  May: {_joined(effective)}",
            f"  May not: {_joined(unavailable)}",
        ]
    )
    if explicitly_denied:
        lines.append(f"  Explicit denials: {_joined(explicitly_denied)}")

    if packet.unresolved_decisions:
        lines.extend(
            [
                "",
                "Human action required",
                f"  Resolve {len(packet.unresolved_decisions)} decision(s) before execution.",
            ]
        )
    else:
        lines.extend(["", "Human action required", "  None before execution."])

    return "\n".join(lines) + "\n"


def _render_items(label: str, values: tuple[str, ...]) -> list[str]:
    if not values:
        return [f"  {label}: none"]
    result = [f"  {label}:"]
    result.extend(f"    - {value}" for value in values)
    return result


def _joined(values: frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "nothing"
