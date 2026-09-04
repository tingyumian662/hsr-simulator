"""v6.2.1 回归测试: Harness 审查 5 P1 + 4 P2 修复

语义依据: HARNESS_HANDOFF.md v6.2.1 节 + CLAUDE_HANDOFF.md v6.2.1 记录"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import load_lightcone
from engine.core.combat_engine import _use_skill, _build_effective_stats, _lc_target_correct, _apply_hit, _begin_enemy_turn
from engine.characters.mydei import _mydei_blood_debt_tick, _mydei_fatal_recovery
from engine.runtime import SimState, SimUnit, TimedBuff
from engine.core.attributes import compute_combat_stats
from engine.systems.remembrance import RemembranceSystem, _ms_effective_stats
from engine.systems.remembrance import _exit_darkness as _exit_darkness_fn


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
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


# ── A1: 至暗之谜进入/退出还原 ──

class TestDarknessRestore:
    def test_enter_exit_restores_panels(self):
        u = _unit('changyeyue')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        vuln0, dmg0 = e.vulnerability, u.base_stats.DMG_BONUS_ALL
        # 进入
        _use_skill(u, state, 'ultimate')
        assert e.vulnerability == pytest.approx(vuln0 + 0.30)
        assert u.base_stats.DMG_BONUS_ALL == pytest.approx(dmg0 + 0.60)
        # 退出
        _exit_darkness_fn(state, u)
        assert e.vulnerability == pytest.approx(vuln0)
        assert u.base_stats.DMG_BONUS_ALL == pytest.approx(dmg0)

    def test_reenter_no_stack(self):
        """v6.2.1: 重复终结技不叠加加成"""
        u = _unit('changyeyue')
        u.current_energy = u.char.max_energy
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        dmg0 = u.base_stats.DMG_BONUS_ALL
        _use_skill(u, state, 'ultimate')
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')  # 重入: 加成不叠加
        assert u.base_stats.DMG_BONUS_ALL == pytest.approx(dmg0 + 0.60)


# ── A2: 血仇进入/退出还原 ──

class TestBloodDebtRestore:
    def test_enter_exit_restores_panels(self):
        u = _unit('mydei', eidolon=4)
        state = SimState(enemies=[_enemy()], units=[u])
        hp0, def0, pen0, cd0 = u.max_hp, u.base_stats.DEF, u.base_stats.DEF_PEN, u.base_stats.CRIT_DMG
        # 进入
        u.extra['mydei_charge'] = 100
        _mydei_blood_debt_tick(u, state)
        assert u.extra.get('is_blood_debt') is True
        assert u.base_stats.DEF == 0
        # 退出（致命攻击）
        u.current_hp = -1  # 触发致命
        _mydei_fatal_recovery(u, state)
        assert u.extra.get('is_blood_debt') is False
        assert u.max_hp == pytest.approx(hp0)
        assert u.base_stats.DEF == pytest.approx(def0)
        assert u.base_stats.DEF_PEN == pytest.approx(pen0)
        assert u.base_stats.CRIT_DMG == pytest.approx(cd0)
        # 回血基于还原后的生命上限
        assert u.current_hp == pytest.approx(hp0 * 0.50)


# ── A3: 迷梦 SPD 回减 + 忆质快照 ──

class TestNightSpdRestore:
    def test_spd_bonus_removed_next_turn(self):
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        spd0 = u.base_stats.SPD
        # 迷梦消失: 快照写入 → despawn 加成为正（行迹3 施放先+1忆质, consumed≥17）
        u.yizhi = 16
        rem._use_memsprite_skill(state, u, ms, 'memsprite_skill')
        amt = u.extra.get('night_spd_bonus_amt', 0.0)
        assert amt > 0
        assert u.base_stats.SPD == pytest.approx(spd0 + amt, rel=1e-9)
        # 下回合开始: 回减
        rem.tick_turn(state, u)
        assert u.base_stats.SPD == pytest.approx(spd0, rel=1e-9)

    def test_yizhi_snapshot_used(self):
        """v6.2.1: despawn 读清零前快照（此前恒 0 → 忆质段加成恒零）"""
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        spd0 = u.base_stats.SPD
        base = u.base_stats._base_SPD
        u.yizhi = 30  # 上限40内
        rem._use_memsprite_skill(state, u, ms, 'memsprite_skill')
        # 加成 = 10% + min(consumed,40)%（行迹3施放先+1 → consumed=31）→ 41%（按白值）
        assert u.base_stats.SPD == pytest.approx(spd0 + base * 0.41, rel=1e-9)
        rem.tick_turn(state, u)
        assert u.base_stats.SPD == pytest.approx(spd0, rel=1e-9)


# ── A4: 至高之姿退出回收 ──

class TestSovereignRestore:
    def test_exit_restores_panels(self):
        u = _unit('aglaea', eidolon=6)
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        atk0 = u.base_stats.ATK
        pen0 = u.base_stats.RES_PEN.get('雷', 0.0)
        # 进入至高（终结技）
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert u.is_sovereign is True
        assert u.base_stats.ATK > atk0
        # 衣匠消失 → 退出回收
        ms = u.memsprite_unit
        rem.despawn_memsprite(state, u, ms, reason='test')
        assert u.is_sovereign is False
        assert u.base_stats.ATK == pytest.approx(atk0)
        assert u.base_stats.RES_PEN.get('雷', 0.0) == pytest.approx(pen0)
        assert u.extra.get('sovereign_atk_bonus', 0) == 0
        assert u.extra.get('sovereign_spd_bonus', 0) == 0


# ── B: 光锥双重负向修正 ──

class TestLcTargetCorrectOnce:
    def test_event_code_not_double_negated(self):
        """v6.2.1: 结算副本只重跑目标码——event_* 不二次抵消（Harness P1-1）"""
        # 星海巡航: event_on_kill ATK+40%（事件缓冲器恢复一次）
        u = _unit('seele', lc_id='cruising_in_the_stellar_sea')
        state = SimState(enemies=[_enemy()], units=[u])
        base_stats = _build_effective_stats(u, state)
        # 静态面板已修正（事件未触发→ATK无加成或含静态残留修正）
        atk_static = base_stats.ATK
        # 结算副本重跑目标码修正后, event_* 码不应再被负向
        t = state.enemies[0]
        corrected = _lc_target_correct(base_stats, u, state, t)
        assert corrected.ATK == pytest.approx(atk_static, rel=1e-9)


# ── C: 忆灵击杀/击破事件 ──

class TestMemspriteKillEvents:
    def test_memsprite_kill_triggers_on_kill(self):
        """v6.2.1: 忆灵击杀触发 on_kill（Harness P1-5）"""
        from engine.hooks.base import HookRegistry
        u = _unit('changyeyue')
        e = _enemy(hp=100)
        state = SimState(enemies=[e], units=[u])
        hook = HookRegistry()
        killed = {'n': 0}
        def _on_kill(u, state, **kw):
            killed['n'] += 1
        hook.register('changyeyue', 'on_kill', _on_kill, source_name='测试')
        state.hooks = hook
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert killed['n'] >= 1
        assert state.extra.get('killed_this_action', 0) >= 1

    def test_memsprite_break_broadcasts_any_weakness_break(self):
        """v6.2.1: 忆灵击破广播 on_any_weakness_break（Harness P1-5）"""
        from engine.hooks.base import HookRegistry
        u = _unit('changyeyue')
        e = _enemy(toughness=1, hp=500000)
        state = SimState(enemies=[e], units=[u])
        hook = HookRegistry()
        breaks = {'n': 0}
        def _on_break(u, state, **kw):
            breaks['n'] += 1
        hook.register('x', 'on_any_weakness_break', _on_break, source_name='测试')
        state.hooks = hook
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert breaks['n'] >= 1


# ── D: 剩余 P2 ──

class TestP2Fixes:
    def test_fengjin_enqueue_no_heal(self):
        """v6.2.1: 入队只排队不治疗（Harness P2-1）"""
        u = _unit('fengjin')
        u.extra['clear_sky_turns'] = 3
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        heal0 = ms.cumulative_healing
        hp0 = u.current_hp
        from engine.characters.fengjin import _fengjin_extra_turn
        _fengjin_extra_turn(state, u)
        assert ms.cumulative_healing == pytest.approx(heal0)
        assert u.current_hp == pytest.approx(hp0)
        assert any(x is ms for x, k in state.extra.get('extra_turns', []))

    def test_force_action_rewrites_next_av(self):
        """v6.2.1: 立即行动后重写排程（Harness P2-2）"""
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        state.current_av = 5000.0
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.extra['next_av'] = 100.0  # 旧残留
        rem._force_memsprite_action(state, u, ms)
        assert ms.extra.get('next_av') != pytest.approx(100.0)
        assert ms.extra.get('next_av') > state.current_av

    def test_enemy_bounce_no_stop_iteration(self):
        """v6.2.1: 敌方弹射未命中初选目标不抛异常（Harness P2-3）"""
        u = _unit('seele')
        e = _enemy()
        e.attacks = [{'name': '弹射', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 50.0, 'target_type': 'bounce', 'hits': 3,
                      'priority': 'random'}]
        state = SimState(enemies=[e], units=[u])
        # 不抛异常即通过
        _begin_enemy_turn(state, e)
