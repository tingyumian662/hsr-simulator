"""v6.2.1b 回归测试: Codex v6.2.1 独立复核 7 项修复（Harness 端修正）

语义依据: CODEX_HANDOFF.md v6.2.1 独立复核节 + HARNESS_HANDOFF.md v6.2.1 复审/修复记录"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, load_enemy
from engine.models.equipment import load_lightcone
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, TimedBuff, PlayerStatus,
    _multihit_damage, _exec_extra_turn, _respawn_wave,
    _use_skill, _ult_post, simulate, _next_y_actor, _set_av,
)
from engine.systems.remembrance import RemembranceSystem, _poem_tiankong


def _enemy(hp=500000, toughness=200, eid='x'):
    return Enemy(id=eid, name=eid.upper(), HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, lc_id=None, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    if lc_id:
        u.lightcone = load_lightcone(lc_id, 'data/light_cones')
    u.extra.update(extra)
    return u


# ── B1: 角色弹射逐段击杀管线（Codex P1-1） ──

class TestBounceKillPipeline:
    def _single_hit_damage(self, u, state):
        """同面板打一只高血敌人单段参考伤害（不触发击杀）"""
        ref = _enemy(hp=10 ** 9, eid='ref')
        return _multihit_damage(u.base_stats, [ref], u.base_stats.ATK, 100.0,
                                'direct', '虚数', False, hits=1, u=u, state=state)

    def test_single_enemy_death_stops_bounce_and_fires_kill(self):
        """单敌低血: 首段击杀后剩余段不再命中尸体, 击杀事件触发"""
        u = _unit('trailblazer_harmony')
        e = _enemy(hp=10)
        state = SimState(enemies=[e], units=[u])
        total = _multihit_damage(u.base_stats, [e], u.base_stats.ATK, 100.0,
                                 'direct', '虚数', False, hits=5, u=u, state=state)
        assert e.HP <= 0
        assert state.extra['killed_this_action'] == 1
        # 剩余4段无存活目标: 总伤害 = 单段
        assert total == pytest.approx(self._single_hit_damage(u, state))

    def test_two_low_enemies_only_two_hits_land(self):
        """双低血敌: 两段各自击杀, 第3段起无存活目标停止"""
        u = _unit('trailblazer_harmony')
        e1, e2 = _enemy(hp=10, eid='a'), _enemy(hp=10, eid='b')
        state = SimState(enemies=[e1, e2], units=[u])
        total = _multihit_damage(u.base_stats, [e1, e2], u.base_stats.ATK, 100.0,
                                 'direct', '虚数', False, hits=5, u=u, state=state)
        assert e1.HP <= 0 and e2.HP <= 0
        assert total == pytest.approx(self._single_hit_damage(u, state) * 2)
        assert state.extra['killed_this_action'] == 2

    def test_integration_bounce_kill_wave_refresh(self):
        """Codex 最小复现场景: 同谐 vs HP=1 敌 → 击杀+波次刷新, 模拟无异常"""
        enemy = load_enemy('target_dummy')
        enemy.HP = 1
        configs = [{'char': load_character('trailblazer_harmony'), 'lightcone': None,
                    'relics': [], 'relic_sets': {}, 'position': 1, 'eidolon': 0}]
        state = simulate(configs, enemy, max_av=400.0)
        assert any('第2波' in l for l in state.log)
        assert not any('[ERROR]' in l for l in state.log)


# ── B2: 真我之诗弹射复用共享管线（Codex P1-2） ──

class TestPoemBouncePipeline:
    def test_poem_bounce_fires_kill_events(self):
        """花与箭主伤打不死目标, 真我之诗弹射补刀 → 击杀事件由弹射触发

        E1: 弹射 13 段; 敌方 HP=3000 足够扛住主伤（60%忆灵HP≈467）但扛不住弹射连段。
        """
        u = _unit('xilian', eidolon=1)
        e = _enemy(hp=3000, toughness=9999)
        state = SimState(enemies=[e], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        u.extra['zhuiyi_sources'] = {'changyeyue'}  # 1 个来源 → E1 共 13 段弹射
        # 主伤第一段即击杀（HP=3000 < 主伤）的情况不存在: 主伤≈467 < 3000
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert e.HP <= 0
        # 主伤未击杀（1 次扣血不足 3000）→ 击杀必来自弹射路径
        assert state.extra['killed_this_action'] == 1


# ── B3: 忆灵击破 lc_break_enemy 上下文（Codex P1-3） ──

class TestMemspriteBreakTargetContext:
    def test_break_sets_lc_break_enemy_context(self):
        """德谬歌击破第二目标 → 上下文指向被击破者（此前缺失, 光锥会退回首敌）"""
        u = _unit('xilian')
        e1 = _enemy(hp=500000, toughness=9999, eid='e1')  # 打不破
        e2 = _enemy(hp=500000, toughness=2, eid='e2')      # 一削即破
        state = SimState(enemies=[e1, e2], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert e2.is_broken and not e1.is_broken
        assert state.extra.get('lc_break_enemy') is e2


# ── B4: 冻结跳过希儿额外回合收尾（Codex P1-4） ──

class TestFrozenSeeleCleanup:
    def test_frozen_extra_turn_clears_reproduce_state(self):
        u = _unit('seele')
        u.extra['seele_in_extra'] = True
        u.buffs.append(TimedBuff(source_id='seele', attributes={'DMG_BONUS_ALL': 80.0},
                                 remaining_turns=1, source_name='再现增幅'))
        u.statuses.append(PlayerStatus(id='t:fz', name='冻结', category='control',
                                       remaining_turns=2))
        state = SimState(enemies=[_enemy()], units=[u])
        _exec_extra_turn(state, u, 'extra')
        assert u.extra.get('seele_in_extra') is False
        assert not any(getattr(b, 'source_name', '') == '再现增幅' for b in u.buffs)


# ── B5: 天空诗统一回能入口（Codex P2-5） ──

class TestPoemTiankongEnergy:
    def test_energy_goes_through_unified_entry(self):
        """24能量经 _gain_energy: 风堇+24(×ER), 迷迷充能 bank 同步累计"""
        fj = _unit('fengjin')
        tbr = _unit('trailblazer_remembrance', position=2)
        state = SimState(enemies=[_enemy()], units=[fj, tbr])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, tbr, tbr.char.memsprite)
        ms = tbr.memsprite_unit
        ms.extra['charge'] = 0.0
        fj.current_energy = 0
        _poem_tiankong(state, None, None, fj)
        gained = fj.current_energy
        assert gained > 0
        assert tbr.extra['tbr_energy_bank'] == pytest.approx(gained % 10.0)
        assert ms.extra['charge'] == pytest.approx(float(int(gained // 10)))


# ── B6: 雨过天晴含忆灵（Codex P2-6, 项目主确认含死龙） ──

class TestClearSkyIncludesMemsprite:
    def test_hp_cap_applies_and_reverts_memsprite(self):
        """风堇终结技→小伊卡(忆灵)同样获得HP上限加成, 3回合后回退"""
        fj = _unit('fengjin')
        state = SimState(enemies=[_enemy()], units=[fj])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        fj.current_energy = fj.char.max_energy
        _use_skill(fj, state, 'ultimate')  # 终结技含召唤小伊卡
        _ult_post(state, fj)               # 雨过天晴
        ms = fj.memsprite_unit
        assert ms is not None and ms.is_alive
        orig = ms.extra.get('clear_sky_orig_maxhp')
        assert orig is not None
        assert ms.max_hp == pytest.approx(orig * 1.30 + 600)
        # 3 回合后回退
        for _ in range(3):
            rem.tick_turn(state, fj)
        assert ms.max_hp == pytest.approx(orig)


# ── B7: 至暗之谜跨波易伤（Codex P2-7） ──

class TestDarknessRespawn:
    def test_respawn_applies_darkness_vuln(self):
        """至暗之谜期间刷波 → 新敌人获得+30%易伤"""
        cy = _unit('changyeyue')
        cy.is_darkness = True
        state = SimState(enemies=[_enemy()], units=[cy])
        state.extra['enemy_blueprint'] = copy.deepcopy(state.enemies[0])
        state.extra['num_enemies'] = 2
        _respawn_wave(state)
        for ne in state.enemies:
            assert ne.vulnerability == pytest.approx(0.30)

    def test_respawn_without_darkness_no_vuln(self):
        """无至暗之谜时刷波 → 新敌人无额外易伤"""
        cy = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[cy])
        state.extra['enemy_blueprint'] = copy.deepcopy(state.enemies[0])
        state.extra['num_enemies'] = 2
        _respawn_wave(state)
        for ne in state.enemies:
            assert ne.vulnerability == 0.0


# ── B8: 忆灵达成戳补全, 同AV后到先动（P3-1） ──

class TestXiadieE2EnhancedSkill:
    def test_enhanced_skill_bonus_fires_when_dragon_alive(self):
        """E2 强化战技+30%新蕊: 死龙在场时遐蝶行动触发（规则3 hold 流程的单元验证）

        v6.2.1b 达成戳修正后, 死龙「0AV后到先动」先行动并自爆, 昔涟激活入队场景
        强化战技不再可达; 但遐蝶自己回合开大(hold→复活)时死龙仍在场 → 触发。
        """
        u = _unit('xiadie', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)  # 死龙召唤（E2 pending 已置位）
        u.xinrui = 20000.0
        rem.xiadie_ai(u, state)
        assert '遐蝶E2: 强化战技+30%新蕊' in '\n'.join(state.log)


class TestSummonStampTiebreak:
    def test_dragon_summon_wins_same_av_tie(self):
        """死龙0AV后到先动: 敌方同AV且先达成 → 死龙仍排其前"""
        u = _unit('xiadie')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        state.current_av = 100.0
        navs = state.extra.setdefault('navs', {})
        _set_av(state, navs, ('e', 0), 100.0)  # 敌方先达成同AV
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        assert ms.extra['next_av'] == pytest.approx(100.0)
        actor, av = _next_y_actor(state)
        assert actor is ms and av == pytest.approx(100.0)

    def test_generic_summon_later_achiever_goes_first(self):
        """双向验证: 忆灵先达成同AV → 后达成的敌方排其前"""
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        state.current_av = 50.0
        navs = state.extra.setdefault('navs', {})
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        target = ms.extra['next_av']
        _set_av(state, navs, ('e', 0), target)  # 敌方后达成同AV → 应排忆灵前
        actor, _ = _next_y_actor(state)
        assert actor is not ms and isinstance(actor, Enemy)

