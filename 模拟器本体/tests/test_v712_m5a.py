# -*- coding: utf-8 -*-
"""v7.12.0 (M5a): 角色调用点收口——结构锁定测试。

三引擎函数（_use_skill/_begin_regular_turn/_apply_skill_effects）char.id 字面量清零、
相位表每局隔离、tick/结算管线保序表与延迟导入计数钉扎。
"""
import ast
import glob
import json
import os

import pytest

from engine.core import combat_engine as cs
from engine.characters import (
    PILOTS, TURN_TICK_ZONE_ORDER, SETTLE_PIPELINE_ORDER, build_phase_tables,
)
from engine.models.character import load_character
from engine.core.attributes import compute_combat_stats

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THREE_FUNCS = ('_use_skill', '_begin_regular_turn', '_apply_skill_effects')
# M5b: _use_skill 拆段后段函数（编排器+7 段; 延迟导入分布其中）
USE_SKILL_PARTS = ('_use_skill', '_us_resolve_skill', '_us_pay_costs', '_us_hp_costs',
                   '_us_skill_hooks', '_us_damage_loop', '_us_heal_effects',
                   '_us_effects_and_tail')


def _roster_ids():
    ids = set()
    for f in glob.glob(os.path.join(REPO, 'data', 'characters', '*.json')):
        if f.endswith('_template.json') or f.endswith('.bak.json'):
            continue
        ids.add(json.load(open(f, encoding='utf-8'))['id'])
    return ids


def _func_source(fn_name):
    with open(cs.__file__, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return node
    raise AssertionError(fn_name)


class TestZeroCharLiterals:
    def test_three_functions_have_no_char_id_literals(self):
        """M5a 验收主断言：三函数（含 M5b 段函数）内不得出现角色名册 id 字符串比较。"""
        roster = _roster_ids()
        targets = THREE_FUNCS + USE_SKILL_PARTS
        hits = []
        for fname in targets:
            node = _func_source(fname)
            for n in ast.walk(node):
                if not isinstance(n, ast.Compare):
                    continue
                for c in n.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                            and c.value in roster:
                        hits.append((fname, n.lineno, c.value))
        assert hits == [], f"残留角色字面量分支: {hits[:10]}"

    def test_deferred_character_import_count_pinned(self):
        """钉扎三函数剩余 characters 延迟导入数（M5b 拆段后按段分布, 合计不变）。"""
        expect = {  # (函数, 期望条数)——合计 _use_skill 系 17, 其余两函数 3/1
            '_use_skill': 0, '_us_resolve_skill': 0, '_us_pay_costs': 2,
            '_us_hp_costs': 3, '_us_skill_hooks': 0, '_us_damage_loop': 8,
            '_us_heal_effects': 1, '_us_effects_and_tail': 3,
            '_begin_regular_turn': 3, '_apply_skill_effects': 1,
        }
        for fname, want in expect.items():
            node = _func_source(fname)
            n = sum(1 for x in ast.walk(node)
                    if isinstance(x, ast.ImportFrom)
                    and x.module and x.module.startswith('engine.characters'))
            assert n == want, (fname, n, want)


class TestOrderLocks:
    def test_turn_tick_zone_order_locked(self):
        assert TURN_TICK_ZONE_ORDER['pre'] == (
            'seele', 'qianye', 'xiadie', 'tribbie', 'hysilens', 'cipher', 'cerydra',
            'sunday', 'ruan_mei', 'robin', 'acheron', 'feixiao', 'anaxa',
            'jierjialameishi', 'archer', 'yuanbanlin')
        assert TURN_TICK_ZONE_ORDER['post_control'] == ('huohuo',)
        assert TURN_TICK_ZONE_ORDER['late'] == ('xilian', 'mydei', 'aglaea')

    def test_settle_pipeline_order_locked(self):
        assert len(SETTLE_PIPELINE_ORDER) == 20
        assert SETTLE_PIPELINE_ORDER[0] == ('tribbie', 'settle_self')
        assert SETTLE_PIPELINE_ORDER[-1] == ('yuanbanlin', 'settle_self')

    def test_module_tick_zones_all_known(self):
        """角色模块注册的 tick 区必须都在保序表内（防静默丢派发）。"""
        for m in PILOTS:
            ticks = getattr(m, 'TURN_TICKS', None) or {}
            for zone in ticks:
                assert zone in TURN_TICK_ZONE_ORDER, (m.CHAR_ID, zone)


class TestPhaseTableIsolation:
    def _mk_state(self, cid, position=1):
        c = load_character(cid, os.path.join(REPO, 'data', 'characters'))
        stats = compute_combat_stats(c, None, None, None)
        u = cs.SimUnit(char=c, base_stats=stats, position=position)
        u.max_hp = u.current_hp = stats.HP
        st = cs.SimState(units=[u])
        st.enemies = [cs.Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                               toughness=200, max_toughness=200, level=80,
                               element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                            '虚数': 0, '物理': 0, '火': 0},
                               attacks=[{'name': 'hit', 'type': 'basic', 'power': 100}])]
        return st, u

    def test_tables_rebuilt_per_state_no_shared_mutable(self):
        st1, _ = self._mk_state('seele')
        st2, _ = self._mk_state('huohuo')
        build_phase_tables(st1)
        build_phase_tables(st2)
        assert st1.char_phases.keys() == {'seele'} or 'seele' in st1.char_phases
        assert 'huohuo' not in st1.char_phases
        assert st1.char_phases is not st2.char_phases
        assert st1._phase_tables_ready and st2._phase_tables_ready

    def test_lazy_bootstrap_on_direct_use_skill(self):
        """直调 _use_skill 的旧测试路径：表未建时惰性自举且不跑 INIT/AI/SKILL_HOOKS。"""
        st, u = self._mk_state('firefly')
        assert not st._phase_tables_ready
        assert st.skill_hooks == {}
        cs._use_skill(u, st, 'basic_attack')
        assert st._phase_tables_ready
        assert 'firefly' in st.char_phases
        assert st.skill_hooks == {}  # 自举不装 SKILL_HOOKS（与旧内联分支语义一致）

    def test_build_ignores_absent_characters(self):
        st, _ = self._mk_state('seele')
        build_phase_tables(st, {'seele', 'tribbie'})  # tribbie 不在场
        assert 'tribbie' not in st.char_phases
        assert all('tribbie' not in fns for fns in st.observer_phases.values())
