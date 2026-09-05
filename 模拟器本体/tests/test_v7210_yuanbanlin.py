"""v7.21.0 批4: 远坂凛录入测试（yuanbanlin, 拼音 id 替换英文壳 rin_tohsaka）。

白值按项目主 2026-09-06 提供（1048/698/460/102, 能量160常规——网页 wiki 数据有误）。
机制面: 宝石能量（SP 变动 1:1、行迹3/E6/秘技）/ 天赋暴伤电池（消耗/恢复 SP 的单位
+70% 2回合, E4 凛叠2层）/ 战技强化判定（≥15 宝石或 SP≥7）与第二魔法实验（全体90%+
3宝石/轮弹射≤33、SP 耗至2转化、E1 影子宝石模式）/ 终结技易伤+行迹3+E6 额外回合 /
自在远坂流（Archer 连携, 每凛回合1次）/ 行迹1 SP上限+2+ATK150%+量子穿（Archer 同享）。
"""
import pytest

from engine.core.combat_engine import simulate
from engine.models.enemy import Enemy
from engine.models.character import load_character
from engine.characters.yuanbanlin import (
    _rin_gems_gain, _rin_can_enhance, _rin_joint_attack,
)
from tests.helpers import _unit as make_unit


def _rin(eidolon=0):
    return make_unit('yuanbanlin', eidolon=eidolon)


def _enemy_obj():
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80, element_res={'量子': 0})


def _sim(eidolon=0, max_av=2000, mate=None):
    team = [{"char": load_character('yuanbanlin', 'data/characters'),
             "position": 1, "eidolon": eidolon}]
    if mate:
        team.append({"char": load_character(mate, 'data/characters'),
                     "position": 2, "eidolon": 0})
    return simulate(team, _enemy_obj(), max_av=max_av)


def _log(s):
    return '\n'.join(s.log)


class TestGemsAndTalent:
    def test_sp_change_grants_gems_and_cd_buff(self):
        from engine.core.combat_engine import _deduct_skill_point_cost
        from engine.runtime import SimState
        rin = _rin()
        mate = make_unit('huohuo')
        state = SimState(enemies=[], units=[rin, mate])
        state.skill_points = 5
        state.max_sp = 9  # 行迹1 场景
        ok = _deduct_skill_point_cost(state, mate, 2)
        assert ok
        assert rin.extra.get('rin_gems') == 2  # 消耗2点→+2宝石
        assert any(b.param_id == 'rin_talent_cd' for b in mate.buffs)  # 消耗者吃暴伤
        assert next(b for b in mate.buffs
                    if b.param_id == 'rin_talent_cd').attributes['CRIT_DMG'] == 70.0

    def test_enhance_condition(self):
        from engine.runtime import SimState
        rin = _rin()
        state = SimState(enemies=[], units=[rin])
        state.skill_points = 5
        assert not _rin_can_enhance(rin, state)
        _rin_gems_gain(rin, 15)
        assert _rin_can_enhance(rin, state)
        rin.extra.pop('rin_gems')
        state.skill_points = 7
        assert _rin_can_enhance(rin, state)


class TestExperiment:
    def test_experiment_bounces_and_sp_conversion(self):
        from engine.characters.yuanbanlin import _rin_skill_experiment_cast
        from engine.runtime import SimState
        rin = _rin()
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        state.skill_points = 5
        state.max_sp = 9
        _rin_gems_gain(rin, 12)
        _rin_skill_experiment_cast(state, rin)
        # SP 5→2 转化 +6 宝石 = 18 → 弹射 6 轮(耗18)
        assert state.skill_points == 2
        assert rin.extra.get('rin_gems', 0) == 0
        assert '第二魔法实验' in _log(state)

    def test_e1_shadow_mode(self):
        from engine.characters.yuanbanlin import _rin_skill_experiment_cast
        from engine.runtime import SimState
        rin = _rin(eidolon=1)
        rin.extra['rin_shadow_gems'] = 9
        rin.extra['rin_gems'] = 20
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        state.skill_points = 5
        state.max_sp = 9
        _rin_skill_experiment_cast(state, rin)
        assert 'rin_shadow_gems' not in rin.extra  # 影子耗尽
        assert rin.extra.get('rin_gems') == 20  # 不耗宝石
        assert state.skill_points == 5  # 不转化 SP

    def test_e1_shadow_gain_at_30(self):
        from engine.characters.yuanbanlin import _rin_skill_experiment_cast
        from engine.runtime import SimState
        rin = _rin(eidolon=1)
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        state.skill_points = 2
        state.max_sp = 9
        _rin_gems_gain(rin, 33)  # 11轮×3=33 ≥30
        _rin_skill_experiment_cast(state, rin)
        assert rin.extra.get('rin_shadow_gems') == 33


