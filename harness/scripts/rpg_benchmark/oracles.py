import math

from scripts.rpg_benchmark.scene import item_id


def pickup_actions(opening: dict, receipts: list[dict], uid: int, item: str,
                   amount: int = 1) -> list[str]:
    selected = []
    for row in receipts:
        action = row["request"].get("args", {}).get("action", {})
        response = row["response"]
        receipt = response.get("result") or {}
        detail = receipt.get("detail") or {}
        feedback = detail.get("pickup_feedback") or {}
        collected = feedback.get("collected_item") or {}
        if (action.get("type") == "pickup" and action.get("uid") == uid
                and row["actor"] == opening["self_identity"]["uid"]
                and response.get("session_id") == opening["session_id"]
                and response.get("ok") is True and receipt.get("status") == "completed"
                and receipt.get("action_id") == action.get("action_id")
                and receipt.get("action_type") == "pickup" and detail.get("uid") == uid
                and detail.get("operational_success") is True
                and feedback.get("server_event") == "Collected"
                and feedback.get("operational_collection_justified") is True
                and feedback.get("candidate_request_count") == 1
                and item_id(collected) == item and collected.get("amount", 0) >= amount):
            selected.append(action["action_id"])
    return selected


def target_validity(fixture: dict, observation: dict, receipts: list[dict],
                    supplies: list[dict]) -> dict:
    supplies = [row for row in supplies if row["required"]]
    if not supplies:
        return {"valid": True, "confirmed_available": True, "uncertain": False, "required_targets": []}
    opening = fixture["opening"]
    clock = observation["survival"]["buff_clock"]["now"]
    if type(clock) not in (int, float) or not math.isfinite(clock):
        raise ValueError("required-target validity needs a finite native simulation clock")
    nearby = {row["uid"]: row for row in observation["nearby_entities"]}
    states = []
    for supply in supplies:
        collected = pickup_actions(opening, receipts, supply["uid"], supply["item"], supply["amount"])
        visible = nearby.get(supply["uid"])
        missing_receipts = []
        for row in receipts:
            action = row["request"].get("args", {}).get("action") or {}
            response = row["response"]
            error = response.get("error") or {}
            if (row["actor"] == opening["self_identity"]["uid"]
                    and response.get("session_id") == opening["session_id"]
                    and action.get("type") == "pickup" and action.get("uid") == supply["uid"]
                    and response.get("ok") is False and error.get("code") == "invalid_action"
                    and error.get("message") in (
                        f"uid {supply['uid']} not found", f"uid {supply['uid']} is not a pickup item")):
                missing_receipts.append(action["action_id"])
        expired_window = clock >= supply["expiry_game_time_estimate"]
        present = (visible is not None and item_id(visible.get("pickup") or {}) == supply["item"]
                   and visible["pickup"]["amount"] >= supply["amount"])
        status = ("collected_by_actor" if collected else "observed_present" if present else
                  "native_target_unavailable" if missing_receipts else
                  "expiry_possible" if expired_window else "not_visible_unconfirmed")
        states.append({"uid": supply["uid"], "item": supply["item"], "status": status,
                       "valid": bool(collected) or present or not missing_receipts,
                       "confirmed_available": bool(collected) or present,
                       "uncertain": not (collected or present or missing_receipts),
                       "pickup_action_ids": collected, "missing_target_action_ids": missing_receipts,
                       "native_tick": observation["tick"], "simulation_time_observed": clock,
                       "estimated_expiry_window_reached": expired_window,
                       "expiry_possible": expired_window and not (collected or present),
                       "limit": ("client-time lifetime estimate and local absence are not ECS deletion proof; "
                                 "a native pickup refusal establishes actor-interface unavailability, not exact expiry")})
    return {"valid": all(row["valid"] for row in states),
            "confirmed_available": all(row["confirmed_available"] for row in states),
            "uncertain": any(row["uncertain"] for row in states), "required_targets": states}
