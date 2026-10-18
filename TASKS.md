# Task definitions

The generated successor template/requirements catalog is at the end of this
file. Historical v1.0.1 definitions, authored goals and byte fixtures below are
preserved; source implementation is not current-native qualification.
Current1.1.0 packages125 evidence-backed qualified selections and35 explicit
legacy-image refusals, without altering these29 goals/37 terrain cells.
Full-UI image has40 selections including weapon variants; qualification is
engineering evidence, not a required task victory. Use native-bound support,
not the historical admission prose below, for current run modes.

The installed `rpg-benchmark tasks` command and
[`scenes.json`](harness/scripts/rpg_benchmark/scenes.json) are authoritative.
The 17 originals retain 25 terrain combinations; the 12 additions bring the
catalog to 29 definitions and 37 raw combinations. All raw definitions and
goals are preserved from locally frozen 1.0.0/build001. Source release v1.0.1
does not change them or establish full-catalog qualification. Historical structured/hybrid admission for
the additions and limited image support, and current qualification, are
distinguished in [SUPPORT.md](SUPPORT.md).
T06 weapon conditions are not separate tasks. No table entry implies support
on every backend or modality.

| ID | Family | Terrains | Player goal / description |
| --- | --- | --- | --- |
| T01 | navigation_tasks | natural | In Liron, ask an actual resident for directions to a workshop, receive the resident's workshop information, then find and enter that real workshop. Settle alive in its CraftingBench area: within one metre horizontally and half a metre vertically of the floor point two metres north of the bench centre. Be on the ground at speed at most 0.1 m/s for two observations at least 0.25 seconds apart. The normal resident dialogue and received map marker provide the missing location information; the initial goal does not supply workshop coordinates. Choose your own resident, route and ordinary tools. |
| T02 | navigation_tasks | flat | Collect the designated loose apple supply and carry at least one apple to the public destination alive. This private benchmark world includes an authored route-change event: after your actual apple pickup is confirmed, the central stair passage closes with real stone collision blocks. This is a benchmark-authored event, not a natural Veloren collapse. A legal north-side stair alternative remains. Choose any legal route, including choosing an alternative early; neither hitting the closure nor subjective replanning is required. Settle on the ground within one metre horizontally and half a metre vertically of the destination, at speed at most 0.1 m/s for two observations at least 0.25 seconds apart, retaining the apple. |
| T03 | crafting_recovery | natural | Learn the supplied food recipe collection and acquire mushrooms from the nearby loose mushroom supply. Make and keep one mushroom stick (common.items.food.mushroom_stick), while keeping at least one uncooked mushroom, one twig and one apple in your bag as backup ingredients. You start with partial ingredients; the food recipes include alternatives sharing these ingredients. Choose your own approach using normal game information. Stay alive. |
| T04 | crafting_recovery | natural | From your raw animal hides, make a rawhide chest armor (common.items.armor.hide.rawhide.chest) and actually wear it in your chest slot. Keep your replaced worker shirt (common.items.armor.misc.chest.worker_purple_brown) in your bag as backup clothing. Useful lawful workstations are nearby. You have no premade leather or chest armor. Choose your own stages and approach from normal recipe and game information; stay alive. |
| T05 | crafting_recovery | natural | You start injured in a safe area at 35/100 health, without residual buffs. Use actual recovery consumables to recover to at least 80/100 health (80 percent), while keeping at least two minor potions (common.items.consumable.potion_minor) in your bag as backup stock. You start with three minor potions. Effects may be delayed and containers may be returned; observe your health and supplies and choose your own recovery approach. Stay alive. |
| C1 | crafting | flat, natural | Make and keep one mushroom stick for food (common.items.food.mushroom_stick). Your character knows the recipe and has sufficient materials. Choose your own tools and approach using the game's current observations. Keep the product. |
| C2 | crafting | flat, natural | Make and keep one mushroom stick for food (common.items.food.mushroom_stick). You know the food recipes, but have only partial supplies. Find what you need in the environment using normal observation and keep the product. |
| C3 | crafting | flat, natural | Make and keep one mushroom stick for food (common.items.food.mushroom_stick). Choose your own tools and approach using current game information. |
| C3U | crafting | flat, natural | Make and keep one mushroom stick for food (common.items.food.mushroom_stick). Your supplies include a food recipe collection, but your character has not learned it. Choose your own tools and approach using normal game information. |
| C4 | crafting | flat, natural | Make and keep a rawhide chest armor (common.items.armor.hide.rawhide.chest) from your raw supplies. Useful crafting stations are in the surrounding environment. Choose your own approach. |
| B1 | combat | flat, natural | Defeat the hostile Green Legoom in this encounter and survive. Choose your own combat tools, positioning and recovery. |
| B2 | combat | flat, natural | Defeat the hostile Wolf in this encounter and survive. Choose your own combat tools, positioning and recovery. |
| B3 | combat | flat, natural | Defeat the hostile male Ogre in this encounter and survive. Choose your own combat tools, positioning and recovery. |
| N1 | navigation | flat | Fixture-generated navigation goal; layout wall_side_detour. Reach the ordinary player-visible rendezvous in the authored layout. |
| N2 | navigation | flat | Fixture-generated navigation goal; layout staggered_s_turn. Reach the ordinary player-visible rendezvous in the authored layout. |
| N3 | navigation | flat | Fixture-generated navigation goal; layout elevation_side_steps. Reach the ordinary player-visible rendezvous in the authored layout. |
| N4 | navigation | flat | Fixture-generated navigation goal; layout long_distance_windows. Reach the ordinary player-visible rendezvous in the authored layout. |
| N5 | navigation | natural | Fixture-generated navigation goal; layout natural_long. explicit player-visible location target, not named-site localization; pickup exact native apple at destination |
| N6 | navigation | flat | Fixture-generated navigation goal; layout recovery. explicit location target after a pre-measurement native displacement; macro goal retained |
| N7 | navigation | natural | Fixture-generated navigation goal; layout named_site_long. real native town label from normal known_sites/InspectMap; no coordinates or route in task |
| X1 | mixed | natural | Prepare and keep a mushroom stick, reach the marked rendezvous and collect its apple, while surviving and resolving the hostile Green Legoom encounter. Decide how to combine the work yourself. |
| X2 | mixed | natural | Establish food readiness at the rendezvous: keep an apple stick (common.items.food.apple_stick), keep the rendezvous apple, and survive after defeating the hostile Green Legoom. Decide your own stages, resource choices, route and recovery while retaining this overall goal. |
| T06 | combat_support | natural | Resolve the authored encounter using your current weapon/build: the designated hostile Green Legoom must actually die, you must make a real damaging contribution to that enemy, and you must remain alive. Choose your own tactics, actions and recovery; no particular attack, order or last blow is required. Sword, bow, staff and sceptre are four configured conditions of this one task, not four professions or presumed equal difficulty. |
| T07 | combat_support | natural | Use your finite native skill points to learn an active sword build, configure at least one ability you learned into an auxiliary slot, and use a configured learned active ability to cause an actual damaging effect on the designated hostile Green Legoom. Resolve the encounter by its actual death with your own damaging contribution, and survive. The Sword group is already unlocked; you have four Sword points and no unspent General points, actual native prerequisites and multiple legal active choices. Normal skill information explains the available choices and costs. No single build or tactic is prescribed, and basic attacks may also contribute to victory. |
| T08 | combat_support | natural | Resolve both simultaneous hostile threats in this encounter: the designated melee Green Legoom and the designated ranged Haniwa Archer must actually die, you must make a real damaging contribution to each, and you must remain alive. The archer has its stock native weapon/armor/skills with a disclosed finite setup injury to 25 HP. The native terrain includes solid rock cover, a raised block and open flanks, with real collision and projectiles. Your inventory also contains one stock starter staff; equipping or using it is optional. Choose your own target order, spacing, recovery and tactics. Cover use is optional; suppression and luring are legal, and success alone is not evidence that you used spatial tactics. |
| T09 | combat_support | natural | Support your genuinely native-Group companion with your sceptre: you must cause actual healing to that other Group member, the companion must reach at least 61/100 health from its 60/100 injured start, the designated hostile Green Legoom must actually die, and both you and the companion must remain alive. You begin with four finite combo from a disclosed expired setup resource buff; setup does not count as healing. You may gain further resources normally. Healing yourself or using self potions is not companion healing; no last blow by you is required. Choose your own support/combat approach. |
| T10 | npc_contracts | natural | Sell some surplus materials to the named local merchant, buy the specified stocked equipment from that merchant, and actually wear it in its appropriate equipment slot. Keep the stated coin and material reserves in your bag and stay alive. The exact merchant, equipment and reserves are stated in your opening goal; normal merchant stock, native prices and bilateral offers are available through ordinary trade. Choose any legal payment combination satisfying these conditions. |
| T11 | npc_contracts | natural | Accept the offered message-courier contract through normal dialogue. Remember its identity and place clues, find the correct recipient among the local people, deliver the message and receive the actual native reward in your bag. Stay alive. This is a Message contract, not an item-delivery alternative; choose your own conversations and route. |
| T12 | npc_contracts | natural | Accept the offered native Slay contract through normal dialogue. Use its normal clues or marker to find the contracted monster, defeat that actual target with your own valid combat contribution, and return alive to the quest giver to report and receive the actual reward in your bag. Choose your own route, equipment, abilities and tactics; no particular build or last blow is required. |

