from __future__ import annotations

from dataclasses import dataclass

from .work_packets import WorkPacket


@dataclass(frozen=True)
class ReadinessBlocker:
    code: str
    message: str


@dataclass(frozen=True)
class ReadinessReport:
    packet: WorkPacket
    blockers: tuple[ReadinessBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def evaluate_readiness(packet: WorkPacket) -> ReadinessReport:
    blockers: list[ReadinessBlocker] = []

    if packet.kind in {"change", "program"} and not packet.scope.include:
        blockers.append(
            ReadinessBlocker(
                code="included-scope-empty",
                message=f"{packet.kind} work must name at least one included scope item.",
            )
        )

    for decision in packet.unresolved_decisions:
        blockers.append(
            ReadinessBlocker(
                code=f"decision-unresolved:{decision.id}",
                message=f"{decision.id} is unresolved: {decision.question}",
            )
        )

    for dependency in packet.dependencies:
        if dependency.state == "unsatisfied":
            blockers.append(
                ReadinessBlocker(
                    code=f"dependency-unsatisfied:{dependency.id}",
                    message=f"Dependency {dependency.id} is not satisfied.",
                )
            )
        elif dependency.state == "unknown":
            blockers.append(
                ReadinessBlocker(
                    code=f"dependency-unknown:{dependency.id}",
                    message=f"Dependency {dependency.id} has unknown state.",
                )
            )

    return ReadinessReport(packet=packet, blockers=tuple(blockers))
