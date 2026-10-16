"""Ordinary structured RPG selections; native readback remains authoritative."""
NAMES = {"context_interact", "dialogue_action", "social_action", "trade_action",
         "unlock_skill", "configure_auxiliary_ability"}


def fields(*, object_schema, integer, string, entity_ref):
    typed = {"anyOf": [string(), object_schema(
        "Exact native typed variant copied from the current ordinary observation.", additional=True)]}
    identity = {"anyOf": [integer(), object_schema(
        "Exact native dialogue ID copied unchanged from observation.", additional=True)]}
    return {
        "context_interact": (["operation", "target"], {
            "operation": {"type": "string", "enum": ["talk", "help_downed"]}, "target": entity_ref}),
        "dialogue_action": (["operation", "sender_uid", "dialogue_id", "tag"], {
            "operation": {"type": "string", "enum": ["ack", "respond"]},
            "sender_uid": integer(1), "dialogue_id": identity,
            "tag": integer(0, 2**32 - 1), "response_id": integer(0, 2**32 - 1)}),
        "social_action": (["operation"], {
            "operation": {"type": "string", "enum": [
                "invite_trade", "invite_group", "respond_accept", "respond_decline"]},
            "target": entity_ref, "target_uid": integer(1),
            "target_alias": string(), "inviter_uid": integer(1),
            "invite_kind": {"type": "string", "enum": ["Trade", "Group"]}}),
        "trade_action": (["operation", "trade_id", "counterparty_uid", "phase"], {
            "operation": {"type": "string", "enum": ["add_item", "remove_item", "accept", "decline"]},
            "trade_id": integer(), "counterparty_uid": integer(1),
            "phase": {"type": "string", "enum": ["Mutate", "Review", "Complete"]},
            "slot": integer(0, 2**32 - 1), "quantity": integer(1, 2**32 - 1),
            "ours": {"type": "boolean"}}),
        "unlock_skill": (["skill"], {"skill": typed}),
        "configure_auxiliary_ability": (["slot", "ability"], {
            "slot": integer(0, 2**32 - 1), "ability": typed}),
    }


def validate(action):
    kind, operation = action["type"], action.get("operation")
    if kind == "dialogue_action" and (("response_id" in action) != (operation == "respond")):
        raise ValueError("response_id is required only for an exact offered dialogue response")
    if kind == "social_action":
        selected = set(action) - {"type", "action_id", "operation"}
        expected = ({"inviter_uid", "invite_kind"} if operation.startswith("respond_") else
                    {"target"} if operation == "invite_trade" and "target" in action else
                    {"target_uid", "target_alias"})
        if selected != expected:
            raise ValueError("social selection must use exactly the observed invitation or target identity")
    if kind == "trade_action":
        mutation = operation in {"add_item", "remove_item"}
        if mutation:
            if not {"slot", "quantity"} <= action.keys() or action["phase"] != "Mutate":
                raise ValueError("trade item mutation requires exact slot/quantity and observed Mutate phase")
        elif {"slot", "quantity", "ours"} & action.keys():
            raise ValueError("trade accept/decline accepts no item mutation fields")
