"""击破异常倒计时测试（敌方行动时结算; X轴额外回合不结算）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate


def _enemy(hp=500000, toughness=100, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                     '虚数': 0, '物理': 0, '火': 0})


def _unit(cid):
    from engine.core.combat_sim import SimUnit
    from engine.core.attributes import compute_combat_stats
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=1)
    u.max_hp = u.current_hp = stats.HP
    return u


def _state(enemy, u):
    """最小战斗状态（手动驱动敌方回合用）"""
    from engine.core.combat_sim import SimState
    state = SimState(enemies=[enemy], units=[u])
    state.extra['navs'] = {('e', 0): 0.0}
    state.extra['av_stamp'] = {('e', 0): 1}
    state.extra['stamp_counter'] = 1
    state.extra['action_ctx'] = 'regular'
    return state


class TestBreakDebuffTick:
    def test_wind_dot_expires_after_two_enemy_turns(self):
        """风化(持续型): 敌方行动时递减+DOT跳伤, 2次行动后到期解除"""
        from engine.core.combat_sim import _apply_break_debuff, _begin_enemy_turn
        u = _unit('fengjin')
        e = _enemy()
        state = _state(e, u)
        _apply_break_debuff(e, '风', u, state)
        assert e.break_debuff_turns == 2
        # 第1次敌方行动: 韧性恢复 + DOT跳伤 + tick 2→1
        _begin_enemy_turn(state, e)
        assert e.break_debuff_turns == 1
        assert any(s.id.startswith('break:') for s in e.statuses)  # 风化未到期
        # 第2次敌方行动: 1→0 解除
        _begin_enemy_turn(state, e)
        assert e.break_debuff_name == ''
        assert not any(s.id.startswith('break:') for s in e.statuses)

    def test_break_dot_tick_damage(self):
        """DOT 跳伤: 击破者快照面板 × 倍率"""
        from engine.core.combat_sim import _apply_break_debuff, _begin_enemy_turn
        u = _unit('fengjin')
        e = _enemy()
        state = _state(e, u)
        _apply_break_debuff(e, '风', u, state)
        hp0 = e.HP
        _begin_enemy_turn(state, e)
        assert e.HP < hp0  # DOT 跳伤扣血
        assert '风化' in '\n'.join(state.log)

    def test_toughness_recovery_on_enemy_turn(self):
        """韧性恢复: 敌方行动时 is_broken→复原"""
        from engine.core.combat_sim import _apply_break_debuff, _begin_enemy_turn
        u = _unit('fengjin')
        e = _enemy(toughness=100)
        e.toughness = 0
        e.is_broken = True
        state = _state(e, u)
        _begin_enemy_turn(state, e)
        assert e.is_broken is False
        assert e.toughness == e.max_toughness

    def test_imprison_speed_restored(self):
        """禁锢(虚数): 敌方2次行动后到期, 速度恢复"""
        from engine.core.combat_sim import _apply_break_debuff, _begin_enemy_turn
        u = _unit('mydei')
        e = _enemy()
        state = _state(e, u)
        _apply_break_debuff(e, '虚数', u, state)
        assert e.SPD == pytest.approx(80 * 0.80, abs=1e-6)  # 禁锢减速
        _begin_enemy_turn(state, e)
        _begin_enemy_turn(state, e)
        assert e.break_debuff_name == ''
        assert e.SPD == pytest.approx(80, abs=1e-6)  # 速度恢复

    def test_freeze_expire_delays_5000(self):
        """冻结（v5.0 P7 用户实机语义）: 常规回合时跳过该回合 + 推条5000 + 解除"""
        from engine.core.combat_sim import _apply_break_debuff, _begin_enemy_turn
        u = _unit('changyeyue')
        e = _enemy()
        state = _state(e, u)
        _apply_break_debuff(e, '冰', u, state)
        _begin_enemy_turn(state, e)
        assert e.break_debuff_name == ''  # 跳过即解除
        assert any('冻结' in l for l in state.log)
        # 推条 5000 进入 av_delayed 被本次行动消费
        assert state.extra['navs'][('e', 0)] >= state.current_av + 5000
