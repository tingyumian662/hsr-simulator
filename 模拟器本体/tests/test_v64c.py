"""v6.4c 回归: 精英双动 + 冻结回合tick（项目主 2026-08-14 确认实机语义）

语义依据: 实机精英怪每回合行动两次（同一回合, 行动间玩家可终结技打断;
行动一上的 self_buff 行动二立即吃到）; 敌方被冻结跳过的回合 buff/debuff 照算倒计时;
破韧打断第二行动口径A（取消第二行动, 推条作用于下回合）。"""
import re

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus, load_enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, simulate, _begin_enemy_turn, _enemy_pending_step,
)


def _enemy(**kw):
    base = dict(id='x', name='X', HP=500000, ATK=600, DEF=800, SPD=80,
                toughness=200, max_toughness=200, level=80,
                element_res={k: 0 for k in ['冰', '量子', '风', '雷', '虚数', '物理', '火']},
                actions_per_turn=1)
    base.update(kw)
    return Enemy(**base)


def _unit(cid='seele', position=1):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    return u


CHARGE_ATTACKS = [
    {'name': '普通', 'element': '物理', 'damage_type': 'direct',
     'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1},
    {'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
     'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 4,
     'self_buffs': [{'id': 'rage', 'name': '狂暴',
                     'attributes': {'ATK_PERCENT': 0.5}, 'duration': 1}]},
    {'name': '狂暴挥击', 'element': '物理', 'damage_type': 'direct',
     'multiplier': 150.0, 'target_type': 'single_enemy', 'priority': 5,
     'requires_buff': 'rage'},
]


class TestEliteDoubleAction:
    def test_second_action_same_turn_consumes_buff(self):
        """精英双动: 行动一=蓄力挂狂暴, 行动二=同回合狂暴挥击吃到"""
        u = _unit()
        e = _enemy(actions_per_turn=2, attacks=CHARGE_ATTACKS)
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        _begin_enemy_turn(state, e)
        assert state.extra.get('enemy_pending_action') is e
        assert e.extra['_actions_left'] == 1
        assert any(s.id == 'rage' for s in e.statuses)
        assert '蓄力' in '\n'.join(state.log)
        _enemy_pending_step(state)
        log = '\n'.join(state.log)
        assert '狂暴挥击' in log
        assert state.extra.get('enemy_pending_action') is None
        assert ('e', 0) in state.extra['navs']  # 回合结束只推进一次 AV

    def test_break_interrupt_cancels_second_action(self):
        """口径A: 两动之间被击破 → 第二行动取消, 推条作用于下回合"""
        u = _unit()
        e = _enemy(actions_per_turn=2, attacks=CHARGE_ATTACKS)
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        _begin_enemy_turn(state, e)
        e.is_broken = True
        e.extra['av_delayed'] = 2500.0  # 模拟终结技破韧写入
        _enemy_pending_step(state)
        log = '\n'.join(state.log)
        assert '破韧打断' in log
        assert '狂暴挥击' not in log
        assert state.extra['navs'][('e', 0)] > 10000.0 / 80.0 + 2500.0 - 1.0

    def test_single_action_enemy_no_pending(self):
        """单动敌人（默认1）: 行为与 v6.4 一致"""
        u = _unit()
        e = _enemy(attacks=CHARGE_ATTACKS[:2])
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        _begin_enemy_turn(state, e)
        assert state.extra.get('enemy_pending_action') is None
        assert ('e', 0) in state.extra['navs']


class TestFrozenTick:
    def test_frozen_turn_ticks_statuses_and_pushes(self):
        """冻结跳过回合: buff/debuff 倒计时照算, 不攻击, 推条5000"""
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='rage', name='狂暴', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'ATK_PERCENT': 0.5}))
        e.statuses.append(EnemyStatus(id='fz', name='冻结', category='control',
                                      source='x', remaining_turns=1))
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        hp0 = u.current_hp
        _begin_enemy_turn(state, e)
        rage = next(s for s in e.statuses if s.id == 'rage')
        assert rage.remaining_turns == 1  # 倒计时照算
        assert not any(s.name == '冻结' for s in e.statuses)  # 冻结解除
        assert u.current_hp == hp0  # 未攻击
        assert state.extra['navs'][('e', 0)] > 10000.0 / 80.0 + 5000.0 - 1.0


class TestBruteBlackbox:
    def test_brute_charge_cycle_in_same_turn(self):
        """黑盒: test_brute 精英双动 → 同 AV 连续出现蓄力+狂暴挥击"""
        enemy = load_enemy('test_brute')
        configs = [{'char': load_character('seele'), 'lightcone': None,
                    'relics': [], 'relic_sets': {}, 'position': 1, 'eidolon': 0}]
        state = simulate(configs, enemy, max_av=1200.0)
        lines = [l for l in state.log if '蓄力' in l or '狂暴挥击' in l]
        assert len(lines) >= 2
        avs = []
        for l in lines:
            m = re.match(r'\[\s*([\d.]+)AV\]', l)
            if m:
                avs.append(float(m.group(1)))
        assert len(avs) >= 2
        assert avs[0] == pytest.approx(avs[1])  # 相邻两次行动 AV 相等（同一回合两动）
        assert not any('[ERROR]' in l for l in state.log)
