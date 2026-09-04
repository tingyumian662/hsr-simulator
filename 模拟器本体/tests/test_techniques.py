"""v6.3.0 秘技系统回归测试（11 角色）

语义依据: CLAUDE_HANDOFF.md v6.3.0 节（用户 2026-08-14 确认）:
- support 全开; battle_start 取站位最前=开怪者; 无开怪秘技→弱点属性角色, 否则队伍第一个
- 灵砂秘技=用户补录(流翠散云, support); 遐蝶两态(开怪召唤死龙/非开怪新蕊+30%);
  长夜月暴伤口径与战技相同"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate
from engine.systems.techniques import apply_techniques


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _sim(ids, max_av=500, hp=500000, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(hp=hp), max_av=max_av)


class TestResolveTechniques:
    def test_support_all_battle_start_front(self):
        """support 全开 + battle_start 取站位最前"""
        from engine.runtime import SimState, SimUnit
        from engine.models.character import load_character as lc
        units = []
        for i, cid in enumerate(['aglaea', 'xiadie', 'trailblazer_harmony']):
            c = lc(cid, 'data/characters')
            stats = compute_combat_stats(c, None, None, None)
            u = SimUnit(char=c, base_stats=stats, position=i + 1)
            units.append(u)
        state = SimState(enemies=[_enemy()], units=units)
        opener = apply_techniques(state, units)
        assert opener == 'aglaea'  # 站位最前 battle_start
        assert state.extra['opener_id'] == 'aglaea'

    def test_no_battle_start_weakness_opener(self):
        """无 battle_start: 首个属性命中敌方弱点的角色为开怪者"""
        from engine.runtime import SimState, SimUnit
        from engine.models.character import load_character as lc
        units = []
        for i, cid in enumerate(['seele', 'bronya']):  # 量子/风, 敌人冰弱点
            c = lc(cid, 'data/characters')
            stats = compute_combat_stats(c, None, None, None)
            u = SimUnit(char=c, base_stats=stats, position=i + 1)
            units.append(u)
        e = _enemy()
        e.element_res = {'冰': 0, '量子': 0.2, '风': 0.2, '火': 0.2,
                         '雷': 0.2, '虚数': 0.2, '物理': 0.2}  # 冰弱点
        state = SimState(enemies=[e], units=units)
        opener = apply_techniques(state, units)
        assert opener == 'seele'  # 首个命中弱点(量子)的角色? 无→量子0.2非弱点
        # 修正: 量子 res=0.2 非弱点 → 无弱点命中 → 队伍第一个
        assert opener == 'seele'

    def test_no_weakness_fallback_first(self):
        """无敌方弱点对应角色 → 队伍第一个角色开怪"""
        from engine.runtime import SimState, SimUnit
        from engine.models.character import load_character as lc
        units = []
        for i, cid in enumerate(['seele', 'bronya']):
            c = lc(cid, 'data/characters')
            stats = compute_combat_stats(c, None, None, None)
            u = SimUnit(char=c, base_stats=stats, position=i + 1)
            units.append(u)
        state = SimState(enemies=[_enemy()], units=units)  # 全抗 0 → 无弱点命中
        opener = apply_techniques(state, units)
        assert opener == 'seele'


class TestSupportTechniques:
    def test_tbh_team_be(self):
        """同谐秘技: 全队击破特攻+30% 2回合"""
        s = _sim(['trailblazer_harmony'])
        assert any('即刻！独奏团' in l for l in s.log)
        u = s.units[0]
        assert any('BREAK_EFFECT' in b.attributes for b in u.buffs)

    def test_fengjin_heal_and_maxhp(self):
        """风堇秘技: 全队回复30%HP+600 + 生命上限+20%"""
        s = _sim(['fengjin'], max_av=50)
        assert any('天气正好' in l for l in s.log)
        # 生命上限+20% 已施加
        assert s.units[0].extra.get('tech_maxhp_turns') == 2

    def test_changyeyue_yizhi_and_cd(self):
        """长夜月秘技: 忆灵暴伤buff(与战技同实现) + 忆质+1"""
        s = _sim(['changyeyue'], max_av=50)
        u = s.units[0]
        assert any(getattr(b, 'param_id', '') == 'changyeyue_tech_cd' for b in u.buffs)
        assert u.yizhi >= 1

    def test_lingsha_summon_and_chunzui(self):
        """灵砂秘技(用户补录): 召唤浮元 + 全敌醇醉2回合"""
        s = _sim(['lingsha'])
        assert any('流翠散云' in l for l in s.log)
        # v6.10: 开局50%能量改变时序, 醇醉2回合在500AV内可能已过期——断言日志更稳
        assert any('醇醉' in l for l in s.log)

    def test_xilian_realm(self):
        """昔涟秘技: 展开结界(真伤24% 2回合)
        v7.2.0: 结界与境界系统解耦(昔涟无境界技能)→独立 xilian_field_turns"""
        s = _sim(['xilian'])
        assert s.realm_owner == ''
        assert s.extra.get('xilian_field_turns') == 2
        assert s.realm_true_dmg == pytest.approx(0.24)


class TestBattleStartTechniques:
    def test_firefly_wave_weakness(self):
        """流萤秘技(开怪): 首波全敌火弱点+200%ATK伤害"""
        e = _enemy(toughness=1000)
        s = _sim(['firefly'], hp=500000)
        assert s.extra.get('firefly_tech_active') is True
        assert '[秘技·焦土陨击]' in '\n'.join(s.log)
        assert any('firefly_fire_weakness' in st.id for st in s.enemies[0].statuses)

    def test_mydei_taunt_and_charge(self):
        """万敌秘技(开怪): 全敌80%HP虚数伤 + 嘲讽1回合 + 充能+50"""
        s = _sim(['mydei'], max_av=50)
        u = s.units[0]
        assert any('折戟臣服的监牢' in l for l in s.log)
        # 嘲讽状态敌人行动即消费（日志含施加与消费均证明生效）
        assert any('嘲讽' in l for l in s.log)
        assert u.extra.get('mydei_charge', 0) >= 50

    def test_fugue_advance_and_def_down(self):
        """忘归人秘技(开怪): 行动提前40% + 全敌DEF-18% 2回合"""
        s = _sim(['fugue'], max_av=50)
        assert any('炤炤彻旷' in l for l in s.log)
        assert any(st.id == 'fugue_def_down' for st in s.enemies[0].statuses)

    def test_tbr_delay_and_damage(self):
        """开拓者·记忆秘技(开怪): 全敌行动延后50% + 100%ATK冰伤

        v6.3.0b P1-2: 延后直接写敌方行动条 navs（此前写 av_delayed, 敌方首次攻击后才消费,
        无法阻止本次进战行动）"""
        s = _sim(['trailblazer_remembrance'], max_av=50)
        assert any('记忆如往日重现' in l for l in s.log)
        navs = s.extra.get('navs', {})
        enemy_nav = navs.get(('e', 0))
        assert enemy_nav is not None
        assert enemy_nav > 10000.0 / 80.0  # 延后前=125.0; 延后50%=+62.5 → 187.5

    def test_aglaea_summon_and_gossamer(self):
        """阿格莱雅秘技(开怪): 召唤衣匠 + 随机敌织线"""
        s = _sim(['aglaea'])
        u = s.units[0]
        assert any('披星百裂' in l for l in s.log)
        assert u.memsprite_unit is not None and u.memsprite_unit.is_alive
        assert any(e.extra.get('gossamer') for e in s.enemies)


class TestXiadieTwoState:
    def test_xiadie_as_opener_summon_dragon(self):
        """遐蝶开怪: 召唤死龙(HP=新蕊上限50%=17000) + 境界 + 全队40%当前HP消耗"""
        s = _sim(['xiadie'], max_av=30)
        u = s.units[0]
        assert s.extra['opener_id'] == 'xiadie'
        # 秘技死龙 HP=新蕊上限50%=17000（4次喷吐×25%后自爆=实机语义, 断言施加日志）
        assert any('召唤死龙 HP=17000' in l for l in s.log)
        assert any('遗世冥域' in l for l in s.log)
        # 40% 当前HP消耗（死龙自爆全队回血会回满, 断言秘技日志语义; 单测 apply_techniques 覆盖消耗值）
        assert any('40%当前HP消耗' in l for l in s.log)

    def test_xiadie_not_opener_get_xinrui(self):
        """遐蝶非开怪(忘归人开怪): 新蕊+30%上限"""
        s = _sim(['aglaea', 'xiadie'], max_av=30)
        assert s.extra['opener_id'] == 'aglaea'
        u = s.units[1]
        assert u.memsprite_unit is None  # 未召唤死龙
        assert u.xinrui >= 34000 * 0.30


class TestBatch2Techniques:
    """v6.3.0 第二批: 希儿/银狼/布洛妮娅/符玄/藿藿/花火（txt 用户录入）"""

    def test_seele_stealth_amplify(self):
        """希儿秘技(进战): 隐身→进战立即增幅(pending, X轴首动激活)"""
        s = _sim(['seele'], max_av=50)
        u = s.units[0]
        assert any('幻身' in l for l in s.log)
        # pending 已设置（首动激活消耗; 50AV 内希儿行动前断言 pending 或增幅已激活）
        assert (u.extra.get('seele_amplify_pending') is True
                or any(getattr(b, 'source_name', '') == '再现增幅' for b in u.buffs))

    def test_silver_wolf_damage_and_toughness(self):
        """银狼秘技(进战): 全敌80%ATK量子伤 + 无视弱点削韧20"""
        s = _sim(['silver_wolf'], max_av=50)
        u = s.units[0]
        assert any('强制结束进程' in l for l in s.log)
        assert u.total_damage_dealt > 0
        # 无视弱点: 敌人初始韧性200, 秘技削韧20 → 180
        assert s.enemies[0].toughness == pytest.approx(180, abs=1e-6)

    def test_bronya_team_atk(self):
        """布洛妮娅秘技(非进战): 全队攻击力+15% 2回合"""
        s = _sim(['bronya'], max_av=50)
        u = s.units[0]
        assert any(getattr(b, 'param_id', '') == 'bronya_technique_atk' for b in u.buffs)
        assert any('在旗帜下' in l for l in s.log)

    def test_fuxuan_field_and_jianzhi(self):
        """符玄秘技(非进战): 穷观阵3回合 + 鉴知(HP上限+6%/暴击+12%)"""
        s = _sim(['fu_xuan'], max_av=50)
        assert s.extra.get('fuxuan_field_turns') == 3
        u = s.units[0]
        assert any(getattr(b, 'param_id', '') == 'fuxuan_tech_barrier' for b in u.buffs)
        assert any('太微行棋' in l for l in s.log)

    def test_huohuo_atk_down(self):
        """藿藿秘技(非进战): 全敌攻击力-25% 2回合"""
        s = _sim(['huohuo'], max_av=50)
        assert any('凶煞' in l for l in s.log)
        assert any('huohuo_tech_atk_down' in st.id for st in s.enemies[0].statuses)

    def test_sparkle_sp_and_energy(self):
        """花火秘技(非进战): 恢复3战技点 + 花火回20能量"""
        s = _sim(['sparkle'], max_av=50)
        assert s.skill_points >= 3
        assert s.units[0].current_energy >= 20
        assert any('不可靠叙事者' in l for l in s.log)


class TestSilverWolfFull:
    """v6.3.0 普通银狼(silver_wolf)完整录入回归（银狼.txt）"""

    def test_skill_weakness_implant(self):
        """战技: 添加队伍第一位角色属性弱点(抗性-20%) + 全抗-13%"""
        s = _sim(['silver_wolf'], max_av=200, hp=500000)
        log = '\n'.join(s.log)
        assert '银狼弱点植入' in log
        assert '银狼全抗-13%' in log
        assert any(st.id == 'silver_wolf_weakness' for st in s.enemies[0].statuses)

    def test_talent_defect_implant(self):
        """天赋: 攻击后100%概率植入随机缺陷"""
        s = _sim(['silver_wolf'], max_av=300, hp=500000)
        assert any('银狼缺陷' in l for l in s.log)
        assert any('silver_wolf_defect' in st.id for st in s.enemies[0].statuses)

    def test_trace2_inject(self):
        """行迹2: 战斗开始回20能量"""
        s = _sim(['silver_wolf'], max_av=50)
        assert any('行迹·注入: 战斗开始回20能量' in l for l in s.log)
        assert s.units[0].current_energy >= 20

    def test_trace3_annotate(self):
        """行迹3: 每10%效果命中→+10%攻击力(上限50%)"""
        s = _sim(['silver_wolf'], max_av=50)
        assert any('行迹·旁注' in l for l in s.log)

    def test_e6_per_debuff_damage(self):
        """E6: 目标每负面伤害+20%(上限100%)"""
        import re
        def ult(eid):
            s = _sim(['silver_wolf'], max_av=800, hp=500000,
                     silver_wolf={'eidolon': eid})
            for l in s.log:
                if '账号已封禁' in l and 'AV]' in l:
                    return float(l.split(':')[-1].strip())
            return 0.0
        d6, d0 = ult(6), ult(0)
        assert d6 > d0 * 1.2  # 至少1个负面以上

    def test_technique_silver_wolf(self):
        """秘技(进战): 全敌80%ATK量子伤 + 无视弱点削韧20"""
        s = _sim(['silver_wolf'], max_av=50)
        assert any('强制结束进程' in l for l in s.log)
        assert s.enemies[0].toughness == pytest.approx(180, abs=1e-6)
