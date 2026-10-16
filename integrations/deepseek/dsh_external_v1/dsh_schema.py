"""Provider presentation only; the original Python native schemas still validate."""
from copy import deepcopy
import json
import math
import re

from scripts import rpg_sdk_agent as common

MAX_SAFE_INTEGER = 2**53 - 1
DECIMAL_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)", re.ASCII)


def wide_integer(schema):
    return schema.get("type") == "integer" and (
        schema.get("minimum", -math.inf) < -MAX_SAFE_INTEGER
        or schema.get("maximum", math.inf) > MAX_SAFE_INTEGER)


def native_arguments(schema, value):
    """Decode only schema-declared integer tokens, then enforce native types/ranges.

    A decimal string is a wire representation, not a new native argument type.
    Unsafe numeric inputs remain errors, even if a JS rounding happened to land
    on another valid native identity.
    """
    def decode(node, item):
        if "oneOf" in node or "anyOf" in node:
            union = "oneOf" if "oneOf" in node else "anyOf"
            candidates = []
            for branch in node[union]:
                try:
                    candidate = decode(branch, item)
                    common.validate_schema(candidate, branch)
                except ValueError:
                    continue
                candidates.append(candidate)
            if not candidates or (union == "oneOf" and len(candidates) != 1):
                raise ValueError("Arguments must select a declared native schema branch")
            return candidates[0]
        if node.get("type") == "integer":
            if isinstance(item, str) and wide_integer(node):
                if not DECIMAL_INTEGER.fullmatch(item):
                    raise ValueError("Integer token must be canonical decimal, never rounded")
                return int(item)
            if type(item) is int and abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("Unsafe numeric integer; use an exact decimal string")
        if node.get("type") == "object" and isinstance(item, dict):
            return {key: decode(node["properties"][key], child)
                    if key in node["properties"] else child for key, child in item.items()}
        if node.get("type") == "array" and isinstance(item, list):
            return [decode(node["items"], child) for child in item]
        return item

    result = decode(schema, value)
    common.validate_schema(result, schema)
    return result


def provider_schema(native):
    result = deepcopy(native)
    changes = []
    visited = set()

    def visit(node, path):
        if isinstance(node, dict):
            # Native schemas share identity/fence dictionaries between actions.
            # Project each object once, not the generated integer alternatives.
            if id(node) in visited:
                return
            visited.add(id(node))
            if wide_integer(node):
                # Preserve native integer semantics after Python decoding. Only
                # the provider presentation gains a lossless wire alternative.
                description = node.get("description", "")
                minimum, maximum = node.get("minimum"), node.get("maximum")
                notes = " ".join(
                    f"Native {key} (inclusive): {bound}."
                    for key, bound in (("minimum", minimum), ("maximum", maximum))
                    if bound is not None)
                node.clear()
                node.update({
                    "description": " ".join(filter(None, [
                        description, notes,
                        "Use a canonical decimal string for integers outside JavaScript's "
                        "safe range; Python restores the exact native integer. Unsafe numeric "
                        "integers are refused, never rounded or clamped. Native validation is unchanged.",
                    ])),
                    "oneOf": [{"type": "integer"}, {"type": "string"}],
                })
                changes.append({"path": path, "provider_representation": "safe_integer_or_decimal_string"})
                return
            kind = node.get("type")
            if kind in ("integer", "number"):
                notes = []
                for key in ("minimum", "maximum"):
                    value = node.get(key)
                    integer_bound = kind == "integer" and type(value) is int
                    number_bound = (kind == "number" and type(value) in {int, float}
                                    and math.isfinite(value))
                    if (integer_bound or number_bound) and abs(value) > MAX_SAFE_INTEGER:
                        decimal = json.dumps(value, allow_nan=False)
                        notes.append(f"Native {key} (inclusive): {decimal}.")
                        changes.append({"path": f"{path}/{key}", "native_value": decimal,
                                        "provider_representation": "description"})
                        del node[key]
                if notes:
                    authority = (
                        ["The unchanged native validator enforces this exact integer domain.",
                         "This harness refuses integers not exactly representable in JavaScript; "
                         "never guess, round or clamp an identity."] if kind == "integer" else
                        ["The unchanged native validator enforces this number range and finite values."])
                    node["description"] = " ".join(filter(None, [
                        node.get("description"), *notes,
                        *authority,
                    ]))
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(result, "$")
    return result, changes
