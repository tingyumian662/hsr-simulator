"""v6.4 敌方系统优化回归: 自增益/蓄力(self_buffs) + debuff 作用域

语义依据: CLAUDE_HANDOFF.md v6.4 节（用户确认: 不加命中率; 加敌方自增益; 本轮只做引擎）"""
import json
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _enemy_attack, _enemy_attack_stats, _enemy_eff_spd, _begin_enemy_turn, _apply_player_status
from engine.runtime import SimState, SimUnit
from engine.systems.techniques import calc_effect_probability


def _enemy(**kw):
    base = dict(id='x', name='X', HP=500000, ATK=600, DEF=800, SPD=80,
                toughness=200, max_toughness=200, level=80,
                element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                             '虚数': 0, '物理': 0, '火': 0})
    base.update(kw)
    return Enemy(**base)


def _unit(cid='seele', position=1):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    return u


class TestEnemySelfBuffs:
    def test_self_buff_applied_and_atk_consumed(self):
        """v6.4: 敌方自增益施加 + ATK_PERCENT 消费（狂暴后敌攻伤害提升）"""
        u = _unit()
        e = _enemy()
        e.attacks = [{'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1,
                      'self_buffs': [{'id': 'rage', 'name': '狂暴',
                                      'attributes': {'ATK_PERCENT': 0.50}, 'duration': 2}]}]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        # buff 已施加
        assert any(s.id == 'rage' and s.category == 'buff' for s in e.statuses)
        # ATK 消费: 600 × 1.5 = 900
        assert _enemy_attack_stats(e).ATK == pytest.approx(900.0, rel=1e-9)

    def test_self_buff_expires(self):
        """v6.4: 自增益倒计时到期移除（tick_statuses 敌方回合倒计时）"""
        from engine.models.enemy import EnemyStatus
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='rage', name='狂暴', category='buff',
                                      source='x', remaining_turns=1,
                                      attributes={'ATK_PERCENT': 0.50}))
        # 施加后 ATK 生效
        assert _enemy_attack_stats(e).ATK == pytest.approx(900.0, rel=1e-9)
        # 敌方回合 tick: 1→0 到期移除
        expired = e.tick_statuses()
        assert any(s.id == 'rage' for s in expired)
        assert not any(s.id == 'rage' for s in e.statuses)
        assert _enemy_attack_stats(e).ATK == pytest.approx(600.0, rel=1e-9)

    def test_requires_buff_charge_cycle(self):
        """v6.4: 蓄力循环——无狂暴用普通技, 有狂暴用强化技（requires_buff 过滤）"""
        u = _unit()
        e = _enemy()
        e.attacks = [
            {'name': '普通', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1},
            {'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 2,
             'self_buffs': [{'id': 'rage', 'name': '狂暴',
                             'attributes': {'ATK_PERCENT': 0.50}, 'duration': 2}]},
            {'name': '狂暴挥击', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 150.0, 'target_type': 'single_enemy', 'priority': 3,
             'requires_buff': 'rage'},
        ]
        state = SimState(enemies=[e], units=[u])
        # 无 rage: requires_buff 技能被过滤 → 取蓄力(priority 2)
        used = []
        for _ in range(4):
            _enemy_attack(state, e)
            # 从日志记录实际使用技能（_enemy_attack 内部选技能+施加）
            last_atk = [l for l in state.log if 'AV]' in l
                        and any(k in l for k in ('普通', '蓄力', '狂暴挥击'))][-1]
            used.append('狂暴挥击' if '狂暴挥击' in last_atk
                        else ('蓄力' if '蓄力' in last_atk else '普通'))
        # 手动调 _enemy_attack 无 tick: 蓄力施加 rage(duration 2) 后持续存在 →
        # R1 蓄力 → R2/R3/R4 狂暴挥击
        assert used == ['蓄力', '狂暴挥击', '狂暴挥击', '狂暴挥击']

    def test_self_buff_and_atk_down_coexist(self):
        """v6.4: 敌方自增益与玩家降攻并存（atk_down × ATK_PERCENT）"""
        from engine.models.enemy import EnemyStatus
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='defect', name='缺陷', category='debuff',
                                      source='silver_wolf', remaining_turns=2,
                                      attributes={'atk_down': 0.10}))
        e.statuses.append(EnemyStatus(id='rage', name='狂暴', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'ATK_PERCENT': 0.50}))
        # 600 × (1-0.10) × (1+0.50) = 810
        assert _enemy_attack_stats(e).ATK == pytest.approx(810.0, rel=1e-9)

    def test_self_spd_buff_consumed(self):
        """v6.4: 敌方 SPD_PERCENT 自增益消费"""
        from engine.models.enemy import EnemyStatus
        e = _enemy()
        e.statuses.append(EnemyStatus(id='haste', name='加速', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_PERCENT': 0.25}))
        assert _enemy_eff_spd(e) == pytest.approx(100.0, rel=1e-9)  # 80×1.25


class TestDebuffScope:
    def test_debuff_target_main_only(self):
        """v6.4: debuffs[].target=main 只施加初选目标"""
        u1, u2 = _unit(position=1), _unit(position=2)
        e = _enemy()
        e.attacks = [{'name': '咆哮', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 50.0, 'target_type': 'all_enemies', 'priority': 1,
                      'debuffs': [{'id': 'stun', 'name': '眩晕', 'category': 'control',
                                   'duration': 1, 'base_chance': 1.0, 'target': 'main'}]}]
        state = SimState(enemies=[e], units=[u1, u2])
        _enemy_attack(state, e)
        # main: 恰好 1 名目标获得眩晕（初选目标, 非全部命中者）
        stunned = [u for u in (u1, u2) if any(s.id == 'stun' for s in u.statuses)]
        assert len(stunned) == 1

    def test_debuff_target_all_default(self):
        """v6.4: 无 target 字段默认 all（全部命中目标施加）"""
        u1, u2 = _unit(position=1), _unit(position=2)
        e = _enemy()
        e.attacks = [{'name': '咆哮', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 50.0, 'target_type': 'all_enemies', 'priority': 1,
                      'debuffs': [{'id': 'stun', 'name': '眩晕', 'category': 'control',
                                   'duration': 1, 'base_chance': 1.0}]}]
        state = SimState(enemies=[e], units=[u1, u2])
        _enemy_attack(state, e)
        assert any(s.id == 'stun' for s in u1.statuses)
        assert any(s.id == 'stun' for s in u2.statuses)
