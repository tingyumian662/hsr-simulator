"""v6.7b 回归: Harness 复审修复 GPT/Codex 遗留路径 + 深审新发现（27 探针落盘）

覆盖: 大丽花(行迹1单位/E6单位/E2败谢弱点/FUA清场回退/结界幂等/败谢快照/E1作用域/
共舞者重绑/终结技均分/战技开结界/行迹3条件/FUA回能)、姬子(E6火抗穿/E4抗穿单位/
歼破充能接入/E6源能自用/战技回满次数)、火花(陷阱扣SP/E2层数上限/战技回能0/
强化普攻回能40)、绯英(E6增笑/天赋暴伤转欢愉度/欢愉技回能好活)、秘技开怪者门控。
语义依据: 角色技能介绍/*.txt + GPT/Codex 审查路径 + HARNESS_HANDOFF v6.7b"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _use_skill, _build_effective_stats, _dahlia_field_apply,
    _dahlia_super_break_rate, _dahlia_fua, _apply_dahlia_baisie, _dahlia_field_active,
    _dahlia_ensure_dancers, _dahlia_e1_flat, _hn_support_skill, _hn_count_hits,
    _hn_support_cap, _sparxie_enhanced_settle, _deduct_skill_point_cost,
    _register_elation_skill_hooks, _apply_toughness_damage, _evanescia_fox_teacher_fua,
)
from engine.core.effect_resolver import (
    _trace_dahlia_trace1_open, _trace_dahlia_trace3_implant, _eid_dahlia_e2,
    _eid_dahlia_e6, _trace_hn_protocol, _eid_hn_e6, _trace_evanescia_talent_elation,
)
from engine.systems.elation import ElationSystem
from engine.core.combat_utils import apply_techniques, _tech_evanescia

R = {'冰': 0, '量子': 0, '风': 0, '雷': 0, '虚数': 0, '物理': 0, '火': 0.2}


def _enemy(hp=500000, toughness=200, broken=False, res=None, name='X'):
    e = Enemy(id='x', name=name, HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness, max_toughness=toughness, level=80,
              element_res=dict(res or R))
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


def _elation(state):
    state.extra['_elation'] = ElationSystem()
    return state.extra['_elation']


class TestDahlia:
    """大丽花: 单位换算/败谢/E2/FUA/结界/共舞者/均分"""

    def test_trace1_be_bonus_units(self):
        """行迹1: 队友BE+(24%×大丽花BE+50%)——buff 存百分比原始数值"""
        u = _unit('the_dahlia')
        u.base_stats.BREAK_EFFECT = 1.5  # 150%
        ally = _unit('seele', position=2)
        st = SimState(enemies=[_enemy()], units=[u, ally])
        _trace_dahlia_trace1_open(u, st)
        b = next(x for x in ally.buffs if getattr(x, 'source_name', '') == '又一场葬礼')
        assert b.attributes['BREAK_EFFECT'] == pytest.approx(86.0)
        s = _build_effective_stats(ally, st)
        assert s.BREAK_EFFECT - ally.base_stats.BREAK_EFFECT == pytest.approx(0.86)

    def test_e6_be_150_pct(self):
        """E6: 共舞者BE+150%（此前 1.50 被 /100 成 +1.5%）"""
        u = _unit('the_dahlia', eidolon=6)
        st = SimState(enemies=[_enemy()], units=[u])
        st.extra['dahlia_dancers'] = ['the_dahlia']
        _eid_dahlia_e6(u, st)
        b = next(x for x in u.buffs if getattr(x, 'source_name', '') == '大丽花E6')
        assert b.attributes['BREAK_EFFECT'] == pytest.approx(150.0)
        s = _build_effective_stats(u, st)
        assert s.BREAK_EFFECT - u.base_stats.BREAK_EFFECT == pytest.approx(1.5)

    def test_e2_baisie_with_weakness(self):
        """E2: 入场败谢含共舞者属性弱点 + 全抗-20%"""
        u = _unit('the_dahlia', eidolon=2)
        ally = _unit('seele', position=2)
        e = _enemy(res={'冰': 0, '量子': 0.4, '风': 0, '雷': 0, '虚数': 0, '物理': 0, '火': 0.4})
        st = SimState(enemies=[e], units=[u, ally])
        st.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        _eid_dahlia_e2(u, st)
        assert e.has_status(status_id='the_dahlia_baisie')
        assert e.element_res['量子'] <= -0.2  # 败谢弱点
        assert e.element_res['物理'] == pytest.approx(-0.2)  # 全抗-20%

    def test_fua_clears_targets_and_counts_kill(self):
        """FUA: 目标死亡后剩余段回退存活目标; 击杀计数不重复"""
        d = _unit('the_dahlia', position=1)
        ally = _unit('seele', position=2)
        e1 = _enemy(hp=10, toughness=1, name='A')
        e2 = _enemy(hp=500000, toughness=1, name='B')
        st = SimState(enemies=[e1, e2], units=[d, ally])
        st.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        _dahlia_fua(st)
        assert st.extra.get('killed_total', 0) == 1  # 打死1只且不重复计数
        assert d.extra.get('killed_total') is None

    def test_fua_gains_2_energy(self):
        """FUA 回2能量（txt 天赋能量恢复2）"""
        d = _unit('the_dahlia')
        st = SimState(enemies=[_enemy()], units=[d])
        st.extra['dahlia_dancers'] = ['the_dahlia']
        e0 = d.current_energy
        _dahlia_fua(st)
        assert d.current_energy - e0 == pytest.approx(2.0)

    def test_field_no_stack(self):
        """结界重复施放: 击破效率不叠加"""
        u = _unit('the_dahlia')
        ally = _unit('seele', position=2)
        st = SimState(enemies=[_enemy()], units=[u, ally])
        _dahlia_field_apply(st, u)
        _dahlia_field_apply(st, u)
        assert _build_effective_stats(u, st).TOUGHNESS_EFFICIENCY == pytest.approx(1.5)

    def test_skill_opens_field(self):
        """战技开启结界: 此前只加buff不设 dahlia_field_turns（P1）"""
        u = _unit('the_dahlia')
        st = SimState(enemies=[_enemy()], units=[u])
        st.skill_points = 3
        _use_skill(u, st, 'skill')
        assert _dahlia_field_active(st) is True
        # 未破韧目标削韧→结界超击破（火弱点目标）
        e = _enemy(toughness=200, broken=False, res={'冰': 0, '量子': 0, '风': 0,
                                                    '雷': 0, '虚数': 0, '物理': 0, '火': -0.2})
        _apply_toughness_damage(st, u, e, 10.0, '火', 'skill', _build_effective_stats(u, st))
        assert any('超击破' in l for l in st.log)

    def test_baisie_snapshot_kept(self):
        """败谢重复施放: 弱点快照保留首次原始抗性"""
        u = _unit('the_dahlia')
        ally = _unit('seele', position=2)
        e = _enemy(res={'冰': 0, '量子': 0.4, '风': 0, '雷': 0, '虚数': 0, '物理': 0, '火': 0.4})
        st = SimState(enemies=[e], units=[u, ally])
        st.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        _apply_dahlia_baisie(u, st, e)
        _apply_dahlia_baisie(u, st, e)
        ws = next(s for s in e.statuses if s.id == 'dahlia_weak_量子')
        assert ws.attributes.get('weakness_old_res') == pytest.approx(0.4)

    def test_e1_not_applied_to_unbroken_field(self):
        """E1 超击破倍率不作用于未破韧结界转化"""
        u = _unit('the_dahlia', eidolon=1)
        t = _enemy(toughness=200, broken=False)
        st = SimState(enemies=[t], units=[u])
        st.extra['dahlia_dancers'] = ['the_dahlia']
        st.extra['dahlia_field_turns'] = 3
        assert _dahlia_super_break_rate(st, u, t) == pytest.approx(0.6)

    def test_e1_flat_scope(self):
        """E1 固定削韧: 仅受击目标 + 大丽花自身攻击也触发"""
        d = _unit('the_dahlia', eidolon=1, position=1)
        ally = _unit('seele', position=2)
        e1 = _enemy(toughness=200, name='A')
        e2 = _enemy(toughness=200, name='B')
        st = SimState(enemies=[e1, e2], units=[d, ally])
        st.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        st.extra['last_attack_targets'] = [e1]
        _dahlia_e1_flat(st, d)
        assert e1.toughness < 200  # 受击目标被削
        assert e2.toughness == pytest.approx(200)  # 未受击目标不动

    def test_dancer_rebind_on_death(self):
        """共舞者死亡→重绑击破特攻最高队友"""
        d = _unit('the_dahlia', position=1)
        ally = _unit('seele', position=2)
        fx = _unit('firefly', position=3)
        st = SimState(enemies=[_enemy()], units=[d, ally, fx])
        st.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        ally.is_alive = False
        _dahlia_ensure_dancers(st)
        assert 'seele' not in st.extra['dahlia_dancers']
        assert st.extra['dahlia_dancers'][0] == 'the_dahlia'

    def test_ultimate_split(self):
        """终结技 300% 由全体均分: 2敌总伤 ≈ 1敌总伤"""
        u = _unit('the_dahlia')
        e1 = _enemy(hp=500000, toughness=100)
        st1 = SimState(enemies=[e1], units=[u])
        u.current_energy = 130
        _use_skill(u, st1, 'ultimate')
        dmg1 = 500000 - e1.HP
        u2 = _unit('the_dahlia')
        ea, eb = _enemy(hp=500000, toughness=100), _enemy(hp=500000, toughness=100)
        st2 = SimState(enemies=[ea, eb], units=[u2])
        u2.current_energy = 130
        _use_skill(u2, st2, 'ultimate')
        dmg2 = (500000 - ea.HP) + (500000 - eb.HP)
        assert dmg2 == pytest.approx(dmg1, rel=0.05)

    def test_trace3_fire_character(self):
        """行迹3: 火属性角色添加任意元素弱点→固定削韧20（非仅火弱点）"""
        u = _unit('the_dahlia')
        fx = _unit('firefly', position=2)
        e = _enemy(toughness=200)
        st = SimState(enemies=[e], units=[u, fx])
        _trace_dahlia_trace3_implant(fx, st, element='量子', target=e)
        assert e.toughness == pytest.approx(180.0)

    def test_trace1_reapply_on_heal(self):
        """行迹1: 受队友治疗→再次触发3回合（单回合1次）"""
        from engine.core.effect_resolver import _trace_dahlia_trace1_reapply
        u = _unit('the_dahlia')
        ally = _unit('luocha' if False else 'seele', position=2)
        st = SimState(enemies=[_enemy()], units=[u, ally])
        _trace_dahlia_trace1_reapply(ally, st, targets=[u])
        n1 = len([b for b in ally.buffs if getattr(b, 'source_name', '') == '又一场葬礼'])
        assert n1 == 1
        assert u.extra.get('dahlia_trace1_used') is True
        # 单回合内不重复
        _trace_dahlia_trace1_reapply(ally, st, targets=[u])
        assert len([b for b in ally.buffs
                    if getattr(b, 'source_name', '') == '又一场葬礼']) == 1


class TestHimeko:
    """姬子·启行: E6/E4 单位、歼破充能、源能、战技回满"""

    def test_e6_fire_res_pen(self):
        u = _unit('himeko_nova', eidolon=6)
        st = SimState(enemies=[_enemy()], units=[u])
        _eid_hn_e6(u, st)
        assert _build_effective_stats(u, st).RES_PEN.get('火', 0.0) == pytest.approx(0.20)

    def test_e4_res_pen_buff_units(self):
        hn = _unit('himeko_nova', eidolon=4)
        ally = _unit('seele', position=2)
        st = SimState(enemies=[_enemy()], units=[hn, ally])
        st.extra['hn_support_uses'] = 1
        _hn_support_skill(st, ally)
        b = next(x for x in ally.buffs if getattr(x, 'source_name', '') == '姬子E4抗穿')
        assert b.attributes['RES_PEN_ALL'] == pytest.approx(20.0)
        assert _build_effective_stats(ally, st).RES_PEN_ALL == pytest.approx(0.20)

    def test_support_counts_charge(self):
        """助战技命中计入歼破充能"""
        hn = _unit('himeko_nova', position=1)
        ally = _unit('changyeyue', position=2)
        st = SimState(enemies=[_enemy()], units=[hn, ally])
        st.extra['hn_support_uses'] = 3
        _trace_hn_protocol(hn, st)
        c0 = st.extra.get('hn_charge', 0)
        _hn_support_skill(st, ally)
        assert st.extra.get('hn_charge', 0) > c0

    def test_e6_source_energy_self_use(self):
        hn = _unit('himeko_nova', eidolon=6)
        st = SimState(enemies=[_enemy()], units=[hn])
        st.extra['hn_support_uses'] = 1
        _hn_support_skill(st, hn)
        assert hn.extra.get('hn_source_energy', 0) == 1

    def test_skill_restores_support_uses(self):
        """战技立即恢复所有助战技使用次数（txt）"""
        hn = _unit('himeko_nova')
        st = SimState(enemies=[_enemy()], units=[hn])
        st.extra['hn_support_uses'] = 0
        st.skill_points = 3
        _use_skill(hn, st, 'skill')
        assert st.extra['hn_support_uses'] == _hn_support_cap(hn)

    def test_charge_skill_cd_flag(self):
        """歼破协议: 战技暴伤额外+100%标记"""
        hn = _unit('himeko_nova', position=1)
        ally = _unit('changyeyue', position=2)
        st = SimState(enemies=[_enemy()], units=[hn, ally])
        _trace_hn_protocol(hn, st)
        assert st.extra.get('hn_charge_skill_cd') is True


class TestSparxie:
    """火花: 陷阱扣SP/E2层数上限/回能口径"""

    def test_trap_costs_sp(self):
        u = _unit('sparxie')
        st = SimState(enemies=[_enemy()], units=[u])
        _elation(st)
        u.extra['sparxie_trap_uses'] = 1
        st.skill_points = 5
        st.max_sp = 20
        _sparxie_enhanced_settle(st, u)
        assert u.extra['sparxie_trap_uses'] == 0
        assert st.skill_points in (4, 6)  # 扣1, 红红火火礼物可能+2

    def test_e2_cd_stack_cap(self):
        """E2 每消耗1爆点暴伤+10% 总层数≤4"""
        u = _unit('sparxie', eidolon=2)
        st = SimState(enemies=[_enemy()], units=[u])
        st.extra['sparxie_burst_points'] = 8.0
        _deduct_skill_point_cost(st, u, 4)
        _deduct_skill_point_cost(st, u, 4)
        cd = sum(getattr(x, 'attributes', {}).get('CRIT_DMG', 0)
                 for x in u.buffs if getattr(x, 'param_id', '') == 'sparxie_e2_cd')
        assert cd == pytest.approx(40.0)

    def test_skill_no_energy(self):
        """战技无能量恢复（txt 无回能行）"""
        u = _unit('sparxie')
        st = SimState(enemies=[_enemy()], units=[u])
        st.skill_points = 3
        _register_elation_skill_hooks(st.skill_hooks)
        e0 = u.current_energy
        _use_skill(u, st, 'skill')
        assert u.current_energy - e0 == pytest.approx(0.0)

    def test_enhanced_basic_energy_40(self):
        """强化普攻【百花齐放】能量恢复40"""
        u = _unit('sparxie')
        st = SimState(enemies=[_enemy()], units=[u])
        _register_elation_skill_hooks(st.skill_hooks)
        u.extra['sparxie_live'] = True
        e0 = u.current_energy
        _use_skill(u, st, 'basic_attack')
        assert u.current_energy - e0 == pytest.approx(40.0)


class TestEvanescia:
    """绯英: E6增笑/天赋欢愉度/欢愉技回能好活"""

    def test_e6_laugh_boost(self):
        u = _unit('evanescia', eidolon=6)
        st = SimState(enemies=[_enemy()], units=[u])
        _elation(st)
        assert _build_effective_stats(u, st).LAUGH_BOOST == pytest.approx(0.15)

    def test_e6_laugh_boost_per_100_goodshow(self):
        u = _unit('evanescia', eidolon=6)
        st = SimState(enemies=[_enemy()], units=[u])
        elation = _elation(st)
        elation.grant_good_show(st, 'evanescia', 250.0)
        # 250好活 → 2层 → +0.04
        assert _build_effective_stats(u, st).LAUGH_BOOST == pytest.approx(0.19)

    def test_talent_elation_from_critdmg(self):
        u = _unit('evanescia')
        st = SimState(enemies=[_enemy()], units=[u])
        cd0 = u.base_stats.CRIT_DMG
        el0 = u.base_stats.ELATION_LEVEL
        _trace_evanescia_talent_elation(u, st)
        assert u.base_stats.ELATION_LEVEL - el0 == pytest.approx(cd0 * 0.20)

    def test_elation_skill_energy_and_goodshow(self):
        """欢愉技: 回5能量 + 额外5好活当赏（txt）"""
        u = _unit('evanescia')
        st = SimState(enemies=[_enemy()], units=[u])
        _elation(st)
        _register_elation_skill_hooks(st.skill_hooks)
        e0 = u.current_energy
        _use_skill(u, st, 'elation_skill')
        assert u.current_energy - e0 == pytest.approx(10.0)  # 5回能 + 5好活→5能互转
        assert st.elation_state.get_good_show_total('evanescia') == pytest.approx(5.0)

    def test_fox_fua_direct_plus_elation_segment(self):
        """狐狸老师FUA: 主段普通物理直伤 + 持好活25%欢愉段"""
        u = _unit('evanescia')
        st = SimState(enemies=[_enemy()], units=[u])
        elation = _elation(st)
        elation.grant_good_show(st, 'evanescia', 20.0)
        d0 = u.total_damage_dealt
        e0 = u.current_energy  # 20（好活→能量互转）
        _evanescia_fox_teacher_fua(st, u)
        assert u.total_damage_dealt > d0
        assert u.current_energy - e0 == pytest.approx(10.0)  # FUA 回10能


class TestTechniqueGating:
    """秘技: 进战秘技开怪者门控"""

    def test_non_opener_evanescia_no_effect(self):
        hn = _unit('himeko_nova', position=1)
        ev = _unit('evanescia', position=2)
        st = SimState(enemies=[_enemy()], units=[hn, ev])
        st.extra['_elation'] = ElationSystem()
        op = apply_techniques(st, [hn, ev])
        assert op == 'himeko_nova'
        assert st.elation_state.get_good_show_total('evanescia') == pytest.approx(0.0)

    def test_opener_evanescia_works(self):
        ev = _unit('evanescia', position=1)
        hn = _unit('himeko_nova', position=2)
        st = SimState(enemies=[_enemy()], units=[ev, hn])
        st.extra['_elation'] = ElationSystem()
        _tech_evanescia(st, ev, is_opener=True)
        assert st.elation_state.get_good_show_total('evanescia') == pytest.approx(20.0)
