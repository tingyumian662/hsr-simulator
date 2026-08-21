"""v5.7 审计修复回归测试（文档-代码出入修正, 2026-08-13）

覆盖批次1/2 的数值修正:
- 逐倍率目标（万敌/衣匠/迷梦/昔涟强化普攻/爻光）
- 万敌E1 全体按主倍率 + 防跨战斗污染
- 阿格莱雅双回血/立即行动/织线易伤单点
- 流萤E6 加算/E6能量
- 昔涟结界倒计时
- 万敌E2重置/行迹1门槛
- 长夜月至暗免疫控制
- 风堇治疗分布/雨过天晴HP回退
- 光锥叠影档取值
- TBR 坏人麻烦削韧
"""
import copy
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import (
    simulate, SimState, SimUnit, _use_skill, _select_targets, _enemy_for_damage,
    _toughness_efficiency, _apply_hit, _gain_energy,
)
from engine.core.attributes import compute_combat_stats


def _enemy(hp=500000, toughness=200, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                     '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _three_enemies():
    e1, e2, e3 = _enemy(), _enemy(), _enemy()
    e1.id, e2.id, e3.id = 'a', 'b', 'c'
    return [e1, e2, e3]


class TestMultTarget:
    """v5.7: 逐倍率目标——主倍率只打主目标, 相邻倍率只打相邻"""

    def test_mydei_skill_main_vs_adjacent(self):
        u = _unit('mydei')
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        hp = [e.HP for e in (e1, e2, e3)]
        _use_skill(u, state, 'skill')
        # 文档: 主90%HP / 相邻50%HP（此前两个倍率各打满3目标）
        d_main = hp[0] - e1.HP
        d_adj = hp[1] - e2.HP
        assert d_main > 0 and d_adj > 0
        assert d_main / d_adj == pytest.approx(90 / 50, rel=1e-6)
        assert d_adj == pytest.approx(hp[2] - e3.HP, rel=1e-6)

    def test_mydei_e1_shenshen_all_enemies_at_main_mult(self):
        """E1: 弑神登神全体按主目标倍率 280×1.3=364%"""
        u = _unit('mydei', eidolon=1)
        u.extra['mydei_charge'] = 150
        u.extra['is_blood_debt'] = True
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        hp = [e.HP for e in (e1, e2, e3)]
        _use_skill(u, state, 'skill_shenshen')
        d0, d1, d2 = [hp[i] - e.HP for i, e in enumerate((e1, e2, e3))]
        for i, d in enumerate((d0, d1, d2)):
            assert d == pytest.approx(d0, rel=1e-9), f'敌{i}应同受主倍率（全体364%）而非主+相邻叠加'
        assert d0 > 0

    def test_mydei_e1_no_cross_battle_pollution(self):
        """E1 deepcopy: 同配置模拟两次, 倍率不叠乘"""
        from engine.core.combat_sim import simulate
        c = load_character('mydei', 'data/characters')
        s1 = simulate([{'char': c, 'position': 1, 'eidolon': 1}], _enemy(), max_av=600)
        s2 = simulate([{'char': c, 'position': 1, 'eidolon': 1}], _enemy(), max_av=600)
        d1 = sum(u.total_damage_dealt for u in s1.units)
        d2 = sum(u.total_damage_dealt for u in s2.units)
        assert d1 == pytest.approx(d2, rel=1e-9)

    def test_aglaea_memsprite_skill_main_vs_adjacent(self):
        """衣匠刺纹之陷: 主110%/相邻66%"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('aglaea')
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        hp = [e.HP for e in (e1, e2, e3)]
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        d_main = hp[0] - e1.HP
        d_adj = hp[1] - e2.HP
        assert d_main / d_adj == pytest.approx(110 / 66, rel=1e-6)

    def test_changyeyue_mimeng_main_vs_others(self):
        """迷梦: 主12%×忆质 / 其他6%×忆质（此前每敌18%）"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('changyeyue')
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)  # yizhi=0, 召唤立即行动只普攻
        ms = u.memsprite_unit
        u.yizhi = 16  # 召唤后再设忆质, 避免召唤立即行动直接迷梦消失
        hp = [e.HP for e in (e1, e2, e3)]
        rem._use_memsprite_skill(state, u, ms, 'memsprite_skill')
        d_main = hp[0] - e1.HP
        d_adj = hp[1] - e2.HP
        assert d_main / d_adj == pytest.approx(12 / 6, rel=1e-6)
        assert d_adj == pytest.approx(hp[2] - e3.HP, rel=1e-6)

    def test_xilian_enhanced_basic_main_plus_all(self):
        """昔涟强化普攻: 主30% + 全体30%（此前全体段只打主目标）"""
        u = _unit('xilian')
        e1, e2, e3 = _three_enemies()
        state = SimState(enemies=[e1, e2, e3], units=[u])
        hp = [e.HP for e in (e1, e2, e3)]
        _use_skill(u, state, 'basic_attack_enhanced')
        d_main = hp[0] - e1.HP
        d_adj = hp[1] - e2.HP
        assert d_main / d_adj == pytest.approx(60 / 30, rel=1e-6)  # 主吃两段: 主30+全体30
        assert d_adj == pytest.approx(hp[2] - e3.HP, rel=1e-6)

    def test_select_targets_all_except_main(self):
        alive = _three_enemies()
        assert len(_select_targets(alive, 'all_except_main')) == 2
        assert _select_targets(alive, 'all_except_main')[0].id == 'b'


