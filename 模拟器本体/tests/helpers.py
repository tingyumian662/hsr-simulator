"""Shared test helpers for new-style tests (refactor project M0).

Existing test files keep their local ``_enemy``/``_unit`` copies untouched;
new tests should ``from helpers import _enemy, _unit`` instead of redefining
them. Run pytest from 模拟器本体 (relative ``data/`` paths, same as the rest
of the suite).
"""
from engine.core.attributes import compute_combat_stats
from engine.runtime import SimUnit
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import load_lightcone

ZERO_RES = {'物理': 0.0, '火': 0.0, '冰': 0.0, '雷': 0.0,
            '风': 0.0, '量子': 0.0, '虚数': 0.0}


def _enemy(hp=500000, atk=100, spd=80, toughness=200, res=None, attacks=None,
           elite=False, **extra):
    """Canonical test enemy — superset of the local variants across the suite.

    toughness 默认 200；历史默认 20 的调用点必须显式传参。
    ``extra`` 直接透传 Enemy 字段（effect_res/vulnerability/...）。
    """
    return Enemy(
        id='x', name='X', HP=hp, ATK=atk, DEF=800, SPD=spd,
        toughness=toughness, max_toughness=toughness, level=80,
        element_res=dict(res or ZERO_RES), attacks=attacks,
        actions_per_turn=2 if elite else 1, **extra,
    )


def _unit(cid, position=1, eidolon=0, lc_id=None, **extra):
    """Canonical test unit：加载角色 → 面板属性 → 满血 → extra 写入。"""
    c = load_character(cid, 'data/characters')
    lc = load_lightcone(lc_id) if lc_id else None
    stats = compute_combat_stats(c, lc, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position, lightcone=lc)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u
