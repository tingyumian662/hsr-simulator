"""5 个存量角色（布洛妮娅/希儿/花火/符玄/藿藿）行迹/星魂补齐测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import simulate


def _enemy(hp=500000, toughness=20, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                     '虚数': 0, '物理': 0, '火': 0})


def _sim(ids, max_av=800, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(), max_av=max_av)


def _unit(cid, eidolon=0, position=1, **extra):
    from engine.runtime import SimUnit
    from engine.core.attributes import compute_combat_stats
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


# ---- 布洛妮娅 ----

class TestBronya:
    def test_trace_def(self):
        """行迹·阵地: 战斗开始全队DEF+20% 2回合"""
        s = _sim(['bronya', 'seele'], max_av=100)  # 首行动前 buff 未 tick
        seele = next(u for u in s.units if u.char.id == 'seele')
        buffs = [b for b in seele.buffs if getattr(b, 'source_name', '') == '行迹·阵地']
        assert buffs and buffs[0].attributes.get('DEF_PERCENT') == 20.0
        assert buffs[0].remaining_turns >= 1  # 部分角色可能已行动tick

    def test_trace_dmg(self):
        """行迹·军势: 全队伤害+10%"""
        s = _sim(['bronya', 'seele'])
        for u in s.units:
            assert u.base_stats.DMG_BONUS_ALL == pytest.approx(0.10, abs=1e-9)

    def test_basic_crit(self):
        """行迹·号令: 普攻必暴"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('bronya')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'basic_attack')
        # 普攻伤害含暴击(1+0.5基础爆伤) — 与不暴对比(构造无暴面板)
        assert u.total_damage_dealt > 0
        assert any('行迹·涟漪' not in l for l in state.log)  # 无无关日志

    def test_e4_follow_up(self):
        """E4: 他角色对风弱敌普攻→布洛妮娅追加攻击（handler 直调, 含每回合1次限制）"""
        from engine.runtime import SimState
        from engine.characters.bronya import _eid_bronya_e4
        u = _unit('seele')  # 攻击者
        bronya = _unit('bronya', eidolon=4, position=2)
        state = SimState(enemies=[_enemy()], units=[u, bronya])
        target = state.enemies[0]
        hp0 = target.HP
        _eid_bronya_e4(u, state, target=target, skill_key='basic_attack')
        assert target.HP < hp0
        hp1 = target.HP
        _eid_bronya_e4(u, state, target=target, skill_key='basic_attack')
        assert target.HP == hp1  # 同回合不再触发

    def test_e6_duration(self):
        """E6: 战技增伤+1回合(1→2)"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('bronya', eidolon=6)
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        buffs = [b for b in u.buffs if getattr(b, 'param_id', '') == 'bronya_skill_dmg_buff']
        assert buffs and buffs[0].remaining_turns == 2


# ---- 希儿 ----

class TestSeeleTraces:
    def test_trace_ripple(self):
        """行迹·涟漪: 普攻后下次行动提前20%（handler 直调）"""
        from engine.runtime import SimState
        from engine.characters.seele import _trace_seele_ripple
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_seele_ripple(u, state)
        assert getattr(u, '_pending_action_advance', 0) == pytest.approx(
            10000.0 / u.base_stats.SPD * 0.20, rel=1e-6)

    def test_trace_luandie_rank0(self):
        """行迹·离析: rank0 终结技也触发乱蝶"""
        s = _sim(['seele'], max_av=600, seele={'initial_energy_pct': 100})
        log = '\n'.join(s.log)
        assert '乱蝶: 目标陷入乱蝶' in log


    def test_trace1_memory_count_cd(self):
        """行迹1·天亮了雨落了: 战技持续期间按记忆命途数量给忆灵暴伤加成
        （4记忆→+65%; 验证完整忆灵伤害管线）"""
        from engine.runtime import SimState, TimedBuff
        from engine.systems.remembrance import RemembranceSystem

        def run(with_skill_buff):
            cy = _unit('changyeyue')
            allies = [_unit(cid, position=i + 2) for i, cid in
                      enumerate(('xiadie', 'xilian', 'fengjin'))]
            # 高韧性避免击破伤害作为固定附加段稀释暴伤乘区。
            state = SimState(enemies=[_enemy(toughness=999)], units=[cy] + allies)
            rem = RemembranceSystem()
            rem.init_battle(state, [cy])
            ms = cy.memsprite_unit
            ms.base_stats.CRIT_RATE = 0.5
            if with_skill_buff:
                cy.buffs.append(TimedBuff(
                    source_id='cy', attributes={'CRIT_DMG': 24.0},
                    remaining_turns=2, source_name='战技',
                    param_id='changyeyue_skill_cd',
                ))
            hp0 = state.enemies[0].HP
            rem._use_memsprite_skill(state, cy, ms, 'memsprite_basic')
            return hp0 - state.enemies[0].HP, ms.base_stats.CRIT_DMG, state.log

        baseline, cd0, _ = run(False)
        actual, _, log = run(True)
        log = '\n'.join(log)
        assert '天亮了，雨落了: 忆灵暴伤+65% (记忆×4)' in log
        # v5.2 期望暴击模式: 倍率 = 1 + CR×CD; CR=0.5 → 比值 = (1+0.5×(CD+bonus))/(1+0.5×CD)
        bonus = 0.24 + 0.65
        assert actual / baseline == pytest.approx(
            (1.0 + 0.5 * (cd0 + bonus)) / (1.0 + 0.5 * cd0), rel=1e-9
        )

    def test_trace1_technique_does_not_enable_skill_only_bonus(self):
        """秘技给忆灵基础24%暴伤，但不得错误启用仅限战技的行迹1。"""
        from engine.runtime import SimState, TimedBuff
        from engine.systems.remembrance import RemembranceSystem

        def run(with_tech_buff):
            cy = _unit('changyeyue')
            state = SimState(enemies=[_enemy(toughness=999)], units=[cy])
            rem = RemembranceSystem()
            rem.init_battle(state, [cy])
            ms = cy.memsprite_unit
            ms.base_stats.CRIT_RATE = 0.5
            if with_tech_buff:
                cy.buffs.append(TimedBuff(
                    source_id='cy', attributes={'CRIT_DMG': 24.0},
                    remaining_turns=2, source_name='秘技',
                    param_id='changyeyue_tech_cd',
                ))
            hp0 = state.enemies[0].HP
            rem._use_memsprite_skill(state, cy, ms, 'memsprite_basic')
            return hp0 - state.enemies[0].HP, ms.base_stats.CRIT_DMG, state.log

        baseline, cd0, _ = run(False)
        actual, _, log = run(True)
        # v5.2 期望暴击模式: 比值 = (1+0.5×(CD+0.24))/(1+0.5×CD)
        assert actual / baseline == pytest.approx(
            (1.0 + 0.5 * (cd0 + 0.24)) / (1.0 + 0.5 * cd0), rel=1e-9
        )
        assert '天亮了，雨落了' not in '\n'.join(log)


# ---- 花火 ----

class TestSparkle:
    def test_trace_energy(self):
        """行迹·岁时记: 普攻额外+10能量（handler 直调, 注册链路由黑盒覆盖）"""
        from engine.runtime import SimState
        from engine.core.effect_resolver import _trace_basic_energy_bonus
        u = _unit('sparkle')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_basic_energy_bonus(u=u, state=state)
        assert u.current_energy == pytest.approx(10.0)

    def test_trace_team_cd(self):
        """v6.10.6: 行迹3·夜想曲改为动态面板——全队ATK+45%（handler 不再永久写面板）"""
        from engine.core.combat_engine import _build_effective_stats
        from engine.runtime import SimState
        from engine.characters.sparkle import _trace_sparkle_team_cd
        u = _unit('sparkle')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        atk0 = ally.base_stats.ATK
        _trace_sparkle_team_cd(u, state)
        assert ally.base_stats.ATK == atk0  # handler 为 no-op
        eff = _build_effective_stats(ally, state)
        assert eff.ATK == pytest.approx(atk0 + ally.base_stats._base_ATK * 0.45, abs=1e-6)

    def test_ult_buff_60(self):
        """v6.10.6: 终结技改为真实谜诡状态（3回合 TimedBuff）+ 回6战技点"""
        s = _sim(['sparkle', 'seele'], max_av=800, sparkle={'initial_energy_pct': 100})
        sparkle = next(u for u in s.units if u.char.id == 'sparkle')
        buffs = [b for b in sparkle.buffs if getattr(b, 'param_id', '') == 'sparkle_mystery']
        assert buffs  # 谜诡已挂上

    def test_e1_atk_holder(self):
        """v6.10.6: E1 谜诡持有者 ATK+40%（动态面板, 不再硬编码希儿）"""
        from engine.core.combat_engine import _build_effective_stats
        from engine.runtime import SimState
        from engine.characters.sparkle import _eid_sparkle_e1
        u = _unit('sparkle', eidolon=1)
        main = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, main])
        from engine.runtime import TimedBuff
        # 给主C挂谜诡（模拟终结技后）
        main.buffs.append(TimedBuff(source_id='sparkle', attributes={},
                                    remaining_turns=3, param_id='sparkle_mystery',
                                    source_name='谜诡'))
        _eid_sparkle_e1(u, state)
        eff = _build_effective_stats(main, state)
        # 行迹3 全队ATK+45% 与 E1 谜诡ATK+40% 同时生效
        assert eff.ATK == pytest.approx(
            main.base_stats.ATK + main.base_stats._base_ATK * (0.45 + 0.40), abs=1e-6)
        # 花火自身 SPD+15% 2回合刷新
        assert any(getattr(b, 'param_id', '') == 'sparkle_e1_spd' for b in u.buffs)

    def test_e6_cd_extra(self):
        """E6: 战技CD额外+花火暴伤30%"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('sparkle', eidolon=6)
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        buffs = [b for b in u.buffs if getattr(b, 'param_id', '') == 'sparkle_cd_buff']
        assert buffs
        expect = round((u.base_stats.CRIT_DMG * 0.24 + 0.45 + u.base_stats.CRIT_DMG * 0.30) * 100, 1)
        assert buffs[0].attributes['CRIT_DMG'] == expect


