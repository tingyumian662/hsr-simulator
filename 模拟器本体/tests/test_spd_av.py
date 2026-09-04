"""P1: SPD_PERCENT buff 接入行动条（_effective_spd）+ 轮次统计测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, LightConeEffect, RelicPiece, RelicSet, RelicSetEffect
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _effective_spd, _begin_regular_turn, _lc_team_advance, _next_y_actor, simulate
from engine.runtime import SimUnit, SimState, TimedBuff


def _enemy(hp=500000, toughness=200, spd=80):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=spd,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


def _spd_buff(pct=25.0, turns=99):
    return TimedBuff(source_id='x', attributes={'SPD_PERCENT': pct},
                     remaining_turns=turns)


class TestEffectiveSpd:
    def test_no_buff_equals_base(self):
        """无 buff 时有效速度 == 面板 SPD（恒等, 既有测试零漂移前提）"""
        u = _unit('seele')
        assert _effective_spd(u) == pytest.approx(u.base_stats.SPD, rel=1e-9)

    def test_static_percent_not_double_folded(self):
        """静态 SPD_PERCENT 已折进 SPD（attributes.py:362）且字段残留 —
        不得重复计算（昔涟行迹 9% 场景）"""
        u = _unit('xilian')
        assert _effective_spd(u) == pytest.approx(u.base_stats.SPD, rel=1e-9)

    def test_timed_buff_applies(self):
        """战斗 TimedBuff 的 SPD_PERCENT 按白值叠加"""
        u = _unit('seele')
        spd0 = u.base_stats.SPD
        u.buffs.append(_spd_buff(25.0))
        expect = spd0 + u.base_stats._base_SPD * 0.25
        assert _effective_spd(u) == pytest.approx(expect, rel=1e-9)


class TestAvWiring:
    def test_next_av_uses_effective_spd(self):
        """常规回合 next_av 用有效速度（SP=0 强制普攻, 防 AI 战技再挂 buff）"""
        u = _unit('seele')
        u.buffs.append(_spd_buff(25.0))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 100.0}, 'av_stamp': {0: 1},
                            'stamp_counter': 1})
        state.current_av = 0.0
        state.skill_points = 0
        _begin_regular_turn(state, u)
        assert state.extra['navs'][0] == pytest.approx(
            0.0 + 10000.0 / _effective_spd(u, state), rel=1e-9)

    def test_lc_team_advance_uses_effective_spd(self):
        """全队拉条按各自有效速度"""
        u = _unit('seele')
        u.buffs.append(_spd_buff(25.0))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 500.0}
        _lc_team_advance(state, 0.24)
        expect = 500.0 - (10000.0 / _effective_spd(u, state)) * 0.24
        assert state.extra['navs'][0] == pytest.approx(expect, rel=1e-9)

    def test_seele_ripple_regression(self):
        """希儿涟漪: 无 buff 时与 base_stats.SPD 恒等（回归既有测试）"""
        from engine.characters.seele import _trace_seele_ripple
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_seele_ripple(u, state)
        assert getattr(u, '_pending_action_advance', 0) == pytest.approx(
            10000.0 / u.base_stats.SPD * 0.20, rel=1e-6)


class TestInitialAv:
    def test_battle_start_speed_lc_is_not_double_counted(self):
        """战斗开始型速度光锥只在事件 Buff 存续时生效一次。"""
        lc = LightCone(
            id='start_spd', name='开局速度', path='巡猎',
            effects=[LightConeEffect(
                type='conditional_buff', condition_code='event_battle_start',
                attributes={'SPD_percent': 12.0}, condition='进入战斗后速度+12%',
            )],
        )
        char = load_character('seele', 'data/characters')
        state = simulate(
            [{'char': char, 'position': 1, 'lightcone': lc}], _enemy(), max_av=1,
        )
        u = state.units[0]
        assert state.extra['navs'][0] == pytest.approx(
            10000.0 / u.base_stats.SPD, rel=1e-9,
        )

    def test_enter_battle_advance_applies_after_nav_initialization(self):
        """翁瓦克的开局40%拉条不能因行动表尚未建立而丢失。"""
        relics = [RelicPiece(slot='head', set_name='test_wacqwaq')]
        relic_sets = {
            'test_wacqwaq': RelicSet(
                name='test_wacqwaq', effects=[RelicSetEffect(
                    pieces_required=1, description='',
                    attributes={'SPD_percent': 10.0},
                    condition='enter_combat_action_advance',
                )],
            ),
        }
        char = load_character('seele', 'data/characters')
        state = simulate(
            [{'char': char, 'position': 1, 'relics': relics, 'relic_sets': relic_sets}],
            _enemy(), max_av=1,
        )
        u = state.units[0]
        assert state.extra['navs'][0] == pytest.approx(
            10000.0 / u.base_stats.SPD * 0.60, rel=1e-9,
        )

    def test_xilian_e6_team_advance_uses_effective_spd(self):
        """昔涟E6第二次献予的全队拉条必须读取临时速度 Buff。"""
        from engine.systems.remembrance import RemembranceSystem

        u = _unit('xilian')
        u.eidolon_rank = 6
        u.extra['zhuiyi_sources'] = {'ally'}
        u.buffs.append(_spd_buff(25.0))
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 500.0}, 'xilian_gift_count': 1})
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)

        rem._use_memsprite_skill(state, u, u.memsprite_unit, 'memsprite_basic')
        assert state.extra['navs'][0] == pytest.approx(
            500.0 - (10000.0 / _effective_spd(u, state)) * 0.24,
            rel=1e-9,
        )


class TestIntegration:
    def _run(self, u, max_av=400):
        """手动驱动 Y 轴常规回合（SP=0 强制普攻防自挂 buff; 敌方 AV 置极大值）"""
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra.update({'navs': {0: 100.0, ('e', 0): 1e9},
                            'av_stamp': {0: 1}, 'stamp_counter': 1})
        while True:
            actor, av = _next_y_actor(state)
            if actor is None or av >= max_av:
                break
            state.current_av = av
            state.skill_points = 0
            _begin_regular_turn(state, actor)
        return state.action_counts.get(u.char.id, 0)

    def test_speed_buff_more_actions(self):
        """SPD buff 使固定窗口内出手次数增加（黑盒集成）"""
        plain = _unit('seele')
        buffed = _unit('seele')
        buffed.buffs.append(_spd_buff(25.0))
        n_plain = self._run(plain)
        n_buffed = self._run(buffed)
        assert n_buffed > n_plain

    def test_action_counts_populated(self):
        """action_counts 记录每角色行动次数"""
        from engine.core.combat_engine import simulate
        chars = [{'char': load_character('seele', 'data/characters'),
                  'position': 1}]
        s = simulate(chars, _enemy(), max_av=300)
        assert sum(s.action_counts.values()) > 0

    def test_cycles_computed(self):
        """cycles: 以队内最慢角色行动值为一轮的近似"""
        from engine.core.combat_engine import simulate
        chars = [{'char': load_character('seele', 'data/characters'),
                  'position': 1}]
        s = simulate(chars, _enemy(), max_av=300)
        min_spd = min(u.base_stats.SPD for u in s.units if u.is_alive)
        assert s.cycles == int(s.current_av * min_spd / 10000.0)
