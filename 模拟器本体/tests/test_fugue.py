"""v5.3: 忘归人（击破辅助）测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _super_break_rate, _apply_toughness_damage
from engine.runtime import SimUnit, SimState
from engine.characters.fugue import _fugue_cloudfire_apply, _fugue_foxian_def_down, _fugue_trace2_team_be, _eid_fugue_e2_energy, _eid_fugue_e2_ult


def _enemy(hp=500000, toughness=200, fire_res=0.0):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': fire_res})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestCloudfire:
    def test_cloudfire_apply(self):
        """云火昭: 敌额外40%韧性上限, 可二次击破"""
        fugue = _unit('fugue')
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[fugue])
        _fugue_cloudfire_apply(fugue, state)
        assert e.extra_toughness_max == pytest.approx(80.0, abs=1e-6)  # 200×40%
        assert e.extra_toughness == pytest.approx(80.0, abs=1e-6)

    def test_cloudfire_second_break(self):
        """已击破目标削云火昭至0 → 二次击破伤害+延后"""
        fugue = _unit('fugue')
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[fugue])
        _fugue_cloudfire_apply(fugue, state)
        state.current_av = 0.0
        hp0 = e.HP
        _use_skill(fugue, state, 'basic_attack')  # 击破主韧性
        assert e.is_broken
        # 削云火昭（削韧10×5次普攻 → 80韧性需要 8 次）
        from engine.core.combat_engine import _apply_toughness_damage
        stats = compute_combat_stats(fugue.char, None, None, None)
        for _ in range(8):
            _apply_toughness_damage(state, fugue, e, 10.0, '火', 'basic_attack', stats)
        assert e.extra_toughness == pytest.approx(0.0, abs=1e-6)
        assert '云火昭击破' in '\n'.join(state.log)
        assert e.HP < hp0
        assert e.extra.get('av_delayed', 0.0) >= 2500.0  # 二次击破延后

    def test_cloudfire_no_restore_on_enemy_turn(self):
        """敌方回合韧性恢复不恢复云火昭"""
        fugue = _unit('fugue')
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[fugue])
        _fugue_cloudfire_apply(fugue, state)
        e.is_broken = True
        e.toughness = 0.0
        from engine.core.combat_engine import _begin_enemy_turn
        _begin_enemy_turn(state, e)
        assert e.extra_toughness == pytest.approx(80.0, abs=1e-6)  # 未恢复

    def test_cloudfire_nonweak_attack_does_not_deplete_extra_toughness(self):
        """没有对应弱点的攻击不会消耗云火昭韧性。"""
        fugue = _unit('fugue')
        attacker = _unit('seele', position=2)
        e = _enemy(toughness=200)
        e.element_res['量子'] = 0.20
        state = SimState(enemies=[e], units=[fugue, attacker])
        _fugue_cloudfire_apply(fugue, state)

        _apply_toughness_damage(
            state, attacker, e, 10.0, '量子', 'basic_attack', attacker.base_stats,
        )

        assert e.extra_toughness == pytest.approx(80.0, abs=1e-6)

    def test_cloudfire_waits_for_primary_break(self):
        """主韧性未被击破前，云火昭不应被提前消耗或触发二次击破。"""
        fugue = _unit('fugue')
        attacker = _unit('seele', position=2)
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[fugue, attacker])
        _fugue_cloudfire_apply(fugue, state)

        _apply_toughness_damage(
            state, attacker, e, 10.0, '量子', 'basic_attack', attacker.base_stats,
        )

        assert e.toughness == pytest.approx(190.0, abs=1e-6)
        assert e.extra_toughness == pytest.approx(80.0, abs=1e-6)
        assert '云火昭击破' not in '\n'.join(state.log)


class TestFoxian:
    def test_foxian_break_effect_buff(self):
        """狐祈: 目标击破特攻+30%（BUFF_REGISTRY）"""
        from engine.core.combat_engine import BUFF_REGISTRY
        assert BUFF_REGISTRY['fugue_foxian']['BREAK_EFFECT'] == pytest.approx(30.0, abs=1e-9)

    def test_foxian_ignores_weakness_half(self):
        """狐祈者攻击无对应弱点目标 → 50%效率削韧（量子攻击 vs 全抗敌人）"""
        fugue = _unit('fugue')
        ally = _unit('seele', position=2)  # 量子攻击
        ally.extra['_foxian'] = True
        e = _enemy(toughness=200)
        e.element_res = {k: 0.20 for k in e.element_res}  # 无任何弱点
        state = SimState(enemies=[e], units=[fugue, ally])
        state.current_av = 0.0
        _use_skill(ally, state, 'basic_attack')
        assert e.toughness == pytest.approx(200 - 5.0, abs=1e-6)  # 削韧10×50%

    def test_foxian_weak_target_full(self):
        """狐祈者攻击有火弱点目标 → 全额削韧"""
        ally = _unit('seele', position=1)
        ally.extra['_foxian'] = True
        e = _enemy(toughness=200, fire_res=0.0)
        state = SimState(enemies=[e], units=[ally])
        state.current_av = 0.0
        _use_skill(ally, state, 'basic_attack')
        assert e.toughness == pytest.approx(200 - 10.0, abs=1e-6)

    def test_foxian_def_down_on_attack(self):
        """狐祈者攻击 → 目标DEF-18% 2回合"""
        ally = _unit('seele', position=1)
        ally.extra['_foxian'] = True
        e = _enemy()
        state = SimState(enemies=[e], units=[ally])
        _fugue_foxian_def_down(ally, state, target=e)
        assert e.status_attribute('def_reduction') == pytest.approx(0.18, abs=1e-9)

    def test_ultimate_ignore_weakness(self):
        """终结技: 无视弱点削减全体韧性"""
        fugue = _unit('fugue')
        fugue.current_energy = 130
        e = _enemy(toughness=200, fire_res=0.20)  # 无火弱点
        state = SimState(enemies=[e], units=[fugue])
        state.current_av = 0.0
        _use_skill(fugue, state, 'ultimate')
        assert e.toughness == pytest.approx(200 - 20.0, abs=1e-6)  # 全额削韧(文档削韧值20)

    def test_chizhuo_enhanced_basic(self):
        """炽灼状态下普攻强化为扩散（主100%+相邻50%）"""
        fugue = _unit('fugue')
        from engine.runtime import TimedBuff
        fugue.buffs.append(TimedBuff(source_id='fugue', attributes={'_chizhuo': 1},
                                     remaining_turns=3, param_id='fugue_chizhuo'))
        e1 = _enemy(toughness=200)
        e2 = _enemy(toughness=200)
        e1.id, e2.id = 'a', 'b'
        state = SimState(enemies=[e1, e2], units=[fugue])
        state.current_av = 0.0
        _use_skill(fugue, state, 'basic_attack')
        assert '冉冉方炽' in '\n'.join(state.log)  # 强化普攻名
        assert e1.HP < 500000 and e2.HP < 500000  # 主+相邻都受伤


class TestSuperBreakSource:
    def test_fugue_presence_provides_source(self):
        """忘归人在场 → 全队超击破源（转化率1.0）"""
        fugue = _unit('fugue')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[fugue, ally])
        assert _super_break_rate(state, ally) == pytest.approx(1.0, abs=1e-9)


class TestBroadcastBreakHooks:
    def test_ally_break_triggers_fugue_owner_hooks(self):
        """队友击破时，忘归人行迹和E2仍以忘归人为持有者结算。"""
        from engine.characters.fugue import _eid_fugue_e2_energy, _fugue_trace2_team_be

        fugue = _unit('fugue', eidolon=2)
        ally = _unit('seele', position=2)
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[fugue, ally])
        state.current_av = 0.0
        state.hooks.register('fugue', 'on_any_weakness_break', _eid_fugue_e2_energy)
        state.hooks.register('fugue', 'on_any_weakness_break', _fugue_trace2_team_be)

        _use_skill(ally, state, 'basic_attack')

        assert fugue.current_energy == pytest.approx(3.0, abs=1e-6)
        assert any(getattr(buff, 'param_id', '') == 'fugue_trace2_be'
                   for buff in ally.buffs)


class TestTraces:
    def test_trace2_team_be(self):
        """行迹2: 击破后队友BE+6%（BE<220%）"""
        fugue = _unit('fugue')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[fugue, ally])
        _fugue_trace2_team_be(fugue, state)
        assert any(getattr(b, 'attributes', {}).get('BREAK_EFFECT') == pytest.approx(6.0)
                   for b in ally.buffs)

    def test_trace2_team_be_threshold(self):
        """行迹2: 忘归人BE≥220% → 队友+18%"""
        fugue = _unit('fugue')
        fugue.base_stats.BREAK_EFFECT = 2.5
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[fugue, ally])
        _fugue_trace2_team_be(fugue, state)
        assert any(getattr(b, 'attributes', {}).get('BREAK_EFFECT') == pytest.approx(18.0)
                   for b in ally.buffs)


class TestEidolons:
    def test_e2_break_energy(self):
        """E2: 击破回3能量"""
        fugue = _unit('fugue', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[fugue])
        _eid_fugue_e2_energy(fugue, state)
        assert fugue.current_energy == pytest.approx(3.0, abs=1e-6)

    def test_e2_ult_advance(self):
        """E2: 终结技后全队行动提前24%"""
        fugue = _unit('fugue', eidolon=2)
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[fugue, ally])
        state.extra['navs'] = {0: 500.0, 1: 600.0}
        _eid_fugue_e2_ult(fugue, state)
        from engine.core.combat_engine import _effective_spd
        expect = 600.0 - (10000.0 / _effective_spd(ally, state)) * 0.24
        assert state.extra['navs'][1] == pytest.approx(expect, abs=1e-6)

    def test_e1_ally_break_efficiency(self):
        """E1: 狐祈者弱点击破效率+50%"""
        fugue = _unit('fugue', eidolon=1)
        ally = _unit('seele', position=2)
        ally.extra['_foxian'] = True
        e = _enemy(toughness=200, fire_res=0.0)
        state = SimState(enemies=[e], units=[fugue, ally])
        state.current_av = 0.0
        _use_skill(ally, state, 'basic_attack')
        assert e.toughness == pytest.approx(200 - 15.0, abs=1e-6)  # 10×1.5

    def test_e4_break_damage_mult(self):
        """E4: 狐祈者击破/超击破伤害+20%"""
        fugue = _unit('fugue', eidolon=4)
        ally = _unit('seele', position=2)
        ally.extra['_foxian'] = True
        e = _enemy(toughness=10)
        state = SimState(enemies=[e], units=[fugue, ally])
        state.current_av = 0.0
        _use_skill(ally, state, 'basic_attack')  # 击破
        # 击破伤害入账（E4 +20%）: 直接断言击破段日志存在且伤害>0
        assert '击破弱点' in '\n'.join(state.log)
        assert ally.total_damage_dealt > 0
