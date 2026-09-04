"""v6.7 批1 回归: 绯英（欢愉·物理）+ 火花（欢愉·火）+ 大丽花（虚无·火）

语义依据: 角色技能介绍/欢愉/绯英.txt、欢愉/火花.txt、虚无/大丽花.txt + CLAUDE_HANDOFF v6.7 节
用户确认（2026-08-15）: 火花直播连线=仅下次普攻强化(一次性); 大丽花结界未破韧转化率=与天赋同率60%;
基础属性以 txt 手抄为准"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _gain_energy, _gain_skill_points, _apply_toughness_damage, _build_effective_stats, _deduct_skill_point_cost
from engine.characters.the_dahlia import _dahlia_talent_open, _dahlia_field_apply, _dahlia_super_break_rate, _dahlia_on_ally_attack, _apply_dahlia_baisie
from engine.characters.sparxie import _sparxie_enhanced_settle
from engine.runtime import SimState, SimUnit
from engine.systems.elation import ElationSystem
from engine.characters.the_dahlia import _trace_dahlia_trace3_implant
from engine.characters.evanescia import _trace_evanescia_energy_convert


def _enemy(hp=500000, toughness=200, broken=False, res=None):
    e = Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness,
              max_toughness=toughness, level=80,
              element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                  '虚数': 0, '物理': -0.2, '火': -0.2})
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _elation_state(state):
    state.extra['_elation'] = ElationSystem()
    return state.extra['_elation']


class TestEvanescia:
    """绯英: 能量↔好活互转 / 240累计狐狸老师FUA / 行迹2/3转移 / E2乘区"""

    def test_energy_converts_to_goodshow(self):
        """能量→好活（单次≤100）; 240累计开始"""
        u = _unit('evanescia')
        state = SimState(enemies=[_enemy()], units=[u])
        _elation_state(state)
        _trace_evanescia_energy_convert(u, state, amount=20.0)
        assert state.elation_state.get_good_show_total('evanescia') == pytest.approx(20.0)
        assert u.extra.get('evanescia_energy_bank') == pytest.approx(20.0)

    def test_240_accumulate_triggers_fua(self):
        """累计240能量→狐狸老师FUA（全体欢愉伤害+回10能量）"""
        u = _unit('evanescia')
        state = SimState(enemies=[_enemy()], units=[u])
        _elation_state(state)
        for _ in range(11):
            _trace_evanescia_energy_convert(u, state, amount=20.0)
        assert u.extra['evanescia_energy_bank'] == pytest.approx(220.0)
        _trace_evanescia_energy_convert(u, state, amount=20.0)  # 240 → FUA
        assert u.extra['evanescia_energy_bank'] == pytest.approx(0.0)
        assert any('狐狸老师FUA' in l for l in state.log)
        assert u.total_damage_dealt > 0

    def test_goodshow_to_energy_locked(self):
        """好活→能量（锁防递归: 转化产生的能量不再反向转好活）"""
        u = _unit('evanescia')
        state = SimState(enemies=[_enemy()], units=[u])
        elation = _elation_state(state)
        e0 = u.current_energy
        elation.grant_good_show(state, 'evanescia', 20.0, source='aha')
        assert u.current_energy - e0 == pytest.approx(20.0)  # 方向2
        assert state.elation_state.get_good_show_total('evanescia') == pytest.approx(20.0)  # 非40

    def test_trace2_steal_on_expire(self):
        """行迹2·开不败: 队友好活到期→50%转绯英
        v6.10.3: 爻光行迹2·鸿运鳞集已实装（好活+1回合）, 故 duration=0 才会在首 tick 到期"""
        u = _unit('evanescia')
        ally = _unit('yaoguang', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        elation = _elation_state(state)
        elation.grant_good_show(state, 'yaoguang', 20.0, duration=0, source='test')
        elation.tick_turn(state, ally)  # 到期
        assert state.elation_state.get_good_show_total('evanescia') == pytest.approx(10.0)

    def test_trace3_cast_number_transfer(self):
        """行迹3·瞰众乐: 参演编号<146(爻光116)队友获好活→50%转绯英; 同编号不转"""
        u = _unit('evanescia')
        ally = _unit('yaoguang', position=2)   # cast 116 < 146
        same = _unit('evanescia', position=3)  # cast 146 非队友
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _elation_state(state)
        elation = state.extra['_elation']
        elation.grant_good_show(state, 'yaoguang', 20.0, source='aha')
        assert state.elation_state.get_good_show_total('evanescia') == pytest.approx(10.0)
        # 绯英自身获得不转移
        state2 = SimState(enemies=[_enemy()], units=[u, same])
        _elation_state(state2)
        state2.extra['_elation'].grant_good_show(state2, 'evanescia', 20.0, source='aha')
        assert state2.elation_state.get_good_show_total('evanescia') == pytest.approx(20.0)

    def test_e2_multiplier(self):
        """E2: 行迹2转移×2 / 行迹3转移×1.5"""
        u = _unit('evanescia', eidolon=2)
        ally = _unit('yaoguang', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        elation = _elation_state(state)
        # 行迹3: 队友20→转移10×1.5=15
        elation.grant_good_show(state, 'yaoguang', 20.0, source='aha')
        assert state.elation_state.get_good_show_total('evanescia') == pytest.approx(15.0)


class TestSparxie:
    """火花: 直播连线一次性 / 陷阱结算+礼物 / 爆点抵扣 / 终结技笑点 / 行迹2 / E2"""

    def test_live_once(self):
        """战技开启连线→下次普攻强化(一次性, 释放后回归正常)"""
        u = _unit('sparxie')
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.characters import register_all_elation_skill_hooks
        register_all_elation_skill_hooks(state.skill_hooks)
        _use_skill(u, state, 'skill')
        assert u.extra.get('sparxie_live') is True
        assert u.extra.get('sparxie_trap_uses') == 1
        _use_skill(u, state, 'basic_attack')
        assert not u.extra.get('sparxie_live')  # 一次性消耗
        assert any('普攻强化为' in l for l in state.log)
        assert u.total_damage_dealt > 0

    def test_trap_settle_gift(self):
        """陷阱结算: 消耗1次+礼物笑点"""
        u = _unit('sparxie')
        state = SimState(enemies=[_enemy()], units=[u])
        _elation_state(state)
        u.extra['sparxie_trap_uses'] = 1
        laugh0 = state.laugh_points
        _sparxie_enhanced_settle(state, u)
        assert u.extra['sparxie_trap_uses'] == 0
        assert state.laugh_points - laugh0 in (1, 2)  # 礼物
        assert u.total_damage_dealt > 0

    def test_burst_deduction(self):
        """爆点优先抵扣战技点（消耗爆点视为消耗战技点）"""
        u = _unit('sparxie')
        state = SimState(enemies=[_enemy()], units=[u])
        state.skill_points = 0
        state.extra['sparxie_burst_points'] = 2.0
        assert _deduct_skill_point_cost(state, u, 1) is True
        assert state.extra['sparxie_burst_points'] == pytest.approx(1.0)
        assert state.skill_points == 0  # 未扣SP
        assert state.extra.get('lc_sp_spent', 0) == 0  # 爆点抵扣不计SP消耗

    def test_ult_laugh_and_trace1(self):
        """终结技+2笑点; 行迹1按欢愉角色数1→额外2笑点+1爆点"""
        u = _unit('sparxie')
        state = SimState(enemies=[_enemy()], units=[u])
        _elation_state(state)
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert state.laugh_points >= 2 + 2  # 基础2 + 行迹1(1名欢愉+2)
        assert state.extra.get('sparxie_burst_points', 0) == pytest.approx(1.0)
        assert u.total_damage_dealt > 0

    def test_eff_stats_trace2_cd(self):
        """行迹2: 每笑点全队暴伤+8%（上限80%=10笑点）"""
        u = _unit('sparxie')
        state = SimState(enemies=[_enemy()], units=[u])
        _elation_state(state)
        state.laugh_points = 10
        s = _build_effective_stats(u, state)
        assert s.CRIT_DMG == pytest.approx(u.base_stats.CRIT_DMG + 0.80)

    def test_e2_extra_turn_on_aha(self):
        """E2: 阿哈时刻结束→+1额外回合入队+2爆点（E1 +5笑点）"""
        u = _unit('sparxie', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[u])
        elation = _elation_state(state)
        state.laugh_points = 1
        state.aha_speed = 100.0
        state.aha_next_av = 0.0
        elation.execute_aha(state)
        kinds = [k for _, k in state.extra.get('extra_turns', [])]
        assert 'sparxie_e2' in kinds
        assert state.extra['sparxie_burst_points'] >= 2.0


class TestTheDahlia:
    """大丽花: 共舞者绑定 / 结界 / 未破韧超击破 / FUA每回合1次 / 败谢 / 行迹3"""

    def test_talent_open_bind(self):
        """天赋: 回35能量+绑定击破特攻最高队友"""
        u = _unit('the_dahlia')
        ally = _unit('firefly', position=2)  # 火萤击破特攻高
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _dahlia_talent_open(state)
        assert u.current_energy == pytest.approx(35.0)
        assert state.extra['dahlia_dancers'] == ['the_dahlia', 'firefly']

    def test_field_apply(self):
        """战技结界: 3回合+全队弱点击破效率+50%"""
        u = _unit('the_dahlia')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _dahlia_field_apply(state, u)
        assert state.extra['dahlia_field_turns'] == 3
        s = _build_effective_stats(u, state)
        assert s.TOUGHNESS_EFFICIENCY == pytest.approx(1.5)

    def test_unbroken_super_break_in_field(self):
        """结界期间未破韧目标削韧→60%超击破（回归探针: 无结界不触发）"""
        u = _unit('the_dahlia')
        e = _enemy(toughness=200, broken=False)
        state = SimState(enemies=[e], units=[u])
        state.extra['dahlia_dancers'] = ['the_dahlia']
        # 无结界: 未破韧目标不产生超击破
        _apply_toughness_damage(state, u, e, 10.0, '火', 'basic_attack',
                                _build_effective_stats(u, state))
        assert not any('超击破' in l for l in state.log)
        # 开结界后: 未破韧也转化
        _dahlia_field_apply(state, u)
        e2 = _enemy(toughness=200, broken=False)
        state.enemies = [e2]
        _apply_toughness_damage(state, u, e2, 10.0, '火', 'basic_attack',
                                _build_effective_stats(u, state))
        assert any('超击破' in l for l in state.log)

    def test_dancer_super_break_rate(self):
        """共舞者攻击破韧目标→天赋60%转化率"""
        u = _unit('seele')
        t = _enemy(broken=True)
        state = SimState(enemies=[t], units=[u])
        state.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        state.units.append(_unit('the_dahlia', position=1))
        rate = _dahlia_super_break_rate(state, u, t)
        assert rate == pytest.approx(0.6)

    def test_fua_once_per_turn(self):
        """另一共舞者攻击→大丽花FUA; 同回合第二次不触发"""
        d = _unit('the_dahlia', position=1)
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[d, ally])
        state.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        _dahlia_on_ally_attack(state, ally)
        assert d.extra.get('dahlia_fua_used') is True
        assert d.total_damage_dealt > 0
        dealt0 = d.total_damage_dealt
        _dahlia_on_ally_attack(state, ally)  # 同回合不重复
        assert d.total_damage_dealt == dealt0

    def test_baisie_weakness(self):
        """败谢: 防御-18% + 共舞者属性双弱点（快照恢复）"""
        u = _unit('the_dahlia')
        ally = _unit('seele', position=2)  # 量子
        e = _enemy(res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                        '虚数': 0, '物理': 0.2, '火': 0.2})
        state = SimState(enemies=[e], units=[u, ally])
        state.extra['dahlia_dancers'] = ['the_dahlia', 'seele']
        _apply_dahlia_baisie(u, state, e)
        assert e.has_status(status_id='the_dahlia_baisie')
        assert e.element_res['火'] <= -0.2
        assert e.element_res['量子'] <= -0.2
        assert e.element_res['物理'] == pytest.approx(0.2)  # 非共舞者属性不动

    def test_trace3_fire_implant(self):
        """行迹3: 火属性添加弱点→+20固定削韧+回10%能量上限; 速度+30% 2回合"""
        u = _unit('the_dahlia')
        e = _enemy(toughness=200)
        state = SimState(enemies=[e], units=[u])
        e0 = u.current_energy
        _trace_dahlia_trace3_implant(u, state, element='火', target=e)
        assert e.toughness == pytest.approx(180.0)
        assert u.current_energy - e0 == pytest.approx(u.char.max_energy * 0.10)
        assert any(getattr(b, 'attributes', {}).get('SPD_PERCENT') == 30.0 for b in u.buffs)