<!-- BEGIN GENERATED SUCCESSOR -->
## Task catalog (generated)

The packaged scored catalog remains 29 definitions / 37 authored terrain cells.
The separate arbitrary-ID example is not silently included.

| Task | Family | Semantic template | Terrain | Requirements |
| --- | --- | --- | --- | --- |
| T01 | navigation_tasks | resident_workshop | natural | dialogue, map, movement |
| T02 | navigation_tasks | changing_route | flat | coordinate_goal, movement, pickup, terrain_event |
| T03 | crafting_recovery | food_reserve | natural | crafting, inventory, movement, pickup |
| T04 | crafting_recovery | craft_equip | natural | crafting, equipment, inventory, movement |
| T05 | crafting_recovery | recover_health | natural | inventory |
| C1 | crafting | starter_food | flat, natural | crafting, inventory, movement |
| C2 | crafting | gather_food | flat, natural | crafting, inventory, movement, pickup |
| C3 | crafting | known_food | flat, natural | crafting, inventory, movement |
| C3U | crafting | learn_food | flat, natural | crafting, inventory, movement |
| C4 | crafting | leather_chain | flat, natural | crafting, inventory, movement |
| B1 | combat | single_creature | flat, natural | combat, inventory, movement |
| B2 | combat | single_creature | flat, natural | combat, inventory, movement |
| B3 | combat | single_creature | flat, natural | combat, inventory, movement |
| N1 | navigation | wall_rendezvous | flat | coordinate_goal, movement |
| N2 | navigation | supply_rendezvous | flat | coordinate_goal, movement |
| N3 | navigation | terrain_rendezvous | flat | coordinate_goal, movement |
| N4 | navigation | terrain_rendezvous | flat | coordinate_goal, movement |
| N5 | navigation | pickup_rendezvous | natural | coordinate_goal, movement, pickup |
| N6 | navigation | terrain_rendezvous | flat | coordinate_goal, movement |
| N7 | navigation | named_site | natural | map, movement |
| X1 | mixed | craft_fight_travel | natural | combat, coordinate_goal, crafting, movement, pickup |
| X2 | mixed | calibrated_craft_fight_travel | natural | combat, coordinate_goal, crafting, movement, pickup |
| T06 | combat_support | weapon_condition | natural | combat, equipment, movement |
| T07 | combat_support | skill_training | natural | combat, movement, skills |
| T08 | combat_support | ranged_encounter | natural | combat, inventory, movement |
| T09 | combat_support | group_support | natural | combat, movement, skills |
| T10 | npc_contracts | resident_exchange | natural | dialogue, movement, trade |
| T11 | npc_contracts | message_contract | natural | dialogue, map, movement |
| T12 | npc_contracts | slay_contract | natural | combat, dialogue, map, movement |
<!-- END GENERATED SUCCESSOR -->
