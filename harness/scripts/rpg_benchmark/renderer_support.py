"""Task admission is separate from interface engineering and mode negotiation."""
from pathlib import Path

from . import output_paths, runtime


def historical_policy() -> dict:
    matrix = {
        "schema": "rpg-task-mode-support/1",
        "renderer": {
            "C1/natural": {
                "structured": {"interface_smoke": True, "scored_evaluation": True,
                               "action_profile": "ordinary-renderer-native-v1"},
                "image": {"interface_smoke": True, "scored_evaluation": False,
                          "action_profile": "visual-motor-v1",
                          "blocker": "crafting visual addressing unavailable"},
                "hybrid": {"interface_smoke": True, "scored_evaluation": True,
                           "action_profile": "ordinary-renderer-native-v1"},
            },
            **{f"{task}/natural": {
                "structured": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1",
                               "qualification": "private native engineering action/effect and oracle checks"},
                "hybrid": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1",
                           "qualification": "private native engineering action/effect and frame checks"},
                "image": {"scored_evaluation": False, "action_profile": "visual-motor-v1",
                          "blocker": "crafting/product/material visual paths unqualified; equipment primitive alone does not qualify this task"},
            } for task in ("T03", "T04")},
            "T05/natural": {
                "structured": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1"},
                "hybrid": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1"},
                "image": {
                    "scored_evaluation": True, "action_profile": "visual-ui-v1",
                    "allowed_run_modes": ["task-engineering"], "accepted": False,
                    "qualification": "fresh integrated034-t05-image-001 ordinary Responses potion use; unchanged goal/oracle; older visual002/030 evidence remains inherited",
                    "legacy_visual_motor_profile": "unsupported for this task; no fallback",
                },
            },
            **{key: {
                "structured": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1",
                               "qualification": "installed ordinary-entry native fixture/action/oracle engineering"},
                "hybrid": {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1",
                           "qualification": "same native task predicates plus actual same-actor PNG delivery"},
                "image": {"scored_evaluation": False, "action_profile": "visual-motor-v1",
                          "blocker": "full visual task navigation/map-targeting/supply paths unqualified; no structured fallback"},
            } for key in ("T01/natural", "T02/flat")},
            **{f"{task}/natural": {
                mode: {"scored_evaluation": True, "action_profile": "ordinary-renderer-native-v1",
                       "qualification": "inherited accepted native combat/support engineering; integrated payload checks are separately identified"}
                for mode in ("structured", "hybrid")
            } | {"image": {"scored_evaluation": False, "action_profile": "visual-motor-v1",
                            "blocker": "target, skill and Group semantic visual addressing unqualified"}}
               for task in ("T06", "T07", "T09")},
            "T08/natural": {
                mode: {"scored_evaluation": True, "task_engineering": True,
                       "action_profile": "ordinary-renderer-native-v1",
                       "qualification": "accepted v2 optional finite stock staff; no mandatory cover/tactic; original witnesses retain original identities"}
                for mode in ("structured", "hybrid")
            } | {"image": {"scored_evaluation": False, "action_profile": "visual-motor-v1",
                          "blocker": "target and combat semantic visual addressing unqualified"}},
            **{f"{task}/natural": {
                mode: {"scored_evaluation": mode != "image", "task_engineering": mode != "image",
                       "action_profile": "visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1",
                       **({"qualification": "inherited accepted exact native exchange/reward joins; fresh integrated checks separately identified"}
                          if mode != "image" else
                          {"blocker": "full native social/trade task visual coverage unqualified"})}
                for mode in ("structured", "hybrid", "image")
            } for task in ("T10", "T11")},
            "T12/natural": {
                mode: {"scored_evaluation": mode != "image", "task_engineering": mode != "image",
                       "action_profile": "visual-motor-v1" if mode == "image" else "ordinary-renderer-native-v1",
                       **({"admission_authority": "A0 D301 scorer-adjudication closeout054",
                           "qualification": "051 native013 success and012 goal evidence;052 installed corrected derived "
                                            "evaluation;054 chronological judge/result replay; NOT052 private-driver qualification"}
                          if mode != "image" else
                          {"blocker": "full native social/combat/return/payout visual coverage unqualified"})}
                for mode in ("structured", "hybrid", "image")
            },
            "N1/flat": {
                "image": {"scored_evaluation": True, "action_profile": "visual-motor-v1",
                          "goal_information": "public geometric rendezvous description plus platform/compass pixels",
                          "qualification": "same original wall/arrival predicates; renderer-specific world/timing"},
            }},
        "other_renderer_combinations": "unsupported-unqualified",
        "headless": {"structured": "unchanged 17 definitions/25 combinations; additional families unqualified",
                     "image": "unsupported", "hybrid": "unsupported"},
        "visual_primitives": {
            "image": {"action_profile": "visual-ui-v1",
                      "qualified_final_native": ["inventory equip selection",
                                                "visible native Talk prompt", "native dialogue choice",
                                                "visible statement continuation", "persistent native workshop marker",
                                                "fresh stock HUD frames"],
                      "final_native_requalification": "fresh integrated034-equipment-image-002, integrated034-dialogue-image-005 and integrated034-namespace-hybrid-001 on integrated034 native; prior visual031 witnesses retain their own native identities",
                      "inherited_consumable_qualification": "sealed visual002/030 remains historical; fresh integrated034-t05-image-001 is separately recorded",
                      "qualification_policy": "disclosed predetermined equipment/dialogue widget gestures; image-only inputs; namespace diagnostics hybrid; synthetic, accepted=false, not autonomous ability",
                      "unsupported": ["crafting", "trade", "skills", "map targeting", "drag", "unequip"],
                      "full_task_implication": "none; T01 image and T04 image remain unsupported"},
            "structured_hybrid": "ordinary-renderer-native-v1 retains structured APIs; visual primitives available only with native-visual-ui/1",
        },
        "qualification": "bounded task-runtime engineering; not a formal ability campaign or world/timing equivalence",
    }
    matrix["qualification_evidence"] = runtime.read_json(Path(__file__).with_name("qualification.json"))
    return matrix


