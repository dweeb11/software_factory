"""Protocol-centered software factory."""

from .preflight import ExecutionEnvironment, PreflightReport, evaluate_preflight
from .readiness import ReadinessReport, evaluate_readiness
from .runs import RunError, RunRecord, initialize_run
from .work_packets import AuthorityEnvelope, PacketError, WorkPacket

__all__ = [
    "AuthorityEnvelope",
    "ExecutionEnvironment",
    "PacketError",
    "PreflightReport",
    "ReadinessReport",
    "RunError",
    "RunRecord",
    "WorkPacket",
    "evaluate_preflight",
    "evaluate_readiness",
    "initialize_run",
]