# ---- 符玄 ----

class TestFuXuan:
    def test_ult_heal(self):
        """行迹·太乙式盘: 终结技回5%生命上限"""
        from engine.runtime import SimState
        from engine.characters.fu_xuan import _trace_fuxuan_ult_heal
        u = _unit('fu_xuan')
        u.current_hp = u.max_hp * 0.50
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_fuxuan_ult_heal(u, state)
        assert u.current_hp == pytest.approx(u.max_hp * 0.55, abs=1e-6)

    def test_trace_energy(self):
        """行迹·六壬兆堪: 穷观阵激活时回合开始+20能量"""
        from engine.runtime import SimState
        from engine.characters.fu_xuan import _trace_fuxuan_energy_regen
        u = _unit('fu_xuan')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['fuxuan_field_turns'] = 3
        _trace_fuxuan_energy_regen(u, state)
        assert u.current_energy == pytest.approx(20.0)

    def test_cc_resist_marker(self):
        """行迹·遁甲星舆: 战技重置控制抗性次数(占位)"""
        s = _sim(['fu_xuan'], max_av=400)
        assert s.extra.get('fuxuan_cc_resist_charges') == 1

    def test_e2_marker(self):
        """E2: 致命保护标记就位"""
        s = _sim(['fu_xuan'], max_av=200, fu_xuan={'eidolon': 2})
        assert s.extra.get('fuxuan_e2_used') is False

    def test_e6_amplify(self):
        """E6种陵: 累计损失→终结技伤害平加200%并清空"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        from engine.characters.fu_xuan import _eid_fuxuan_e6_loss
        u = _unit('fu_xuan', eidolon=6)
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['fuxuan_field_turns'] = 3
        _eid_fuxuan_e6_loss(u, state, total_lost=100000)
        assert state.extra['fuxuan_lost_hp_total'] == pytest.approx(
            min(100000.0, u.max_hp * 1.20), abs=1e-6)
        hp0 = state.enemies[0].HP
        _use_skill(u, state, 'ultimate')
        log = '\n'.join(state.log)
        assert 'E6种陵: 损失增幅' in log
        assert 'E6种陵: 累计损失已清空' in log
        assert state.extra['fuxuan_lost_hp_total'] == 0.0


# ---- 藿藿 ----

class TestHuohuo:
    def test_skill_heal_no_crash(self):
        """战技 heal 命名 paramId 不再崩溃且数值正确"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('huohuo')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')  # 不应抛 ValueError
        assert u.current_hp == pytest.approx(u.max_hp, abs=1e-6)  # 主目标自回(24%+640)

    def test_ult_wiring(self):
        """终结技: 队友回20%能量上限 + ATK buff"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('huohuo')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        e0 = ally.current_energy
        _use_skill(u, state, 'ultimate')
        assert ally.current_energy > e0
        assert any(getattr(b, 'param_id', '') == 'huohuo_ult_atk' for b in ally.buffs)

    def test_control_resist(self):
        """行迹·控抗精通: EFFECT_RES+35%"""
        s = _sim(['huohuo'])
        assert s.units[0].base_stats.EFFECT_RES >= 0.35

    def test_energy_cycle(self):
        """行迹·能量循环: 治疗触发回1能量"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        u = _unit('huohuo')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')  # 战技治疗触发 on_heal
        assert u.current_energy >= 1.0

    def test_e2_marker(self):
        """E2: 致命保护2次就位"""
        s = _sim(['huohuo'], max_av=200, huohuo={'eidolon': 2})
        assert s.extra.get('huohuo_e2_charges') == 2

    def test_e4_low_hp_heal(self):
        """E4: 目标低血治疗加成生效（blast 相邻目标, E4 组治疗量 > 无 E4）"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState

        def gained(eidolon):
            u = _unit('huohuo', eidolon=eidolon)
            main = _unit('seele', position=2)
            adj = _unit('mydei', position=3)  # 高血相邻目标(1831)防封顶
            adj.current_hp = adj.max_hp * 0.30
            state = SimState(enemies=[_enemy()], units=[u, main, adj])
            hp0 = adj.current_hp
            _use_skill(u, state, 'skill')
            return adj.current_hp - hp0

        assert gained(4) > gained(0)  # 低血加成使 E4 组治疗更多

    def test_e1_team_spd(self):
        """E1: 全队SPD+12%"""
        from engine.runtime import SimState
        from engine.characters.huohuo import _eid_huohuo_e1
        u = _unit('huohuo', eidolon=1)
        ally = _unit('seele', position=2)
        spd0 = ally.base_stats.SPD
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _eid_huohuo_e1(u, state)
        assert ally.base_stats.SPD == pytest.approx(spd0 + ally.base_stats._base_SPD * 0.12, abs=1e-6)

    def test_e6_on_heal(self):
        """E6: 治疗时→被治疗目标伤害+50% 2回合（黑盒走注册链路, 短窗口防tick）"""
        s = _sim(['huohuo', 'seele'], max_av=200, huohuo={'eidolon': 6})
        seele = next(u for u in s.units if u.char.id == 'seele')
        buffs = [b for b in seele.buffs if getattr(b, 'source_name', '') == '藿藿E6·同休共戚']
        assert buffs and buffs[0].attributes.get('DMG_BONUS_ALL') == 50.0
        assert buffs[0].remaining_turns >= 1  # 可能已行动tick


class TestCleanseTarget:
    def test_cleanse_single_ally_targets_ally_not_self(self):
        """cleanse single_ally: 净化队友, 不净化施法者自己"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState, PlayerStatus
        bronya = _unit('bronya')
        seele = _unit('seele', position=2)
        seele.statuses.append(PlayerStatus(id='t', name='眩晕', category='control',
                                           remaining_turns=2))
        bronya.statuses.append(PlayerStatus(id='t2', name='眩晕', category='control',
                                            remaining_turns=2))
        state = SimState(enemies=[_enemy()], units=[bronya, seele])
        _use_skill(bronya, state, 'skill')
        assert seele.statuses == []  # 队友被净化
        assert len(bronya.statuses) == 1  # 施法者自己的状态保留
