"""Public visual goal wording derived only from the preserved authored task."""
from . import layouts

N1_VISUAL_GOAL = (
    "Visual rendezvous description: on this rectangular stone platform, the destination is "
    "centered north/south and 2.5 metres west of the platform's eastern edge. The authored "
    "platform is 17 metres east/west and 13 metres north/south; its blocks are one metre. "
    "The ordinary minimap shows compass directions. Come to rest at that location alive. "
    "This describes the same rendezvous as the coordinates above, not a route. "
    "Debug coordinate readouts are disabled in the ordinary agent renderer."
)


def public_task(definition, fixture, mode):
    from . import navigation
    template = definition.get("template", navigation.LEGACY_TEMPLATES.get(definition["id"]))
    if template != "wall_rendezvous" or mode != "image":
        return fixture["task"]
    expected = layouts.scene("wall_side_detour")
    if fixture["layout"]["geometry_sha256"] != expected["geometry_sha256"]:
        raise ValueError("N1 visual description requires its unchanged authored geometry")
    return fixture["task"] + "\n\n" + N1_VISUAL_GOAL
