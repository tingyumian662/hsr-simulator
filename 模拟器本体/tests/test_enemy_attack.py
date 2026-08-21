"""敌方攻击系统测试（Phase A: 行动条/选人/受击/死亡/av_delayed 消费）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate


def _enemy(hp=500000, atk=100, spd=80, toughness=20, attacks=None, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=atk, DEF=800, SPD=spd,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                     '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks)


# 可控小伤敌人（长窗口安全）与标准攻击技能
SWING = [{"name": "挥击", "element": "物理", "damage_type": "direct",
          "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]


def _sim(ids, max_av=800, enemy=None, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, enemy or _enemy(), max_av=max_av)


def _unit(cid, position=1, eidolon=None, **extra):
    from engine.core.combat_sim import SimUnit
    from engine.core.attributes import compute_combat_stats
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    # v6.10.6: eidolon 参数必须真正写入 eidolon_rank（此前落入 extra, 星魂从未生效,
    # 测试依赖旧无门控行为误通过）
    if eidolon is not None:
        u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestEnemyTurn:
    def test_enemy_takes_y_axis_turn(self):
        """带 attacks 的敌人走 Y 轴行动条并攻击"""
        s = _sim(['seele'], max_av=600, enemy=_enemy(atk=10, attacks=SWING))
        log = '\n'.join(s.log)
        assert '挥击' in log
        assert s.units[0].current_hp < s.units[0].max_hp  # 角色掉血

    def test_enemy_no_attack_noop(self):
        """无 attacks 敌人 no-op 回合: 不造成伤害"""
        s = _sim(['seele'], max_av=800, enemy=_enemy())
        hp0 = s.units[0].current_hp
        assert hp0 == s.units[0].max_hp  # 无攻击技能→不掉血
        assert '无攻击技能' in '\n'.join(s.log)

    def test_attack_damage_exact(self):
        """伤害精确: ATK=100, DEF=460 → 100×0.9×1000/1460"""
        from engine.core.combat_sim import SimState, _enemy_attack
        u = _unit('seele')  # seele DEF=363? 直接用面板算
        state = SimState(enemies=[_enemy(atk=100, attacks=SWING)], units=[u])
        from engine.core.combat_sim import _build_effective_stats
        from engine.core.damage import calculate_damage
        from engine.core.combat_sim import CharacterAsTarget, _enemy_attack_stats
        stats = _enemy_attack_stats(state.enemies[0])
        t_stats = _build_effective_stats(u, state)
        view = CharacterAsTarget(u, t_stats)
        d = calculate_damage(stats, view, stats.ATK, 100.0, "direct", "物理", 80, False)
        hp0 = u.current_hp
        _enemy_attack(state, state.enemies[0])
        assert u.current_hp == pytest.approx(hp0 - d.final_damage, abs=1e-6)

    def test_area_attack_uses_each_target_defense(self):
        """范围攻击必须按每名目标的防御面板分别结算。"""
        from engine.core.combat_sim import (
            SimState, _enemy_attack, _build_effective_stats,
            CharacterAsTarget, _enemy_attack_stats,
        )
        from engine.core.damage import calculate_damage

        blast = [{**SWING[0], 'target_type': 'all_enemies'}]
        enemy = _enemy(atk=100, attacks=blast)
        fu = _unit('fu_xuan')
        seele = _unit('seele', position=2)
        state = SimState(enemies=[enemy], units=[fu, seele])
        attacker_stats = _enemy_attack_stats(enemy)
        expected = []
        for unit in (fu, seele):
            target_stats = _build_effective_stats(unit, state)
            damage = calculate_damage(
                attacker_stats, CharacterAsTarget(unit, target_stats),
                attacker_stats.ATK, 100.0, 'direct', '物理', enemy.level, False,
            ).final_damage
            expected.append(damage)
        hp0 = [fu.current_hp, seele.current_hp]

        _enemy_attack(state, enemy)

        assert fu.current_hp == pytest.approx(hp0[0] - expected[0], abs=1e-6)
        assert seele.current_hp == pytest.approx(hp0[1] - expected[1], abs=1e-6)

    def test_break_dot_kill_prevents_enemy_attack(self):
        """DOT 在敌方行动开始时击杀后，敌人不能继续攻击。"""
        from engine.core.combat_sim import SimState, _apply_break_debuff, _begin_enemy_turn

        enemy = _enemy(hp=1, attacks=SWING)
        unit = _unit('fengjin')
        state = SimState(enemies=[enemy], units=[unit])
        state.extra.update({
            'navs': {('e', 0): 0.0},
            'av_stamp': {('e', 0): 1},
            'stamp_counter': 1,
        })
        _apply_break_debuff(enemy, '风', unit, state)

        _begin_enemy_turn(state, enemy)

        assert enemy.HP == 0
        assert '挥击' not in '\n'.join(state.log)

    def test_taunt_weighted_targeting(self):
        """嘲讽加权: 存护(150) vs 巡猎(75) → 存护命中显著更多"""
        from engine.core.combat_sim import SimState, _select_enemy_target
        fu = _unit('fu_xuan')       # 存护 150
        seele = _unit('seele', position=2)  # 巡猎 75
        state = SimState(enemies=[_enemy()], units=[fu, seele])
        import random
        random.seed(42)
        hits = {'fu_xuan': 0, 'seele': 0}
        for _ in range(200):
            t = _select_enemy_target(state)
            hits[t.char.id] += 1
        assert hits['fu_xuan'] > hits['seele'] * 1.5  # 150/75=2:1 权重

    def test_av_delayed_consumed(self):
        """av_delayed 消费: 击破推条2500 后敌方行动推迟"""
        s1 = _sim(['seele'], max_av=600, enemy=_enemy(atk=10, attacks=SWING, toughness=3))
        s2 = _sim(['seele'], max_av=600, enemy=_enemy(atk=10, attacks=SWING, toughness=200))
        log1 = '\n'.join(s1.log)
        # 韧性3 被击破 → av_delayed 推条 → 同一窗口内敌方行动次数更少
        def swing_count(log):
            return sum(1 for l in log.splitlines() if '挥击' in l)
        assert swing_count(log1) < swing_count('\n'.join(s2.log))


class TestHitAndDeath:
    def test_character_death_removes_from_navs(self):
        """死亡: is_alive=False + navs 剔除 + 不再被选中"""
        from engine.core.combat_sim import SimState, _apply_hit, _next_y_actor
        u = _unit('seele')
        u.current_hp = 10
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[u])
        state.extra['navs'] = {0: 100.0}
        state.extra['av_stamp'] = {0: 1}
        state.extra['stamp_counter'] = 1
        _apply_hit(state, u, 50, state.enemies[0])
        assert u.is_alive is False
        assert 0 not in state.extra['navs']
        actor, _ = _next_y_actor(state)
        assert actor is None or actor is not u

    def test_team_wipe_ends_simulation(self):
        """全队阵亡: 模拟终止（高韧防击破推条拖慢敌方攻击）"""
        s = _sim(['seele'], max_av=3000,
                 enemy=_enemy(atk=500, attacks=SWING, toughness=200))
        log = '\n'.join(s.log)
        assert '全队阵亡, 模拟结束' in log
        assert s.units[0].is_alive is False

    def test_mydei_blood_debt_survives(self):
        """万敌血仇致命保护: 不死"""
        from engine.core.combat_sim import SimState, _apply_hit
        u = _unit('mydei')
        u.extra['is_blood_debt'] = True
        u.extra['debt_retain_charges'] = 3
        u.current_hp = 10
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[u])
        _apply_hit(state, u, 500, state.enemies[0])
        assert u.is_alive is True
        assert u.current_hp >= 1  # 水与泥土保留

    def test_memsprite_death_removes_runtime_references(self):
        """忆灵被敌方击杀后不应继续留在行动条或召唤者引用中。"""
        from engine.core.combat_sim import SimState, _apply_hit
        from engine.core.attributes import CombatStats
        from engine.models.memsprite import MemSprite
        from engine.systems.remembrance import MemSpriteUnit, RemembranceSystem

        summoner = _unit('seele')
        memsprite = MemSpriteUnit(
            data=MemSprite(name='测试忆灵'), summoner_id='seele',
            current_hp=10, max_hp=10, base_stats=CombatStats(DEF=100),
        )
        summoner.memsprite_unit = memsprite
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[summoner], memsprites=[memsprite])
        state.extra['_rem_sys'] = RemembranceSystem()

        _apply_hit(state, memsprite, 50, state.enemies[0])

        assert memsprite.is_alive is False
        assert memsprite not in state.memsprites
        assert summoner.memsprite_unit is None

    def test_fatal_protection_fuxuan_e2(self):
        """符玄E2 致命保护: 单场1次, 全队回70%"""
        from engine.core.combat_sim import SimState, _apply_hit
        from engine.core.effect_resolver import _eid_fuxuan_e2
        fu = _unit('fu_xuan', eidolon=2)
        seele = _unit('seele', position=2)
        seele.current_hp = 10
        state = SimState(enemies=[_enemy(attacks=SWING)], units=[fu, seele])
        state.extra['fuxuan_field_turns'] = 3
        _eid_fuxuan_e2(fu, state)
        _apply_hit(state, seele, 500, state.enemies[0])
        assert seele.is_alive is True
        assert seele.current_hp > 10  # 回70%生命上限
        assert state.extra.get('fuxuan_e2_used') is True
        # 第二次致命: 保护已用 → 死亡
        seele.current_hp = 5
        _apply_hit(state, seele, 500, state.enemies[0])
        assert seele.is_alive is False

    def test_hooks_fire(self):
        """on_enemy_attack/on_take_damage/on_hp_loss 触发"""
        from engine.core.combat_sim import SimState, _enemy_attack
        seen = []
        u = _unit('seele')
        state = SimState(enemies=[_enemy(atk=10, attacks=SWING)], units=[u])
        # 注册 spy handler
        def spy_ea(**kw): seen.append('enemy_attack'); return None
        def spy_td(**kw): seen.append('take_damage'); return None
        def spy_hl(**kw): seen.append('hp_loss'); return None
        state.hooks.register('seele', 'on_enemy_attack', spy_ea)
        state.hooks.register('seele', 'on_take_damage', spy_td)
        state.hooks.register('seele', 'on_hp_loss', spy_hl)
        _enemy_attack(state, state.enemies[0])
        assert 'enemy_attack' in seen
        assert 'take_damage' in seen
        assert 'hp_loss' in seen