def support_matrix() -> dict:
    from .support import matrix
    return matrix()


def admit_selection(args, selected) -> dict:
    if selected.get("renderer") is None or selected.get("renderer_fixture_helper") is None:
        raise ValueError("renderer episodes require verified renderer and matching-source fixture helper")
    mode = args.observation_mode or "structured"
    from . import lifecycle, qualification, suite, support
    manifest = selected.get("dependencies", {}).get("scene_manifest", {}).get("path")
    definitions = suite.load(Path(manifest)) if manifest else support.catalog()
    if args.scene not in definitions:
        raise ValueError("unknown task definition")
    unqualified = getattr(args, "unqualified_engineering", False)
    native_features = selected["renderer"].get("benchmark_features")
    if unqualified and native_features is None:
        raise ValueError("unsupported renderer: selected native contract does not declare successor features")
    definition = definitions[args.scene]
    suite.runtime_options(definition, variant=getattr(args, "variant", None))
    cell = support.admit(definition, args.terrain, "renderer", mode,
                         getattr(args, "action_profile", None), args.mode,
                         unqualified_engineering=unqualified, native_features=native_features,
                         native_identity=qualification.selected_native(selected),
                         definitions=definitions,
                         task_manifest_sha256=lifecycle.sha(Path(manifest)) if manifest else None)
    if args.recordings is None or (
            args.recordings.parent.name != "recordings" or args.recordings.parent.parent.name != "outputs"):
        raise ValueError("renderer recordings must be an explicit outputs/recordings/RUN-ID path")
    recording_root = args.recordings.parent
    output_paths.validate_root(recording_root)
    output_paths.validate_output(args.recordings, recording_root)
    runtime.exclude_outputs(recording_root, [Path(p) for p in selected["protected_paths"]])
    if args.recordings == args.out or args.out.is_relative_to(args.recordings) or args.recordings.is_relative_to(args.out):
        raise ValueError("recordings and private run outputs must be separate")
    return cell
