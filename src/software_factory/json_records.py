from __future__ import annotations

import json
from typing import cast


class DuplicateJsonMemberError(ValueError):
    """A JSON object contains more than one value for the same member name."""


def parse_json(text: str) -> object:
    return cast(object, json.loads(text, object_pairs_hook=_unique_object))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonMemberError(f"duplicate JSON member: {key}")
        result[key] = value
    return result
