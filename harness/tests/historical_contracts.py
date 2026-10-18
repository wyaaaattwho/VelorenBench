"""Function contracts against the unchanged accepted-contracts.json AST.

Only the enumerated authorized syntax changes below are translated. Every
other node of each original function/class remains an exact historical AST
assertion. In particular, there is no current-file hash allowlist, general
node stripping, name erasure, or fixture rewrite.
"""
import ast
from pathlib import Path
import textwrap


def statements(source):
    return ast.parse(textwrap.dedent(source)).body


def splice(expected, before, after):
    """Replace exactly one explicitly specified contiguous statement sequence."""
    left = ", ".join(ast.dump(node) for node in statements(before))
    right = ", ".join(ast.dump(node) for node in statements(after))
    assert expected.count(left) == 1, before
    return expected.replace(left, right, 1)


def expression(expected, before, after):
    left = ast.dump(ast.parse(before, mode="eval").body)
    right = ast.dump(ast.parse(after, mode="eval").body)
    assert expected.count(left) == 1, before
    return expected.replace(left, right, 1)


def expected_function(module, name, expected):
    if (module, name) == ("mixed.py", "setup"):
        old = ast.dump(ast.arg(arg="suite", annotation=ast.Name(id="dict", ctx=ast.Load())))
        new = ast.dump(ast.arg(arg="dependencies", annotation=ast.Name(id="dict", ctx=ast.Load())))
        assert expected.count(old) == 1
        expected = expected.replace(old, new, 1)
        expected = expression(expected, "suite['C2']", "dependencies['craft']")
        return expression(expected, "suite['B1']", "dependencies['encounter']")
    if (module, name) == ("oracles.py", "pickup_actions"):
        expected = expression(expected, "action.get('uid') == uid",
                              "'uid' not in action or action['uid'] == uid")
        doc = statements('''\
            def holder():
                """Match native operational collection, including optional nearest pickup.

                An omitted request UID is resolved by native; the exact receipt target and
                all collection predicates below remain required. An explicit UID must match.
                """
        ''')[0].body[0]
        # Only this documentation node is new; all actual target/detail/collection
        # predicates remain part of the exact historical comparison.
        return expected.replace("body=[", "body=[" + ast.dump(doc) + ", ", 1)
    if (module, name) == ("setup.py", "clean_inventory"):
        return splice(expected, "opening = await actor.request('observe')",
                      "opening = await ready_observation(actor)")
    if (module, name) == ("setup.py", "author_voxels"):
        # Preserve historical bounds, top coordinate, exact one build submission,
        # result append/journal and returned collection. Replace ordinary
        # administrator standing with explicitly scoped construction/readback.
        return splice(expected, '''
            area = 'benchmark_' + uuid4().hex[:12]
            await command(admin, 'area_add', area, 'build', low[0], high[0], low[1], high[1], low[2], high[2])
            await command(admin, 'permit_build', area)
            await command(admin, 'build')
            await wait_observation(admin, lambda row: (row['character_controls'].get('can_build') or {}).get('enabled') is True, 'administrator-only native build permission')
            results = []
            for point in voxels:
                top = [point[0] + .5, point[1] + .5, point[2] + 1]
                await goto(admin, admin, [top[0], top[1], top[2] + .8], settle=False)
                receipt = await action(admin, 'build_block', operation='place', position=point, expected_kind='Air', new_kind='Rock', color_rgb=[160,160,160])
                await goto(admin, admin, top)
                settled = await ready_observation(admin)
                readback = await terrain(admin, top)
                if not any(row['occupiable'] is True and row['support']['position'] == point and row['support']['kind'] == 'Rock' and row['support']['solid'] is True for row in readback.get('support_candidates', [])):
                    raise RuntimeError(f'explicit build submission lacks exact native Rock readback: {point}')
                result = {'voxel': point, 'receipt': receipt, 'terrain': readback, 'settled_on_top': {key: settled[key] for key in ('position','velocity','on_ground','tick','session_id','self_identity')}}
                results.append(result)
                admin.journal.write(origin='setup', event='explicit_authored_rock_voxel', **result)
        ''', '''
            results = []
            async with fixture_authoring(admin, low, high) as owner:
                anchor = None
                for point in voxels:
                    top = [point[0] + .5, point[1] + .5, point[2] + 1]
                    if anchor is None or math.dist(anchor[:2], top[:2]) > 8 or abs(anchor[2] - top[2]) > 1.5:
                        anchor = [top[0], top[1], top[2] + .8]
                        await command(admin, 'fixture_authoring', owner, 'move', *anchor)
                        await wait_observation(admin, lambda row: (row['character_controls'].get('fixture_authoring') or {}).get('owner') == owner and math.dist(position(row), anchor) < .01, 'stable fixture authoring observation anchor')
                    loaded = await construction_readback(admin, point, phase='loaded-input', owner=owner)
                    receipt = await action(admin, 'build_block', operation='place', position=point, expected_kind='Air', new_kind='Rock', color_rgb=[160,160,160])
                    support = await construction_readback(admin, point, phase='effect', receipt=receipt, owner=owner)
                    result = {'voxel': point, 'receipt': receipt, 'loaded_input': loaded, 'support_before_standing': support, 'terrain': support, 'administrator_hold': {'owner': owner, 'position': list(anchor), 'not_grounded_evidence': True}}
                    results.append(result)
                    admin.journal.write(origin='setup', event='explicit_authored_rock_voxel', **result)
        ''')
    if (module, name) == ("suite.py", "load"):
        expected = splice(expected, '''
            result[definition['id']] = {**definition, 'placements': value['placements'], 'player_equipment': value['player_equipment']}
        ''', '''
            result[definition['id']] = task_templates.configure(MODULES[definition['kind']], {**definition, 'placements': value['placements'], 'player_equipment': value['player_equipment']})
        ''')
        return splice(expected, "return result", '''
            for definition in result.values():
                setup_dependencies(definition, result)
            return result
        ''')
    if (module, name) == ("suite.py", "score"):
        return splice(expected, '''
            return MODULES[definition['kind']].score(fixture, final, receipts, events, samples)
        ''', '''
            from .temporal import event_window
            return MODULES[definition['kind']].score(fixture, final, receipts, event_window(fixture['opening'], final, events), samples)
        ''')
    if (module, name) == ("suite.py", "setup"):
        return splice(expected, '''
            if definition['kind'] == 'mixed':
                return await mixed.setup(admin, actor, definition, terrain, definitions)
            return await MODULES[definition['kind']].setup(admin, actor, definition, terrain)
        ''', '''
            module = MODULES[definition['kind']]
            concrete = task_templates.mechanics(module, definition)
            if getattr(module, 'SETUP_REFERENCES', {}):
                dependencies = setup_dependencies(definition, definitions)
                fixture = await module.setup(admin, actor, concrete, terrain, dependencies)
            elif concrete.get('layout') == 'named_site_long':
                fixture = await navigation.setup_named_site(admin, actor, concrete, start_actor=True)
            else:
                fixture = await module.setup(admin, actor, concrete, terrain)
            fixture.update(task_id=definition['id'], semantic_template=definition['template'], task_content_sha256=task_templates.identity(definition))
            return fixture
        ''')
    if (module, name) == ("run.py", "agent_command"):
        return expression(expected, "str(Path(__file__).with_name('bridge_client.py'))",
                          "str(Path(python).parent / 'rpg-episode-bridge')")
    if (module, name) == ("run.py", "measurement_stop"):
        return expression(expected, "'provider_or_agent_error'", "'entry_exited_without_goal'")
    if (module, name) == ("run.py", "stop_agent"):
        return splice(expected, '''
            lifecycle.assert_owned(process.pid, start)
            record['stop_signal'] = {'signal': 'SIGINT', 'requested_monotonic': time.monotonic(), 'sent': False}
            process.send_signal(signal.SIGINT)
            record['stop_signal']['sent'] = True
        ''', '''
            from .native import signal_owned
            record['stop_signal'] = {'signal': 'SIGINT', 'requested_monotonic': time.monotonic(), 'sent': False, 'clock': 'host-monotonic', 'stop_id': record.get('stop_id'), 'recipient_pid': process.pid, 'recipient_start_ticks': start}
            record['stop_signal']['sent'] = await signal_owned(process, start, signal.SIGINT)
        ''')
    if (module, name) == ("native.py", "stop_owned"):
        expected = splice(expected, '''
            lifecycle.assert_owned(process.pid, start)
            process.send_signal(signal.SIGTERM)
        ''', "await signal_owned(process, start, signal.SIGTERM)")
        return splice(expected, '''
            lifecycle.assert_owned(process.pid, start)
            process.kill()
        ''', "await signal_owned(process, start, signal.SIGKILL)")
    if (module, name) == ("bridge_client.py", "main"):
        # The complete original async relay body, including cancellation/close,
        # is retained, with only its name changed for the sync console wrapper.
        prefix = "AsyncFunctionDef(name='main',"
        assert expected.startswith(prefix)
        return expected.replace(prefix, "AsyncFunctionDef(name='relay',", 1)
    return expected


def check_module(baseline, package, module, names=None, *, tree=None):
    """Check all original nodes unless the caller retains a narrower old set."""
    nodes = (ast.parse((Path(package) / module).read_text()) if tree is None else tree).body
    current = {node.name: node for node in nodes
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    originals = baseline["ast"][module]
    for name in originals if names is None else names:
        selected = "relay" if (module, name) == ("bridge_client.py", "main") else name
        assert ast.dump(current[selected]) == expected_function(module, name, originals[name]), (module, name)
    if module == "bridge_client.py":
        assert ast.dump(current["main"]) == ast.dump(statements('''
            def main() -> None:
                asyncio.run(relay())
        ''')[0])


def check_core(baseline, package):
    from scripts.rpg_benchmark.lifecycle import sha
    assert sha(Path(package) / "scene.py") == baseline["files"]["scene.py"]
    for module in ("scene.py", "suite.py", "setup.py", "crafting.py", "combat.py",
                   "navigation.py", "mixed.py", "oracles.py"):
        check_module(baseline, package, module)