class TestAglaeaFixes:
    def test_skill_heal_single_50_when_present(self):
        """战技已在场: 只回50%（此前通用回血+heal effect=100%）"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('aglaea')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.current_hp = ms.max_hp * 0.4
        _use_skill(u, state, 'skill')
        assert ms.current_hp == pytest.approx(ms.max_hp * 0.9, rel=1e-6)

    def test_sovereign_immediate_action(self):
        """终结技→自身立即行动"""
        u = _unit('aglaea')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {0: 10000.0}
        state.current_av = 50.0
        u.current_energy = 350
        _use_skill(u, state, 'ultimate')
        assert state.extra['navs'][0] == pytest.approx(50.0, rel=1e-9)

    def test_gossamer_vuln_single_point(self):
        """织线易伤单点: _enemy_for_damage 对所有伤害来源生效"""
        e = _enemy()
        e.extra['gossamer_dmg_bonus'] = 0.15
        view = _enemy_for_damage(e)
        assert view.vulnerability == pytest.approx(0.15, abs=1e-9)
        e2 = _enemy()
        assert _enemy_for_damage(e2) is e2  # 无易伤不复制

    def test_enhanced_basic_no_sp_recover(self):
        """孤锋千吻无法恢复战技点"""
        u = _unit('aglaea')
        state = SimState(enemies=[_enemy()], units=[u])
        state.skill_points = 2
        _use_skill(u, state, 'basic_attack_enhanced')
        assert state.skill_points == 2
        # 昔涟强化普攻同样不回
        x = _unit('xilian')
        state2 = SimState(enemies=[_enemy()], units=[x])
        state2.skill_points = 2
        _use_skill(x, state2, 'basic_attack_enhanced')
        assert state2.skill_points == 2


class TestFireflyFixes:
    def test_enhanced_skill_no_energy_regen(self):
        """强化战技标准回能=0, 只固定回60%能量上限"""
        u = _unit('firefly')
        u.extra['combustion'] = True
        state = SimState(enemies=[_enemy()], units=[u])
        u.current_energy = 0
        _use_skill(u, state, 'skill')
        assert u.current_energy == pytest.approx(240 * 0.60, rel=1e-6)

    def test_e6_toughness_efficiency_additive(self):
        """E6 击破效率加算: 燃烧1.5+E6 0.5=2.0（用户确认实机口径）"""
        u = _unit('firefly', eidolon=6)
        u.extra['combustion'] = True
        state = SimState(enemies=[_enemy()], units=[u])
        eff = _toughness_efficiency(u, state, 'skill_enhanced')
        assert eff == pytest.approx(2.0, abs=1e-9)


class TestRealmCountdown:
    def test_xilian_field_expires_after_2_turns(self):
        """结界2回合到期解除（此前 realm_turns 无消费点=永久）"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xilian')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        _use_skill(u, state, 'skill')
        assert state.realm_owner == 'xilian'
        assert state.realm_turns == 2
        rem.tick_turn(state, u)
        assert state.realm_turns == 1
        rem.tick_turn(state, u)
        assert state.realm_owner == ''
        assert state.realm_true_dmg == 0

    def test_field_cleared_on_death(self):
        """昔涟阵亡→结界解除"""
        from engine.core.combat_sim import _check_fatal
        u = _unit('xilian')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        assert state.realm_owner == 'xilian'
        u.current_hp = 0
        _check_fatal(state, u)
        assert state.realm_owner == ''


class TestMydeiE2Reset:
    def test_e2_heal_convert_resets_on_any_turn(self):
        """E2: 任意单位行动后重置累计（此前40上限变整场累计）"""
        from engine.core.combat_sim import _begin_regular_turn
        u = _unit('mydei', eidolon=2)
        u.extra['is_blood_debt'] = True
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        u.extra['e2_heal_converted'] = 30.0
        _begin_regular_turn(state, ally)
        assert u.extra['e2_heal_converted'] == 0.0


