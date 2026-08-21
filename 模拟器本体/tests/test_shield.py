"""P5: 护盾系统测试（吸收顺序 + 施加 + SHIELD_BONUS + 隐士4pc + 忆灵盾）"""
from types import SimpleNamespace
import pytest
from engine.models.character import load_character, SkillEffect
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimUnit, SimState, _apply_hit, _apply_skill_effects,
)


def _enemy(hp=500000, attacks=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=200, max_toughness=200, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks)


def _unit(cid, position=1, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


class TestAbsorption:
    def test_shield_absorbs_first(self):
        """盾 150 / 伤害 200 → HP 只扣 50, 盾归零"""
        u = _unit('seele')
        u.shield = 150.0
        state = SimState(enemies=[_enemy()], units=[u])
        hp0 = u.current_hp
        _apply_hit(state, u, 200.0, state.enemies[0])
        assert u.shield == 0.0
        assert u.current_hp == pytest.approx(hp0 - 50.0, abs=1e-6)

    def test_shield_full_absorption(self):
        """盾 300 / 伤害 200 → 完全吸收, HP 不减"""
        u = _unit('seele')
        u.shield = 300.0
        state = SimState(enemies=[_enemy()], units=[u])
        hp0 = u.current_hp
        _apply_hit(state, u, 200.0, state.enemies[0])
        assert u.current_hp == hp0
        assert u.shield == pytest.approx(100.0, abs=1e-6)

    def test_absorption_not_hp_loss(self):
        """盾吸收不算 HP 损失: 万敌受击充能不触发, on_hp_loss 不触发"""
        seen = []
        mydei = _unit('mydei')
        mydei.shield = 5000.0
        mydei.extra['mydei_charge'] = 0.0
        state = SimState(enemies=[_enemy()], units=[mydei])
        def spy(**kw):
            seen.append(1)
        state.hooks.register('mydei', 'on_hp_loss', spy)
        _apply_hit(state, mydei, 200.0, state.enemies[0])
        assert seen == []
        assert mydei.extra.get('mydei_charge', 0.0) == 0.0


class TestApply:
    def test_shield_effect_apply(self):
        """shield effect: 盾值 = value × (1+SHIELD_BONUS)"""
        u = _unit('seele')
        u.base_stats.SHIELD_BONUS = 0.2
        state = SimState(enemies=[_enemy()], units=[u])
        skill = SimpleNamespace(name='测试', effects=[
            SkillEffect(type='shield', target='self', value=1000.0)])
        _apply_skill_effects(u, state, skill, 'skill')
        assert u.shield == pytest.approx(1200.0, abs=1e-6)

    def test_shield_all_allies(self):
        """target=all_allies: 全队加盾"""
        u = _unit('seele')
        ally = _unit('xilian', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        skill = SimpleNamespace(name='测试', effects=[
            SkillEffect(type='shield', target='all_allies', value=500.0)])
        _apply_skill_effects(u, state, skill, 'skill')
        assert u.shield == 500.0
        assert ally.shield == 500.0


class TestRelic:
    def test_hidden_cd_on_shield(self):
        """隐士4pc: 受盾者持 15% CD buff（2回合）"""
        u = _unit('seele')
        u._active_relic_conditions = {'shield_ally_cd'}
        state = SimState(enemies=[_enemy()], units=[u])
        skill = SimpleNamespace(name='测试', effects=[
            SkillEffect(type='shield', target='self', value=1000.0)])
        _apply_skill_effects(u, state, skill, 'skill')
        buffs = [b for b in u.buffs if b.source_id == '隐士4pc']
        assert buffs and buffs[0].attributes.get('CRIT_DMG') == 15.0
        assert buffs[0].remaining_turns == 2

    def test_no_relic_no_buff(self):
        """未佩戴隐士4pc: 不加 CD buff"""
        u = _unit('seele')
        state = SimState(enemies=[_enemy()], units=[u])
        skill = SimpleNamespace(name='测试', effects=[
            SkillEffect(type='shield', target='self', value=1000.0)])
        _apply_skill_effects(u, state, skill, 'skill')
        assert not any(b.source_id == '隐士4pc' for b in u.buffs)


class TestMemspriteShield:
    def test_memsprite_shield_absorbs(self):
        """忆灵护盾吸收"""
        from engine.core.attributes import CombatStats
        from engine.models.memsprite import MemSprite
        from engine.systems.remembrance import MemSpriteUnit
        summoner = _unit('seele')
        ms = MemSpriteUnit(data=MemSprite(name='测试忆灵'), summoner_id='seele',
                           current_hp=100, max_hp=100,
                           base_stats=CombatStats(DEF=100))
        ms.shield = 60.0
        state = SimState(enemies=[_enemy()], units=[summoner], memsprites=[ms])
        _apply_hit(state, ms, 100.0, state.enemies[0])
        assert ms.current_hp == pytest.approx(60.0, abs=1e-6)  # 100-40
        assert ms.shield == 0.0
