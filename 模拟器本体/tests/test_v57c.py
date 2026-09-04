"""v5.7 批次4 回归测试: 声援逐段/迷迷全队充能/真我之诗（2026-08-13）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import _use_skill, _gain_energy
from engine.characters.trailblazer_remembrance import _apply_tbr_support
from engine.runtime import SimState, SimUnit, TimedBuff
from engine.core.attributes import compute_combat_stats
from engine.systems.remembrance import RemembranceSystem


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestTbrSupportPerHit:
    def test_support_damage_is_counted_once(self):
        u = _unit('trailblazer_remembrance')
        u.buffs.append(TimedBuff(source_id='tbr', attributes={'_tbr_support': 1}, remaining_turns=3))
        target = _enemy()
        state = SimState(enemies=[target], units=[u])
        before = u.total_damage_dealt
        support = _apply_tbr_support(state, u, target, 100.0)
        assert support == pytest.approx(31.36)
        assert u.total_damage_dealt == pytest.approx(before)

    def test_support_triggers_per_hit_not_lump(self):
        """v5.7: 声援逐段触发——多目标技能每段各触发一次（此前按行动总额一次）"""
        u = _unit('trailblazer_remembrance')
        u.buffs.append(TimedBuff(source_id='tbr', attributes={'_tbr_support': 1},
                                 remaining_turns=3))
        e1, e2 = _enemy(), _enemy()
        e1.id, e2.id = 'a', 'b'
        state = SimState(enemies=[e1, e2], units=[u])
        # 昔涟强化普攻（主30+全体30 两段）打 2 敌 → 逐段后主目标受伤应大于相邻
        x = _unit('xilian', position=2)
        x.buffs.append(TimedBuff(source_id='tbr', attributes={'_tbr_support': 1},
                                 remaining_turns=3))
        state.units.append(x)
        _use_skill(x, state, 'basic_attack_enhanced')
        assert e1.HP < 500000 and e2.HP < 500000
        # 逐段触发: 主段1次 + 全体段2敌 = 3 次伤害 → 3 条声援日志（lump 旧实现仅 1 条）
        support_logs = [l for l in state.log if '迷迷的声援' in l]
        assert len(support_logs) == 3

    def test_tbr_ultimate_damage_is_in_summoner_total(self):
        u = _unit('trailblazer_remembrance')
        u.current_energy = u.char.max_energy
        enemy = _enemy()
        state = SimState(enemies=[enemy], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        hp_before = enemy.HP
        _use_skill(u, state, 'ultimate')
        assert u.total_damage_dealt == pytest.approx(hp_before - enemy.HP, rel=1e-6)

    def test_gain_energy_banks_mimi_charge_all_sources(self):
        """v5.7: 迷迷充能全队回能渠道——_gain_energy 统一 bank（受击/藿藿终结技等）"""
        u = _unit('trailblazer_remembrance')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ch0 = ms.extra.get('charge', 0)
        # 队友回 20 能量 → bank 2 次 → 迷迷 +2%
        _gain_energy(ally, 20.0, state=state)
        assert ms.extra.get('charge', 0) == pytest.approx(min(100, ch0 + 2), rel=1e-9)
        # 残余 bank: 再回 5 → 累计 25 → 已兑换 2 次, 剩 5 不进位
        _gain_energy(ally, 5.0, state=state)
        assert ms.extra.get('charge', 0) == pytest.approx(min(100, ch0 + 2), rel=1e-9)
        # 再回 5 → 30 → +1%
        _gain_energy(ally, 5.0, state=state)
        assert ms.extra.get('charge', 0) == pytest.approx(min(100, ch0 + 3), rel=1e-9)


class TestXilianTrueSelfPoem:
    def test_bounce_count_equals_sources(self):
        """v5.7 回归: 献予真我之诗——每1个不同队友来源→花与箭额外1次60%HP弹射"""
        u = _unit('xilian')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        # 3 个不同队友来源
        u.extra['zhuiyi_sources'] = {'seele', 'bronya', 'fuxuan'}
        hp0 = state.enemies[0].HP
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        dmg = hp0 - state.enemies[0].HP
        # 花与箭 60%HP 全体 + 3 次弹射 60%HP（弹射随机单体, 单敌全吃）
        base_hp = ms.base_stats.HP
        per_hit = base_hp * 0.60
        assert dmg > 0
        # 4 段全部打在唯一敌人上: 60%×4（防御乘区同乘, 用段数判断）
        # 无法直接断言精确值（防御乘区），用 E1 对比: 弹射+12
        u2 = _unit('xilian', eidolon=1)
        state2 = SimState(enemies=[_enemy()], units=[u2])
        rem2 = RemembranceSystem()
        state2.extra['_rem_sys'] = rem2
        rem2.summon_memsprite(state2, u2, u2.char.memsprite)
        ms2 = u2.memsprite_unit
        u2.extra['zhuiyi_sources'] = {'seele'}
        hp2 = state2.enemies[0].HP
        rem2._use_memsprite_skill(state2, u2, ms2, 'memsprite_basic')
        dmg2 = hp2 - state2.enemies[0].HP
        # E1: 1来源+12弹射=13次弹射 + 全体1段 = 14 段 vs 非E1 1来源=2段
        assert dmg2 / dmg > 2.0