class TestUltAndJoint:
    def test_ult_gems_and_vuln(self):
        from engine.characters.yuanbanlin import _rin_ult_cast
        from engine.runtime import SimState
        rin = _rin()
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        _rin_ult_cast(state, rin)
        assert rin.extra.get('rin_gems') == 13  # 行迹3 12 + 自给1SP联动+1(直调无开战20)
        assert any(s.id == 'rin_ult_vuln' for s in state.enemies[0].statuses)
        assert state.skill_points == 4  # 初始3 + 终结技自给1

    def test_e6_ult_extra_turn_and_gems(self):
        from engine.characters.yuanbanlin import _rin_ult_cast
        from engine.runtime import SimState
        rin = _rin(eidolon=6)
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        _rin_ult_cast(state, rin)
        assert rin.extra.get('rin_gems') == 37  # 行迹3 12 + E6 24 + 自给1SP
        assert any(x is rin for x, k in state.extra.get('extra_turns', []))

    def test_joint_attack_requires_archer(self):
        from engine.runtime import SimState
        rin = _rin()
        state = SimState(enemies=[_enemy_obj()], units=[rin])
        _rin_joint_attack(state)  # 无 Archer → 静默
        archer = make_unit('archer')
        state.units.append(archer)
        state.max_sp = 9
        state.skill_points = 2
        _rin_joint_attack(state)
        assert '自在远坂流' in _log(state)
        assert state.skill_points == 6  # +4
        assert rin.extra.get('rin_joint_used')  # 每凛回合1次
        _rin_joint_attack(state)  # 已用 → 不再触发

    def test_settle_after_archer_skill_triggers(self):
        from engine.characters.yuanbanlin import _rin_settle_self
        from engine.runtime import SimState
        rin = _rin()
        archer = make_unit('archer')
        state = SimState(enemies=[_enemy_obj()], units=[rin, archer])
        state.skill_points = 3  # ≤3 → 触发条件
        _rin_settle_self(archer, state, None, 'skill', 0)
        assert '自在远坂流' in _log(state)
        state.skill_points = 5
        rin.extra.pop('rin_joint_used', None)
        _rin_settle_self(archer, state, None, 'skill', 0)  # SP>3 且回路未满5 → 不触发
        assert _log(state).count('自在远坂流') == 1


class TestInit:
    def test_trace1_buffs_and_sp_cap(self):
        from engine.core.combat_engine import _setup_battle
        state, _ = _setup_battle(
            [{"char": load_character('yuanbanlin', 'data/characters'),
              "position": 1, "eidolon": 0},
             {"char": load_character('archer', 'data/characters'),
              "position": 2, "eidolon": 0}], _enemy_obj(), 1000, 1, None)
        rin = next(x for x in state.units if x.char.id == 'yuanbanlin')
        archer = next(x for x in state.units if x.char.id == 'archer')
        assert state.max_sp == 9  # 5+2(凛)+2(Archer)
        assert rin.base_stats.ATK == pytest.approx(698 * (1 + 0.18 + 1.50))  # 白值×(行迹+150%)
        assert archer.base_stats.ATK > archer.base_stats._base_ATK  # E... 行迹1 同享
        assert any(b.param_id == 'rin_t2_spd' for b in rin.buffs)  # 行迹2
        assert rin.extra.get('rin_gems') == 20  # 开战初值(项目主裁决)


class TestSimulation:
    def test_pair_battle_full_mechanics(self):
        s = _sim(max_av=2500, mate='archer')
        log = _log(s)
        assert '宝石能量' in log
        assert '宝石魔术' in log or 'rin_talent_cd' in log or 'SP变动' in log
        assert s.units[0].total_damage_dealt > 0

    def test_e6_long_window_experiment(self):
        s = _sim(eidolon=6, max_av=3000, mate='huohuo')
        assert '第二魔法实验' in _log(s)
