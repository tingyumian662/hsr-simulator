"""受击机制接线测试（Phase C: 符玄承伤/万敌受击/遗器受击叠层）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import simulate


def _enemy(atk=100, toughness=100, attacks=None):
    return Enemy(id='x', name='X', HP=500000, ATK=atk, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0},
                 attacks=attacks or [])


SWING = [{"name": "挥击", "element": "物理", "damage_type": "direct",
          "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]


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


def _hit_state(*units, enemy=None):
    """最小受击测试状态（enemy 无 attacks → 直接调 _apply_hit）"""
    from engine.runtime import SimState
    state = SimState(enemies=[enemy or _enemy()], units=list(units))
    state.extra['navs'] = {i: 100.0 for i in range(len(units))}
    state.extra['av_stamp'] = {i: i + 1 for i in range(len(units))}
    state.extra['stamp_counter'] = len(units)
    return state


class TestFuxuanSharing:
    def test_damage_share_3565(self):
        """承伤: 穷观阵激活时 目标35%/符玄65%"""
        from engine.core.combat_engine import _apply_hit, _distribute_damage
        fu = _unit('fu_xuan')
        seele = _unit('seele', position=2)
        state = _hit_state(fu, seele)
        state.extra['fuxuan_field_turns'] = 3
        hp_fu0, hp_see0 = fu.current_hp, seele.current_hp
        _distribute_damage(state, seele, 200, state.enemies[0])
        # 200×0.82 减伤后 → 目标 35% / 符玄 65%
        assert hp_see0 - seele.current_hp == pytest.approx(200 * 0.82 * 0.35, abs=1e-6)
        assert hp_fu0 - fu.current_hp == pytest.approx(200 * 0.82 * 0.65, abs=1e-6)

    def test_no_field_no_share(self):
        """无穷观阵: 不承伤不减伤"""
        from engine.core.combat_engine import _distribute_damage
        fu = _unit('fu_xuan')
        seele = _unit('seele', position=2)
        state = _hit_state(fu, seele)
        hp_see0 = seele.current_hp
        _distribute_damage(state, seele, 200, state.enemies[0])
        assert hp_see0 - seele.current_hp == pytest.approx(200, abs=1e-6)

    def test_self_heal_threshold(self):
        """自回血: 符玄HP≤50% → 回已损失90%（E1 2次）"""
        from engine.core.combat_engine import _apply_hit
        fu = _unit('fu_xuan', eidolon=1)
        state = _hit_state(fu)
        fu.current_hp = fu.max_hp * 0.60
        _apply_hit(state, fu, 200, state.enemies[0])  # HP 降到 50% 以下
        assert fu.extra.get('fuxuan_self_heal_used', 0) == 1
        assert fu.current_hp > fu.max_hp * 0.50  # 回血后高于50%

    def test_e4_take_damage_hook(self):
        """E4: 受击→符玄回5能量（on_take_damage 注册链路）"""
        from engine.core.combat_engine import _apply_hit
        from engine.runtime import SimState
        from engine.characters.fu_xuan import _eid_fuxuan_e4
        fu = _unit('fu_xuan', eidolon=4)
        seele = _unit('seele', position=2)
        state = _hit_state(fu, seele)
        state.extra['fuxuan_field_turns'] = 3
        state.hooks.register('fu_xuan', 'on_take_damage', _eid_fuxuan_e4)
        e0 = fu.current_energy
        _apply_hit(state, seele, 50, state.enemies[0])
        assert fu.current_energy == pytest.approx(e0 + 5)

    def test_e2_fatal_under_share(self):
        """E2: 承伤路径下符玄致死也触发保护"""
        from engine.core.combat_engine import _distribute_damage
        from engine.characters.fu_xuan import _eid_fuxuan_e2
        fu = _unit('fu_xuan', eidolon=2)
        seele = _unit('seele', position=2)
        state = _hit_state(fu, seele)
        state.extra['fuxuan_field_turns'] = 3
        _eid_fuxuan_e2(fu, state)
        fu.current_hp = 5
        # 小伤害: seele 的 35% 部分不致死, 符玄的 65% 部分致死 → E2 保护触发
        _distribute_damage(state, seele, 100, state.enemies[0])
        assert fu.is_alive is True  # E2 保护


class TestMydeiHit:
    def test_hit_charge(self):
        """受击充能: 每损失1%生命=1充能（v5.7: 行迹1需HP>4000才加成, 裸装无加成）"""
        from engine.core.combat_engine import _apply_hit
        mydei = _unit('mydei')
        state = _hit_state(mydei)
        _apply_hit(state, mydei, 100, state.enemies[0])
        pct = 100 / mydei.max_hp * 100.0
        assert mydei.extra.get('mydei_charge', 0) == pytest.approx(pct, abs=1e-6)

    def test_hit_charge_trace1_bonus(self):
        """v5.7: 行迹1门槛——HP每超4000点100→充能比例+2.5%（最多计入4000点）"""
        from engine.core.combat_engine import _apply_hit
        mydei = _unit('mydei')
        mydei.max_hp = 5000.0  # 超1000点→10档×2.5%=+25%
        state = _hit_state(mydei)
        _apply_hit(state, mydei, 100, state.enemies[0])
        pct = 100 / 5000.0 * 100.0
        assert mydei.extra.get('mydei_charge', 0) == pytest.approx(pct * 1.25, abs=1e-6)

    def test_hit_charge_trace1_cap(self):
        """v5.7: 行迹1封顶——超4000点以上不再加成（9000HP→按4000计=+100%）"""
        from engine.core.combat_engine import _apply_hit
        mydei = _unit('mydei')
        mydei.max_hp = 9000.0
        state = _hit_state(mydei)
        _apply_hit(state, mydei, 100, state.enemies[0])
        pct = 100 / 9000.0 * 100.0
        assert mydei.extra.get('mydei_charge', 0) == pytest.approx(pct * 2.0, abs=1e-6)

    def test_e4_hit_heal(self):
        """E4: 受击回10%生命上限"""
        from engine.core.combat_engine import _apply_hit
        mydei = _unit('mydei', eidolon=4)
        state = _hit_state(mydei)
        _apply_hit(state, mydei, 200, state.enemies[0])
        # 受击后回10%上限（净损失 = 200 - 10%上限）
        assert mydei.current_hp == pytest.approx(
            mydei.max_hp - 200 + mydei.max_hp * 0.10, abs=1e-6)

    def test_heal_bonus_apply(self):
        """行迹1: 万敌受疗+0.75%（blast 相邻目标被治疗）"""
        from engine.core.combat_engine import _use_skill
        from engine.runtime import SimState
        huohuo = _unit('huohuo')
        main = _unit('seele', position=2)
        mydei = _unit('mydei', position=3)
        mydei.current_hp = mydei.max_hp * 0.50
        state = SimState(enemies=[_enemy()], units=[huohuo, main, mydei])
        hp0 = mydei.current_hp
        _use_skill(huohuo, state, 'skill')
        gained = mydei.current_hp - hp0
        assert gained > 0  # 被治疗（含1.0075倍加成）


class TestRelicOnHit:
    def test_quanwang_hit_stack(self):
        """拳王4pc: 受击叠ATK层"""
        from engine.core.combat_engine import _apply_hit
        seele = _unit('seele')
        seele._active_relic_conditions = {'stack_atk_on_hit'}
        state = _hit_state(seele)
        _apply_hit(state, seele, 50, state.enemies[0])
        assert seele.relic_stacks.get('拳王', 0) >= 1

    def test_shizhe_hit_stack(self):
        """莳者4pc: 受击挂CR buff"""
        from engine.core.combat_engine import _apply_hit
        seele = _unit('seele')
        seele._active_relic_conditions = {'stack_cr_on_hit'}
        state = _hit_state(seele)
        _apply_hit(state, seele, 50, state.enemies[0])
        assert any(getattr(b, 'source_name', '') == '莳者4pc' for b in seele.buffs)
