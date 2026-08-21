"""v6.8b 回归: 裁决落实(9dbf9e1)边界 + 白厄秘技补全 + 前端置顶顺序（Harness 复审）

覆盖: 白厄秘技 max_sp+3 队伍效果不门控 / 开怪全效果 / ATK+50% 两层上限 / 每波200% /
赛飞儿进战秘技门控 / 大丽花秘技开战削韧 20/10 判定。语义依据: 各角色 txt 秘技段 + v6.7b 裁决记录。"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import (
    SimState, SimUnit, _build_effective_stats, _phainon_kasier_end,
    _apply_phainon_tech_wave,
)
from engine.core.combat_utils import (_tech_phainon, _tech_cipher, _tech_the_dahlia)


def _enemy(hp=500000, toughness=200, broken=False):
    e = Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness, max_toughness=toughness, level=80,
              element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0, '虚数': 0, '物理': -0.2, '火': 0.2})
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


def _tech_cat(cid):
    c = load_character(cid, 'data/characters')
    tech = c.skills.get('technique')
    return getattr(tech, 'technique_category', '') if tech else ''


class TestPhainonTechnique:
    def test_non_opener_keeps_team_effect(self):
        """非开怪者: 秘技点上限+3 队伍效果仍生效, 进战效果不生效"""
        u = _unit('phainon')
        st = SimState(enemies=[_enemy()], units=[u])
        e0 = u.current_energy
        _tech_phainon(st, u, is_opener=False)
        assert st.max_sp == 8  # 5+3
        assert u.current_energy == pytest.approx(e0)  # 无25能
        assert not st.extra.get('phainon_tech_active')
        assert not any(getattr(b, 'source_name', '') == '终结之始' for b in u.buffs)

    def test_opener_full_effect(self):
        """开怪者: +3上限/全队25能/毁伤2/SP1/ATK+50%第1层/首波200%伤害"""
        u = _unit('phainon')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        e0 = u.current_energy
        sp0 = st.skill_points
        _tech_phainon(st, u, is_opener=True)
        assert st.max_sp == 8
        # 白厄能量上限 12 → 25 能被上限截断（引擎正确）
        # v6.10: 白厄特殊能量角色, 自身不吃全队25能(队友仍吃)
        assert u.current_energy - e0 == pytest.approx(0.0)
        assert u.extra.get('huishang') == 2
        assert st.skill_points == sp0 + 1
        # v6.8.1: ATK+50%×2层归位行迹3, 秘技不再给 ATK
        assert not any(getattr(b, 'source_name', '') == '终结之始' for b in u.buffs)
        assert e.HP < 500000  # 首波 200% 伤害
        assert st.extra.get('phainon_tech_active') is True

    def test_atk_stack_capped_two_layers(self):
        """行迹3: 进入战斗+变身结束=2层, 再叠不超2层（v6.8.1: 归位行迹3, 不依赖秘技）"""
        u = _unit('phainon')
        st = SimState(enemies=[_enemy()], units=[u])
        st.extra['navs'] = {}
        from engine.core.effect_resolver import _trace_phainon_trace3
        _trace_phainon_trace3(u, st)  # 行迹3: 进战第1层
        assert u.extra.get('phainon_atk_stacks') == 1
        _phainon_kasier_end(st, u)  # 变身结束 → 第2层
        assert u.extra.get('phainon_atk_stacks') == 2
        s2 = _build_effective_stats(u, st)
        _phainon_kasier_end(st, u)  # 再次退出 → 不超2层
        assert u.extra.get('phainon_atk_stacks') == 2
        assert _build_effective_stats(u, st).ATK == pytest.approx(s2.ATK)

    def test_wave_damage(self):
        """每波200%ATK: 直接调用波次函数造成物理伤害"""
        u = _unit('phainon')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        _apply_phainon_tech_wave(st, u)
        assert e.HP < 500000
        assert u.total_damage_dealt > 0


class TestCipherTechniqueGating:
    def test_non_opener_no_effect(self):
        """赛飞儿进战秘技: 非开怪者不生效（v6.7b 裁决）"""
        u = _unit('cipher')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        _tech_cipher(st, u, is_opener=False)
        assert e.HP == pytest.approx(500000)
        assert u.total_damage_dealt == 0

    def test_opener_effect(self):
        u = _unit('cipher')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        _tech_cipher(st, u, is_opener=True)
        assert e.HP < 500000


class TestDahliaTechniqueBreakAmt:
    def _state(self, opener_cid):
        d = _unit('the_dahlia', position=1)
        o = _unit(opener_cid, position=2)
        st = SimState(enemies=[_enemy(broken=True)], units=[d, o])
        st.extra['opener_id'] = opener_cid
        return d, st

    def test_opener_battle_start_uses_20(self):
        """开怪者持进战秘技→开战削韧20"""
        d, st = self._state('phainon')
        assert _tech_cat('phainon') == 'battle_start'
        _tech_the_dahlia(st, d, is_opener=False)
        assert any('开战削韧20' in l for l in st.log)

    def test_opener_not_battle_start_uses_10(self):
        """开怪者非进战秘技（普攻进战）→开战削韧10"""
        d, st = self._state('bronya')  # 布洛妮娅秘技=support（非进战）
        assert _tech_cat('bronya') == 'support'
        _tech_the_dahlia(st, d, is_opener=False)
        assert any('开战削韧10' in l for l in st.log)
        assert not any('开战削韧20' in l for l in st.log)
