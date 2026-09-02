"""v7.0.0 知更鸟·晴歌审查修复回归 (d34d0aa 审查 A1-A6/B3 + C 类覆盖)

数据源: 角色技能介绍/记忆/知更鸟·晴歌.txt（用户原稿 v2）
A1 终结技自身回能5 / A2 E3/E5等级解析(含风堇同病) / A3 等级加成消费(每级+5%)
A4 忆灵路径E2/律动 / A5 E6倍率引擎级对照 / A6 特邀嘉宾精确落位
B3 退出Fever行动提前基于摘除快照 / C 倒计时边界+E4速度精确值
"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate


def _enemy(res=None):
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80,
                 element_res=res or {'风': 0})


def _qingge():
    return load_character('robin_summeretto', 'data/characters')


def _state(eidolon=0):
    """手动构造晴歌单人 SimState（引擎函数级测试用）"""
    from engine.core.combat_sim import SimState, SimUnit
    from engine.core.attributes import compute_combat_stats
    char = _qingge()
    stats = compute_combat_stats(char, None, None, None)
    u = SimUnit(char=char, base_stats=stats, position=1)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    state = SimState(enemies=[_enemy()], units=[u])
    state.extra['navs'] = {0: 100.0}
    from engine.systems.remembrance import RemembranceSystem
    state.extra['_rem_sys'] = RemembranceSystem()
    return state, u


class TestA1UltSelfEnergy:
    def test_ult_gains_self_5_energy(self):
        """A1: txt L42 终结技'能量恢复:5'——函数级精确断言
        (通用energy_regen路径消费JSON effects; v7.0.0 GLM验收P1: 曾内联+通用双重回能+10,
        已删除内联仅保留JSON, 与姬子·启行等26角色同模式)"""
        from engine.core.combat_sim import _use_skill, SimUnit
        from engine.core.attributes import compute_combat_stats
        state, u = _state(eidolon=0)
        u.current_energy = float(u.char.max_energy)
        shell = load_character('march_7th', 'data/characters')
        st = compute_combat_stats(shell, None, None, None)
        t = SimUnit(char=shell, base_stats=st, position=2)
        t.max_hp = t.current_hp = st.HP
        t.current_energy = 0
        state.units.append(t)
        state.extra['navs'][1] = 200.0
        _use_skill(u, state, 'ultimate')
        assert u.current_energy == pytest.approx(5.0, rel=1e-9)


class TestA2E3E5Parse:
    def test_e3_boost_keys(self):
        """A2: E3'战技+2/天赋+2/忆灵天赋+1'→skill2/talent2/memsprite_talent1(非talent3)"""
        s = simulate([{'char': _qingge(), 'position': 1, 'eidolon': 3}],
                     _enemy(), max_av=50)
        boost = s.units[0].extra.get('skill_level_boost', {})
        assert boost == {'skill': 2, 'talent': 2, 'memsprite_talent': 1}

    def test_e5_boost_keys(self):
        """A2: E5'终结技+2/普攻+1/忆灵技+1'→ultimate2/basic_attack1/memsprite_skill1
        (E5档位E3同激活: skill2/talent2/memsprite_talent1)"""
        s = simulate([{'char': _qingge(), 'position': 1, 'eidolon': 5}],
                     _enemy(), max_av=50)
        boost = s.units[0].extra.get('skill_level_boost', {})
        assert boost == {'skill': 2, 'talent': 2, 'memsprite_talent': 1,
                         'ultimate': 2, 'basic_attack': 1, 'memsprite_skill': 1}

    def test_fengjin_e5_same_fix(self):
        """A2 同病修复: 风堇E5'战技+2/天赋+2/忆灵天赋+1'→talent=2(非3)"""
        s = simulate([{'char': load_character('fengjin', 'data/characters'),
                       'position': 1, 'eidolon': 5}], _enemy(), max_av=50)
        boost = s.units[0].extra.get('skill_level_boost', {})
        assert boost.get('talent') == 2
        assert boost.get('memsprite_talent') == 1


class TestA3LevelFactors:
    def test_skill_heal_factor_e3(self):
        """A3①: E3战技+2→Lv12回血=晴空乐手HP上限×1.1(日志可见未截断值)"""
        state, u = _state(eidolon=3)
        u.extra['skill_level_boost'] = {'skill': 2, 'talent': 2, 'memsprite_talent': 1}
        rem = state.extra['_rem_sys']
        ms = rem._qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        ms.current_hp = ms.max_hp * 0.5
        rem._qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        log = '\n'.join(state.log)
        assert f'回血{int(ms.max_hp * 1.1)}' in log

    def test_ult_energy_factor_e5(self):
        """A3④: E5终结技+2→目标回能=20%能量上限×1.1"""
        from engine.core.combat_sim import _qingge_ultimate, SimUnit
        from engine.core.attributes import compute_combat_stats
        state, u = _state(eidolon=5)
        u.extra['skill_level_boost'] = {'ultimate': 2, 'basic_attack': 1,
                                        'memsprite_skill': 1}
        fj = load_character('fengjin', 'data/characters')
        st = compute_combat_stats(fj, None, None, None)
        t = SimUnit(char=fj, base_stats=st, position=2)
        t.max_hp = t.current_hp = st.HP
        t.current_energy = 0
        state.units.append(t)
        state.extra['navs'][1] = 200.0
        _qingge_ultimate(state, u)
        assert t.current_energy == pytest.approx(
            (fj.max_energy or 0) * 0.20 * 1.1, rel=1e-9)

    def test_field_def_pen_factor_e3(self):
        """A3②: E3天赋+2→结界DEF_PEN=(15%+气氛×0.5%)×1.1"""
        state, u = _state(eidolon=3)
        u.extra['skill_level_boost'] = {'skill': 2, 'talent': 2, 'memsprite_talent': 1}
        base_pen = u.base_stats.DEF_PEN
        rem = state.extra['_rem_sys']
        for name in ('贝茜', '啾米', '派丁'):
            rem._qingge_summon_variant(state, u, u.char.memsprite, name)
        atmo = u.extra.get('qingge_atmo', 0.0)
        assert u.base_stats.DEF_PEN == pytest.approx(
            base_pen + (0.15 + atmo * 0.005) * 1.1, rel=1e-9)

    def test_fever_boost_and_vuln_factor_e3(self):
        """A3③: E3忆灵天赋+1→Fever伤害=(60%+气氛×2%)×1.05; 易伤3只=16%×1.05"""
        state, u = _state(eidolon=3)
        u.extra['skill_level_boost'] = {'skill': 2, 'talent': 2, 'memsprite_talent': 1}
        base_dmg = u.base_stats.DMG_BONUS_ALL
        base_vuln = u.base_stats.VULNERABILITY_APPLIED
        rem = state.extra['_rem_sys']
        for name in ('贝茜', '啾米', '派丁'):
            rem._qingge_summon_variant(state, u, u.char.memsprite, name)
        atmo = u.extra.get('qingge_atmo', 0.0)
        assert u.base_stats.DMG_BONUS_ALL == pytest.approx(
            base_dmg + (0.60 + atmo * 0.02) * 1.05, rel=1e-9)
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(
            base_vuln + 0.16 * 1.05, rel=1e-9)


class TestA4MemspritePath:
    def test_memsprite_attack_triggers_e2_and_rhythm(self):
        """A4: 晴歌忆灵施放忆灵技→E2首次额外+2与律动消耗触发"""
        state, u = _state(eidolon=2)
        u.extra['qingge_atmo'] = 10.0
        u.extra['qingge_rhythm'] = 12
        from engine.core.combat_sim import _qingge_on_ally_attack
        _qingge_on_ally_attack(state, u, via_memsprite=True)
        assert u.extra['qingge_atmo'] == pytest.approx(13.0, rel=1e-9)  # +1攻击+2E2
        log = '\n'.join(state.log)
        assert 'E2额外' in log
        assert '消耗1层律动' in log


class TestA5E6MultiplierEngine:
    def test_e6_multiplier_via_engine(self):
        """A5: 引擎实际忆灵技伤害 E5=E0×1.05, E6=E0×2.10(1.05×2)"""
        def _dealt(rank, boost):
            state, u = _state(eidolon=rank)
            u.extra['skill_level_boost'] = boost
            rem = state.extra['_rem_sys']
            ms = rem._qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
            before = state.enemies[0].HP
            rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
            return before - state.enemies[0].HP
        d0 = _dealt(0, {})
        d5 = _dealt(5, {'memsprite_skill': 1})
        d6 = _dealt(6, {'memsprite_skill': 1})
        assert d0 > 0
        assert d5 == pytest.approx(d0 * 1.05, rel=1e-9)
        assert d6 == pytest.approx(d0 * 2.10, rel=1e-9)


class TestA6GuestBuff:
    def test_guest_buff_lands_2_turns(self):
        """A6: 特邀嘉宾buff真实落位且remaining_turns==2"""
        from engine.core.combat_sim import _qingge_ultimate, SimUnit
        from engine.core.attributes import compute_combat_stats
        state, u = _state(eidolon=0)
        fj = load_character('fengjin', 'data/characters')
        st = compute_combat_stats(fj, None, None, None)
        t = SimUnit(char=fj, base_stats=st, position=2)
        t.max_hp = t.current_hp = st.HP
        state.units.append(t)
        state.extra['navs'][1] = 200.0
        _qingge_ultimate(state, u)
        guest = [b for b in t.buffs if getattr(b, 'param_id', '') == 'qingge_guest']
        assert len(guest) == 1 and guest[0].remaining_turns == 2


class TestB3ExitFeverAdvance:
    def test_exit_uses_suspended_av(self):
        """B3: 退出Fever行动提前=基于摘除快照减半, max(current_av, susp-half)"""
        state, u = _state(eidolon=0)
        state.extra['navs'] = {0: 1000.0}
        rem = state.extra['_rem_sys']
        for name in ('贝茜', '啾米', '派丁'):
            rem._qingge_summon_variant(state, u, u.char.memsprite, name)
        assert u.extra.get('qingge_fever')
        assert 0 not in state.extra['navs']
        from engine.core.combat_sim import (_qingge_exit_fever, AV_PER_TURN,
                                            _effective_spd)
        half = AV_PER_TURN / max(_effective_spd(u, state), 1.0) * 0.5
        _qingge_exit_fever(state, u)
        assert state.extra['navs'][0] == pytest.approx(
            max(state.current_av, 1000.0 - half), rel=1e-9)


class TestCCountdownAndE4:
    def test_countdown_min_12_boundary(self):
        """C: 倒计时扣50%至少12——气氛10(<24)时扣10→归零退出Fever"""
        state, u = _state(eidolon=0)
        u.extra['qingge_atmo'] = 10.0
        u.extra['qingge_fever'] = True
        from engine.core.combat_sim import _qingge_countdown_action
        _qingge_countdown_action(state, None)
        assert u.extra['qingge_atmo'] == 0
        assert not u.extra.get('qingge_fever')

    def test_e4_ms_spd_precise(self):
        """C: E4晴空乐手速度=晴歌SPD×1.8×(1+20%+气氛×0.5%)精确断言"""
        state, u = _state(eidolon=4)
        u.extra['qingge_atmo'] = 20.0
        u.extra['qingge_fever'] = True
        from engine.core.combat_sim import _qingge_ms_spd, _effective_spd
        expect = _effective_spd(u, state) * 1.80 * (1.0 + 0.20 + 20.0 * 0.005)
        rem = state.extra['_rem_sys']
        ms = rem._qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        assert _qingge_ms_spd(state, u, ms) == pytest.approx(expect, rel=1e-9)
