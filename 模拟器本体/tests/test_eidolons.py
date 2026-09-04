"""星魂效果测试（17 个：希儿/昔涟/遐蝶/长夜月/风堇）"""
import copy
import re
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import simulate


def _enemy(hp=500000, toughness=20, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                     '虚数': 0, '物理': 0, '火': 0})


def _sim(ids, max_av=800, hp=500000, num_enemies=1, res=None, toughness=20, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(hp=hp, res=res, toughness=toughness),
                    max_av=max_av, num_enemies=num_enemies)


def _seele_ctx(eidolon, crit_rate=0.05):
    """直接构造希儿战斗上下文（E1/E2 确定性单元测试用）"""
    from engine.core.attributes import compute_combat_stats
    from engine.runtime import SimState, SimUnit
    c = load_character('seele', 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    stats.CRIT_RATE = crit_rate
    u = SimUnit(char=c, base_stats=stats, position=1)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    e = _enemy()
    state = SimState(enemies=[e], units=[u])
    state.extra['enemy_blueprint'] = copy.copy(e)  # E1 判定需初始满血蓝图
    return u, state


# ---- 希儿 ----

class TestSeele:
    def test_e1_low_hp_bonus(self):
        """行迹·斩尽(与E1同效果, 无条件): 对HP≤80%目标暴击+15%跨阈值+无视20%防御"""
        from engine.core.combat_engine import _use_skill

        def dmg(hp_pct):
            # rank0 下斩尽恒生效; 蓝图=满血500000保证80%阈值判定
            u, state = _seele_ctx(0, crit_rate=0.40)
            state.enemies[0].HP = 500000 * hp_pct
            state.enemies[0].toughness = 200  # 高韧性防一次击破稀释对比
            state.enemies[0].max_toughness = 200
            _use_skill(u, state, "skill")
            return u.total_damage_dealt

        d_low, d_high = dmg(0.60), dmg(0.90)
        # v5.2 期望模式: 斩尽 CR+15% 进期望公式 + DEF_PEN20%
        # 理论比值 = (1+0.55×1.74)/(1+0.40×1.74) × 防御(0.556/0.5) ≈ 1.28
        assert d_low > d_high * 1.10  # 实测比值≈1.19

    def test_e2_speed_stack_cap(self):
        """E2蝶舞: 战技加速叠层上限2(E2)/1(rank0刷新)"""
        from engine.core.combat_engine import _use_skill

        def stacks(eidolon):
            u, state = _seele_ctx(eidolon)
            _use_skill(u, state, "skill")
            _use_skill(u, state, "skill")
            return [b for b in u.buffs if getattr(b, 'param_id', '') == 'seele_speed_buff']

        assert len(stacks(2)) == 2   # E2: 2层
        assert len(stacks(0)) == 1   # rank0: 刷新不叠加(修复无限叠层)

    def test_e6_luandie(self):
        """E6离析: 终结技→乱蝶(真伤30%×3), 受击触发"""
        s = _sim(['seele'], max_av=800,
                 seele={'eidolon': 6, 'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '乱蝶: 目标陷入乱蝶' in log
        assert '乱蝶真伤' in log


# ---- 昔涟 ----

class TestXilian:
    def test_e1_poem_boost(self):
        """E1: 真我之诗触发→+6追忆, 弹射次数+12"""
        s = _sim(['xilian'], max_av=3000, xilian={'eidolon': 1})
        log = '\n'.join(s.log)
        assert '昔涟E1: 真我之诗+6追忆' in log
        assert log.count('献予真我之诗弹射') >= 12  # 每来源1次+额外12

    def test_e2_start_zhuiyi(self):
        """E2: 进战+12追忆; 结界真伤倍率计算生效
        （v5.6.1 后追忆会被终结技/忆灵技消耗, 故在早期检查开局+12）"""
        s = _sim(['xilian'], max_av=200, xilian={'eidolon': 2})
        log = '\n'.join(s.log)
        assert s.units[0].zhuiyi >= 12
        assert '昔涟E2: 结界真伤=' in log

    def test_e4_stack_growth(self):
        """E4: 花与箭叠层递增, 弹射倍率+6%/层"""
        s = _sim(['xilian'], max_av=3000, xilian={'eidolon': 4})
        log = '\n'.join(s.log)
        assert '昔涟E4: 花与箭叠层+1' in log
        assert re.search(r'献予真我之诗弹射: .*?\((?:6[6-9]|[7-9]\d)', log)

    def test_e6_pull_and_def(self):
        """E6: 首次终结技全队拉条100%; 献予触发→敌DEF-20%"""
        s = _sim(['xilian'], max_av=3000, xilian={'eidolon': 6})
        log = '\n'.join(s.log)
        assert '昔涟E6: 首次终结技→全队拉条100%' in log
        assert '昔涟E6: 献予触发→敌方DEF-20%' in log


# ---- 遐蝶 ----

class TestXiadie:
    # 死龙测试需多角色攒新蕊(HP损失1:1), 同 test_action_bar 配置
    def test_e1_dragon_dmg(self):
        """E1: 敌HP≤80%→死龙伤害×1.2
        v6.3.0: 阿格莱雅(battle_start)站最前为开怪者, 遐蝶非开怪→死龙仍由终结技召唤(测试原意)"""
        def flame(eidolon):
            # 低血敌人保证首次焰息时已≤80%; 三人队攒新蕊召唤死龙
            s = _sim(['aglaea', 'xiadie', 'fengjin', 'xilian'], max_av=3000, hp=60000,
                     xiadie={'eidolon': eidolon, 'initial_energy_pct': 100})
            m = re.search(r'焰息: ([\d.]+)', '\n'.join(s.log))
            return float(m.group(1)) if m else 0.0

        f0, f1 = flame(0), flame(1)
        assert f1 > f0 * 1.15  # 首次焰息时敌人已≤80%血

    def test_e2_chiyi_chain(self):
        """E2: 召唤→+2炽意+行动提前; 炽意抵扣焰息HP消耗。

        强化战技+30%新蕊在「常规回合终结技hold→复活回自己回合」(规则3)流程触发;
        本场景昔涟激活把遐蝶终结技入队→死龙按铁律8「0AV后到先动」先行动并自爆
        →强化战技不再可达（v6.2.1b 达成戳修正后的正确语义）。
        该段单测见 tests/test_v621b.py::TestXiadieE2EnhancedSkill。
        """
        s = _sim(['xiadie', 'fengjin', 'xilian'], max_av=3000,
                 xiadie={'eidolon': 2, 'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '遐蝶E2: +2炽意, 行动提前100%' in log
        assert '炽意抵扣' in log

    def test_e4_heal_bonus(self):
        """E4: 遐蝶在场全队受疗+20%"""
        s = _sim(['xiadie', 'fengjin'], max_av=1500,
                 xiadie={'eidolon': 4}, fengjin={'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '遐蝶E4: 全队受疗+20%' in log

    def test_e6_quantum_pen(self):
        """E6: 量子抗穿+20%; 无视弱点削韧; 晦翼9次弹射"""
        s = _sim(['xiadie', 'fengjin', 'xilian'], max_av=3000, res={'量子': 0.2},
                 xiadie={'eidolon': 6, 'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '遐蝶E6: 量子抗性穿透+20%' in log
        assert '击破弱点' in log  # 非弱点(抗0.2)仍被削韧
        assert '(9次弹射)' in log


# ---- 长夜月 ----

class TestChangyeyue:
    def test_e1_memsprite_dmg(self):
        """E1: 1敌人时忆灵伤害×1.5"""
        s0 = _sim(['changyeyue'], max_av=1500)
        s1 = _sim(['changyeyue'], max_av=1500, changyeyue={'eidolon': 1})
        assert s1.units[0].total_damage_dealt > s0.units[0].total_damage_dealt * 1.1

    def test_e4_toughness(self):
        """E4: 忆灵削韧效率+25%(长夜自身再+25%)"""
        s0 = _sim(['changyeyue'], max_av=1000, toughness=500)
        s1 = _sim(['changyeyue'], max_av=1000, toughness=500, changyeyue={'eidolon': 4})
        t0 = s0.enemies[0].toughness
        t1 = s1.enemies[0].toughness
        assert t0 > 0 and t1 < t0  # E4 削韧更快→剩余韧性更少(且未削空)


# ---- 风堇 ----

class TestFengjin:
    def test_e1_ult_heal(self):
        """E1: 雨过天晴HP上限+50%; 攻击后回8%HP"""
        s = _sim(['fengjin'], max_av=1500,
                 fengjin={'eidolon': 1, 'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '雨过天晴' in log
        assert '风堇E1: 攻击后回8%HP' in log

    def test_e2_hp_loss_spd(self):
        """E2: HP降低→SPD+30% 2回合（遐蝶战技全队扣血触发）"""
        s = _sim(['xiadie', 'fengjin'], max_av=800, fengjin={'eidolon': 2})
        log = '\n'.join(s.log)
        assert '风堇E2: HP降低→SPD+30% 2回合' in log

    def test_e4_overspeed_cd(self):
        """E4: SPD>200每超1点→暴伤+2%"""
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        from engine.characters.fengjin import _eid_fengjin_e4
        c = load_character('fengjin', 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        stats.SPD = 230
        u = SimUnit(char=c, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        u.eidolon_rank = 4
        state = SimState(enemies=[_enemy()], units=[u])
        _eid_fengjin_e4(u, state)
        assert u.base_stats.CRIT_DMG == pytest.approx(0.50 + 0.60, abs=1e-9)

    def test_e6_cloud_and_respen(self):
        """E6: 乌云乌云清空→全队回12%; 小伊卡在场全队RES_PEN+20%"""
        s = _sim(['fengjin'], max_av=3000,
                 fengjin={'eidolon': 6, 'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '乌云乌云: 全队回复12%并清空' in log
        assert s.units[0].base_stats.RES_PEN_ALL >= 0.20


# ---- 风堇行迹 ----

class TestFengjinTraces:
    def _fengjin_ctx(self, spd=125):
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        c = load_character('fengjin', 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        stats.SPD = spd
        u = SimUnit(char=c, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        e = _enemy()
        return u, SimState(enemies=[e], units=[u])

    def _ally(self, cid, hp_pct=1.0):
        from engine.runtime import SimUnit
        from engine.core.attributes import compute_combat_stats
        c = load_character(cid, 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        u = SimUnit(char=c, base_stats=stats, position=2)
        u.max_hp = stats.HP
        u.current_hp = stats.HP * hp_pct
        return u

    def test_trace1_spd_heal(self):
        """行迹1·暴风停歇: SPD>200→HP×1.2; 每超1点SPD治疗量+1%"""
        from engine.characters.fengjin import _trace_fengjin_t1
        from engine.core.combat_engine import _use_skill
        # SPD=125 不触发
        u0, s0 = self._fengjin_ctx(spd=125)
        _trace_fengjin_t1(u0, s0)
        assert u0.base_stats.HP == pytest.approx(s0.units[0].base_stats.HP)
        # SPD=230 触发: HP×1.2, 治疗量×1.3(超速30点)
        u1, s1 = self._fengjin_ctx(spd=230)
        base_hp = u1.base_stats.HP
        _trace_fengjin_t1(u1, s1)
        assert u1.base_stats.HP == pytest.approx(base_hp * 1.2)
        hp0 = u0.base_stats.HP
        u0, s0 = self._fengjin_ctx(spd=125)
        _use_skill(u0, s0, 'skill')
        _use_skill(u1, s1, 'skill')
        h0 = float(re.search(r'治疗: ([\d.]+)×', '\n'.join(s0.log)).group(1))
        h1 = float(re.search(r'治疗: ([\d.]+)×', '\n'.join(s1.log)).group(1))
        # 治疗=HP×8%+160: HP×1.2 只影响比例部分, 固定160不受影响; 再乘超速1.3
        expect = ((hp0 * 1.2 * 0.08 + 160) * 1.3) / (hp0 * 0.08 + 160)
        assert h1 == pytest.approx(h0 * expect, rel=1e-2)  # 日志取整, 1% 容差

    def test_trace2_cr_and_low_hp(self):
        """行迹2·阴云莞尔: CR+100%; 对HP≤50%目标治疗+25%"""
        from engine.characters.fengjin import _trace_fengjin_t2
        from engine.core.combat_engine import _use_skill
        u, s = self._fengjin_ctx()
        _trace_fengjin_t2(u, s)
        assert u.base_stats.CRIT_RATE >= 1.00
        # 治疗目标: 甲60%血(×1.0, 不封顶不触发), 乙30%血(×1.25)
        ally_a = self._ally('seele', hp_pct=0.60)
        ally_b = self._ally('seele', hp_pct=0.30)
        s.units.extend([ally_a, ally_b])
        heal_a0 = ally_a.current_hp
        heal_b0 = ally_b.current_hp
        _use_skill(u, s, 'skill')
        assert (ally_b.current_hp - heal_b0) == pytest.approx(
            (ally_a.current_hp - heal_a0) * 1.25, rel=1e-6)

    def test_trace3_res_and_cleanse(self):
        """行迹3·雷雨轻柔: EFFECT_RES+50%; 战技→净化全队1个负面"""
        from engine.characters.fengjin import _trace_fengjin_t3
        from engine.core.combat_engine import _use_skill
        from engine.runtime import TimedBuff
        u, s = self._fengjin_ctx()
        _trace_fengjin_t3(u, s)
        assert u.base_stats.EFFECT_RES >= 0.50
        # 挂一个负属性 buff, 战技后应被净化清除
        u.buffs.append(TimedBuff(source_id='x', attributes={'SPD_PERCENT': -10.0},
                                 remaining_turns=3, source_name='负面测试'))
        _use_skill(u, s, 'skill')
        assert not any(getattr(b, 'source_name', '') == '负面测试' for b in u.buffs)
        log = '\n'.join(s.log)
        assert '行迹3·雷雨轻柔: 净化全队1个负面效果' in log
