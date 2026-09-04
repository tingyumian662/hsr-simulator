"""P4: 玩家侧控制状态 + EHR 命中检定测试（最小闭环）"""
import random
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _apply_player_status, _check_control_status, _begin_regular_turn, _enqueue_ult, _exec_extra_turn, _select_enemy_target, _enemy_attack, _effective_spd
from engine.characters.fengjin import _fengjin_cleanse
from engine.runtime import SimUnit, SimState, PlayerStatus, TimedBuff, _set_av


def _enemy(hp=500000, attacks=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks)


SWING = [{"name": "挥击", "element": "物理", "damage_type": "direct",
          "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


def _control(name='眩晕', turns=1, chance=1.0):
    return PlayerStatus(id=f'test:{name}', name=name, category='control',
                        remaining_turns=turns, base_chance=chance)


class TestApplyStatus:
    def test_full_chance_hits(self):
        """base_chance=1.0 无抵抗 → 必中"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        ok = _apply_player_status(state, u, _control())
        assert ok
        assert any(s.name == '眩晕' for s in u.statuses)

    def test_res_reduces_chance(self):
        """EFFECT_RES=0.5 → 命中率 50%（seeded 统计）"""
        u = _unit('seele')
        u.base_stats.EFFECT_RES = 0.5
        state = SimState(enemies=[_enemy()], units=[u])
        random.seed(7)
        hits = sum(1 for _ in range(400)
                   if _apply_player_status(state, u, _control(chance=1.0)))
        assert 120 < hits < 280  # 50% ± 容差

    def test_huohuo_res_35(self):
        """藿藿控抗精通 0.35 + 基础 RES 0.15 → 命中率 50%"""
        u = _unit('huohuo')
        u.base_stats.EFFECT_RES += 0.35
        state = SimState(enemies=[_enemy()], units=[u])
        random.seed(7)
        hits = sum(1 for _ in range(400)
                   if _apply_player_status(state, u, _control(chance=1.0)))
        assert 140 < hits < 260  # 50% ± 容差

    def test_mydei_immune(self):
        """万敌血仇 + 三十僭主 → 免疫控制"""
        u = _unit('mydei', is_blood_debt=True, debt_control_immune=True)
        state = SimState(enemies=[_enemy()], units=[u])
        ok = _apply_player_status(state, u, _control())
        assert not ok
        assert not u.statuses
        assert any('免疫' in l for l in state.log)

    def test_fuxuan_charge_consumed(self):
        """符玄遁甲星舆: 消耗1次抵抗, 第二次命中"""
        u = _unit('seele')
        fu = _unit('fu_xuan', position=2, fuxuan_cc_resist_charges=1)
        state = SimState(enemies=[_enemy()], units=[u, fu])
        ok1 = _apply_player_status(state, u, _control())
        assert not ok1  # 符玄消耗次数全队抵抗
        assert fu.extra['fuxuan_cc_resist_charges'] == 0
        ok2 = _apply_player_status(state, u, _control())
        assert ok2  # 次数用尽 → 命中


class TestControlActions:
    def test_status_attributes_affect_effective_stats(self):
        """减速 PlayerStatus 的 SPD_PERCENT 必须进入有效速度/行动条。"""
        u = _unit('seele')
        u.statuses.append(PlayerStatus(
            id='slow', name='减速', category='debuff', remaining_turns=2,
            attributes={'SPD_PERCENT': -20.0},
        ))
        state = SimState(enemies=[_enemy()], units=[u])
        assert _effective_spd(u, state) == pytest.approx(
            u.base_stats.SPD - u.base_stats._base_SPD * 0.20,
            rel=1e-9,
        )

    def test_timed_status_expires_after_its_last_action_value(self):
        """duration=1 的减速只影响一次下一行动值，不能遗留到再下一回合。"""
        u = _unit('seele')
        u.statuses.append(PlayerStatus(
            id='slow', name='减速', category='debuff', remaining_turns=1,
            attributes={'SPD_PERCENT': -20.0},
        ))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 0.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1}, 'stamp_counter': 1})
        state.skill_points = 0
        _begin_regular_turn(state, u)
        first_next_av = state.extra['navs'][0]
        assert u.statuses == []
        assert first_next_av == pytest.approx(
            10000.0 / (u.base_stats.SPD - u.base_stats._base_SPD * 0.20),
            rel=1e-9,
        )

        state.current_av = first_next_av
        _begin_regular_turn(state, u)
        assert state.extra['navs'][0] == pytest.approx(
            first_next_av + 10000.0 / u.base_stats.SPD, rel=1e-9,
        )

    def test_stun_skips_turn(self):
        """眩晕: 常规回合被跳过, 行动计数不增, buff 正常 tick"""
        u = _unit('seele')
        u.statuses.append(_control('眩晕', turns=1))
        u.buffs.append(TimedBuff(source_id='x', attributes={'ATK_PERCENT': 10.0},
                                 remaining_turns=1))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 100.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1}, 'stamp_counter': 1})
        state.current_av = 0.0
        state.skill_points = 0
        _begin_regular_turn(state, u)
        assert state.action_counts.get('seele', 0) == 0
        assert any('跳过本回合' in l for l in state.log)
        assert u.statuses == []  # 倒计时归零移除
        assert u.buffs == []  # buff 正常 tick

    def test_frozen_skips_and_pushes(self):
        """冻结: 跳过回合 + 推条5000"""
        u = _unit('seele')
        u.statuses.append(_control('冻结', turns=1))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 100.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1}, 'stamp_counter': 1})
        state.current_av = 0.0
        _begin_regular_turn(state, u)
        # next_av(10000/SPD) + 推条 5000
        assert state.extra['navs'][0] == pytest.approx(
            0.0 + 10000.0 / u.base_stats.SPD + 5000.0, rel=1e-6)

    def test_frozen_blocks_x_axis(self):
        """冻结期间: 终结技无法入队; 已入队行动被跳过"""
        u = _unit('seele')
        u.statuses.append(_control('冻结', turns=2))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['extra_turns'] = [(u, 'ult')]
        _enqueue_ult(state, u)  # 新终结技被拦
        assert len(state.extra['extra_turns']) == 1
        _exec_extra_turn(state, u, 'ult')  # 已入队被跳过
        assert any('冻结中' in l for l in state.log)

    def test_taunt_forces_target(self):
        """嘲讽: 敌方强制选中嘲讽单位"""
        u = _unit('seele')
        u.statuses.append(PlayerStatus(id='t', name='嘲讽', category='control'))
        other = _unit('fu_xuan', position=2)
        state = SimState(enemies=[_enemy()], units=[u, other])
        random.seed(42)
        for _ in range(50):
            assert _select_enemy_target(state) is u


class TestCleanse:
    def test_fengjin_cleanse_status_first(self):
        """风堇净化: 优先清 PlayerStatus"""
        u = _unit('seele')
        u.statuses.append(_control('眩晕', turns=2))
        fengjin = _unit('fengjin', position=2)
        state = SimState(enemies=[_enemy()], units=[u, fengjin])
        _fengjin_cleanse(state, fengjin)
        assert u.statuses == []


class TestEnemyApply:
    def test_attack_applies_debuffs(self):
        """敌方攻击 debuffs 字段 → 施加到目标"""
        atk = [{**SWING[0], 'debuffs': [
            {'id': 'enemy_stun', 'name': '眩晕', 'category': 'control',
             'duration': 1, 'base_chance': 1.0}]}]
        u = _unit('seele')
        state = SimState(enemies=[_enemy(attacks=atk)], units=[u])
        _enemy_attack(state, state.enemies[0])
        assert any(s.name == '眩晕' for s in u.statuses)

    def test_no_debuffs_no_status(self):
        """无 debuffs 字段 → 不施加（既有敌人零影响）"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[u])
        _enemy_attack(state, state.enemies[0])
        assert u.statuses == []