class TestChangyeyueImmunity:
    def test_darkness_control_immune(self):
        """至暗之谜期间免疫控制类负面状态"""
        from engine.core.combat_sim import _apply_player_status
        from engine.core.combat_sim import PlayerStatus
        u = _unit('changyeyue')
        u.is_darkness = True
        state = SimState(enemies=[_enemy()], units=[u])
        st = PlayerStatus(id='frozen', name='冻结', category='control',
                          remaining_turns=2, attributes={})
        assert _apply_player_status(state, u, st) is False

    def test_yizhi_16_control_immune(self):
        """忆质≥16 免疫控制"""
        from engine.core.combat_sim import _apply_player_status
        from engine.core.combat_sim import PlayerStatus
        u = _unit('changyeyue')
        u.is_darkness = False
        u.yizhi = 16
        state = SimState(enemies=[_enemy()], units=[u])
        st = PlayerStatus(id='frozen', name='冻结', category='control',
                          remaining_turns=2, attributes={})
        assert _apply_player_status(state, u, st) is False


class TestFengjinFixes:
    def test_skill_heal_split(self):
        """战技: 除小伊卡外全队8%+160 / 小伊卡10%+200（按风堇上限）"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('fengjin')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ally.current_hp = ally.max_hp * 0.7  # 掉血后治疗可见; >50%避免行迹2 +25%加成干扰
        u.current_hp = u.max_hp * 0.7
        ms.current_hp = ms.max_hp * 0.7
        ally_hp0, ms_hp0, u_hp0 = ally.current_hp, ms.current_hp, u.current_hp
        _use_skill(u, state, 'skill')
        hp_base = u.base_stats.HP
        assert ally.current_hp - ally_hp0 == pytest.approx(hp_base * 0.08 + 160, rel=1e-6)
        assert u.current_hp - u_hp0 == pytest.approx(hp_base * 0.08 + 160, rel=1e-6)
        assert ms.current_hp - ms_hp0 == pytest.approx(
            min(hp_base * 0.10 + 200, ms.max_hp - ms_hp0), rel=1e-6)  # 满血钳制

    def test_clear_sky_hp_revert(self):
        """雨过天晴3回合结束→全队HP上限回退"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('fengjin')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        u.current_energy = 140
        from engine.core.combat_sim import _ult_post
        _ult_post(state, u)
        orig = ally.extra['clear_sky_orig_maxhp']
        assert ally.max_hp == pytest.approx(orig * 1.30 + 600, rel=1e-6)
        for _ in range(3):
            rem.tick_turn(state, u)
        assert ally.max_hp == pytest.approx(orig, rel=1e-9)


class TestLightConeRanks:
    def test_planetary_rendezvous_rank_values(self):
        """与行星相会: S5=24%（values 按叠影档）"""
        from engine.models.equipment import load_lightcone
        from engine.core.combat_sim import _lc_same_element_dmg_bonus, _lc_rank_value
        lc = load_lightcone('planetary_rendezvous')
        u = _unit('seele')
        u.lightcone = lc
        assert _lc_rank_value(u, 12.0) == 12.0
        u.lightcone.rank = 5
        assert _lc_rank_value(u, 12.0) == 24.0

    def test_swordplay_rank_consumption(self):
        """论剑: 层数增伤按叠影档（v5.7 用户实机确认: S1-S5 = 8/10/12/14/16%/层）"""
        from engine.models.equipment import load_lightcone
        from engine.core.combat_sim import _lc_target_correct, _build_effective_stats
        lc = load_lightcone('swordplay')
        u = _unit('seele')
        u.lightcone = lc
        u.extra['swordplay_tid'] = 'x'
        u.extra['swordplay_layers'] = 5
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        s = _build_effective_stats(u, state)
        t = _lc_target_correct(s, u, state, e)
        assert t.DMG_BONUS_ALL == pytest.approx(s.DMG_BONUS_ALL + 0.08 * 5, abs=1e-9)
        # S5 = 16%/层
        u.lightcone.rank = 5
        t5 = _lc_target_correct(_build_effective_stats(u, state), u, state, e)
        assert t5.DMG_BONUS_ALL == pytest.approx(s.DMG_BONUS_ALL + 0.16 * 5, abs=1e-9)


class TestTbrToughness:
    def test_mimi_bounce_toughness_total(self):
        """坏人麻烦: 弹射4跳×5 + 全体10 = 总削韧30"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('trailblazer_remembrance')
        e = _enemy(toughness=300)
        state = SimState(enemies=[e], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert 300 - e.toughness == pytest.approx(30.0, abs=1e-9)

    def test_tbr_ultimate_mimi_damage_and_toughness(self):
        """v5.7: TBR终结技迷迷240%ATK全体伤害 + 削韧20（此前伤害段缺失）"""
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('trailblazer_remembrance')
        e = _enemy(toughness=300)
        state = SimState(enemies=[e], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        hp0 = e.HP
        u.current_energy = 160
        _use_skill(u, state, 'ultimate')
        assert hp0 - e.HP > 0  # 迷迷240%ATK伤害
        assert 300 - e.toughness == pytest.approx(20.0, abs=1e-9)
