from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .presentation import render_work_packet
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    raise AssertionError(f"unhandled packet command: {args.packet_command}")
