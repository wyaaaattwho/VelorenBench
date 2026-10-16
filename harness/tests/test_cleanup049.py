"""The actual cleanup entry must not capture a moving teleport origin."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts.rpg_benchmark import setup, npc_contracts


def test_cleanup_waits_for_grounded_neutral_velocity_before_capturing_teleport_origin():
    moving = {"position": [1040, 2802, 242], "velocity": [-2.0966, 3.6367, 0],
              "on_ground": True, "inventory": {"items": []}}
    airborne = {**moving, "position": [1039.8, 2802.2, 243], "velocity": [0, 0, -2], "on_ground": False}
    stopped = {**moving, "position": [1039.66, 2802.29, 242], "velocity": [0, 0, 0]}
    observations = iter([moving, airborne, stopped])

    async def observe(command):
        assert command == "observe"
        return next(observations, stopped)

    actor = SimpleNamespace(request=observe, name="owned-test")
    gotos = []

    async def goto(admin, selected, point, **kwargs):
        assert selected is actor
        gotos.append((point, kwargs))
        return stopped

    with patch.object(setup, "goto", side_effect=goto), patch.object(setup, "command", AsyncMock()), \
            patch.object(setup.asyncio, "sleep", AsyncMock()):
        receipt = asyncio.run(npc_contracts.stationary_cleanup(object(), actor))
    assert receipt["before"] == stopped
    assert gotos == [
        ([1139.66, 2902.29, 342], {"settle": False}),
        ([1039.66, 2802.29, 242], {})]
