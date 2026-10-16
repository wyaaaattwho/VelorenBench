"""Provider presentation only; the original Python native schemas still validate."""
from copy import deepcopy
import json
import math

MAX_SAFE_INTEGER = 2**53 - 1


def provider_schema(native):
    result = deepcopy(native)
    changes = []

    def visit(node, path):
        if isinstance(node, dict):
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
