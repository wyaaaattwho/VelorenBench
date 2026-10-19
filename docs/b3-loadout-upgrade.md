# B3 player loadout upgrade

B3's hostile Male Ogre and native Ogre hammer are unchanged. The initial player
mainhand is now the native Epic two-handed **Frost Saw**
(`common.items.weapons.sword.frost-1`), with **3 minor potions**
(`common.items.consumable.potion_minor`). All other starter equipment, the
100-HP player opening, cleared skills, full durability, task goal and combat
scorer remain unchanged. B1, B2 and the other 26 scene definitions are unchanged.

The researcher requested the stronger player weapon because the starter sword
was impractical against the armored 320-HP Ogre. This is an intentional B3
initial-condition change, not a scorer relaxation or a model-calibrated
difficulty claim. Earlier B3 receipts and screenshots describe the old initial
loadout; they are not fresh evidence for this one.

Setup equips the declared mainhand after placement, removes the displaced
starter sword with the native inventory cleanup/repair operation, and only then
gifts the three potions. Opening checks require exactly that equipment and
consumable inventory. Omitted `mainhand` still means the starter sword.

`current-qualification.json` keys are refreshed for the changed harness code
and B3 content; evidence citations, statuses and permitted run modes are not
rewritten. This key refresh does **not** claim a new live qualification,
gameplay success, or release acceptance. All 29 tasks' current keys are checked
for structured, hybrid and image modes, including existing variants.

The separate `qualification.json` is a historical `rpg-release-qualification/1`
artifact, not a current engineering registry. It has no `records`,
`harness_code_sha256`, or `task_content_sha256` fields and remains byte-identical.
The current-registry `qualification.validate()` intentionally rejects that
historical schema; replacing it to pass that validator would destroy its
meaning.

The preservation tests now explicitly recognize the researcher-approved B3
loadout and pin the upgraded catalog and setup code. Reversing only the three
approved B3 fields in memory must recover the exact pre-upgrade catalog SHA-256;
all 28 other scenes, global fields, remaining B3 fields and historical scorer
identities remain strict. Historical fixtures and qualification records are not
rewritten.
No live experiment, native build, cloud push, or replacement of sealed release
artifacts is part of this local change. New runtime/content bindings, release
packages and evidence-sidecar synchronization require the release coordinator
before deployment; old bindings must not be presented as matching these edits.
