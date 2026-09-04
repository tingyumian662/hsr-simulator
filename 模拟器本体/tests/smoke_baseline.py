#!/usr/bin/env python
"""Behavior baseline harness (refactor project M0) — combat_engine 拆分期间的行为不变锚点。

Usage（在 模拟器本体 目录下运行，脚本会自行 chdir 到仓库本体根）:
  python tests/smoke_baseline.py            # 默认 --check：与已入库基线比对，漂移则 exit 1
  python tests/smoke_baseline.py --update   # 显式重生成 tests/baseline_teams.json

契约（M0 门禁审核 P1/P2 修订版）:
- 任何 test_*.py 禁止以 --update 调用本脚本（pytest 运行期间不得写仓库文件）。
- 基线 JSON 随代码入库；只有项目主批准的行为变更轮次才允许 --update 后提交。
- 引擎含 ~30 处 random 调用且 simulate() 不播种——本脚本在每队/每角色模拟前
  显式 random.seed(...)（每队独立常数，不是全局播一次），实测播种后跨进程、
  跨 PYTHONHASHSEED 逐位一致。
- 覆盖面: 4 个四人队（启行+燃烧+击破 / 双忆灵 / 欢愉全链路 / 银狼植入+盾C）
  + 1 个异构敌精英双动波次场景；装备自动取 data/recommendations.json 首选
  （光锥/四件套/二件套/主词条全真实配置）；另含全名册 92 角色的推荐 digest
  与单人短局 digest。
"""
import hashlib
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.constants import RELIC_MAIN_STAT_VALUES, StatType
from engine.core.combat_engine import simulate
from engine.core.relic_optimizer import TOTAL_ROLLS, recommend_substats
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import RelicPiece, RelicSet, load_lightcone

DATA = ROOT / "data"
BASELINE = Path(__file__).resolve().parent / "baseline_teams.json"
SCHEMA_VERSION = 1

MAIN_VALUES = {}
for _pool in RELIC_MAIN_STAT_VALUES.values():
    for _st, _val in _pool.items():
        MAIN_VALUES.setdefault(_st.name, _val)
MAIN_STAT_TYPE = {k: getattr(StatType, k, k) for k in MAIN_VALUES}

DEFAULT_ATTACKS = [{"name": "挥击", "element": "物理", "damage_type": "direct",
                    "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]
ELEMENTS = ["物理", "火", "冰", "雷", "风", "量子", "虚数"]

# ── 场景定义 ─────────────────────────────────────────────────────────────
# members: (cid, eidolon)。装备来自 recommendations.json（无条目则裸装）。
# hetero: (name, hp, toughness, elite) —— 非空时走 v6.5 异构敌契约（精英双动+波次重生）。
TEAMS = {
    "t1_hn_combustion": {
        "members": [("himeko_nova", 0), ("firefly", 0), ("ruan_mei", 0), ("huohuo", 0)],
        "weakness": ["火"], "seed": 101,
    },
    "t2_double_memsprite": {
        "members": [("aglaea", 2), ("robin_summeretto", 0), ("trailblazer_harmony", 0), ("luocha", 0)],
        "weakness": ["雷"], "seed": 102,
    },
    "t3_elation": {
        "members": [("yinlang", 0), ("evanescia", 0), ("sparxie", 6), ("aventurine", 0)],
        "weakness": ["量子"], "seed": 103,
    },
    "t4_implant": {
        "members": [("silver_wolf", 1), ("sparkle", 0), ("dan_heng_permansor_terrae", 0), ("huohuo", 0)],
        "weakness": ["量子"], "seed": 104,
    },
    "t5_waves_elite": {
        "members": [("firefly", 0)],
        "weakness": ["火"], "seed": 105,
        "hetero": [("elite_a", 60000, 150, True), ("mob_b", 25000, 60, False),
                   ("mob_c", 25000, 60, False)],
    },
}


def _roster():
    return [p for p in sorted(DATA.glob("characters/*.json"))
            if not p.name.startswith("_") and not p.name.endswith(".bak")]


def _load_relic_sets():
    sets = {}
    for f in sorted(DATA.glob("relics/*.json")):
        if f.name.startswith("_") or f.name.endswith(".bak"):
            continue
        try:
            rs = RelicSet.from_json(str(f))
            sets[rs.name] = rs
        except Exception:
            pass
    return sets


def _build_enemy(eid, name, hp, toughness, weakness, elite=False, zero_res=False):
    if zero_res:
        res = {e: 0.0 for e in ELEMENTS}
    else:
        res = {e: (0.0 if e in weakness else 0.20) for e in ELEMENTS}
    return Enemy(
        id=eid, name=name, level=80, HP=hp, ATK=100, DEF=800, SPD=80,
        toughness=toughness, max_toughness=toughness,
        element_res=res, attacks=DEFAULT_ATTACKS,
        actions_per_turn=2 if elite else 1,
    )


def _build_member(cid, eidolon, position, rec, relic_sets):
    """按 api.py 的 canonical 构造法组队（装备取 recommendations 首选）。"""
    cfg = {"char": load_character(cid, "data/characters"), "relics": None,
           "relic_sets": relic_sets, "position": position, "eidolon": eidolon}
    r = rec.get(cid) or {}
    lc_id = r.get("light_cone")
    lc = None
    if lc_id:
        try:
            lc = load_lightcone(lc_id)
        except FileNotFoundError:
            # 推荐表可能引用尚未入库的光锥——裸装降级，不中断基线
            print(f"  [smoke] lc missing for {cid}: {lc_id} (bare load)")
    cfg["lightcone"] = lc
    if r:
        set4 = (r.get("set4") or [""])[0]
        set2 = (r.get("set2") or [""])[0]
        pieces = []
        for slot, sn, key, flat in [
            ("head", set4, None, (StatType.HP_FLAT, 705)),
            ("hands", set4, None, (StatType.ATK_FLAT, 352)),
            ("body", set4, "body", None),
            ("feet", set4, "feet", None),
            ("planar_sphere", set2, "sphere", None),
            ("link_rope", set2, "rope", None),
        ]:
            if key is None:
                mt, mv = flat
            else:
                name = (r.get(key) or [""])[0]
                mt, mv = MAIN_STAT_TYPE.get(name, ""), MAIN_VALUES.get(name, 0)
            pieces.append(RelicPiece(slot=slot, set_name=sn, main_stat_type=mt,
                                     main_stat_value=mv, sub_stats={}))
        cfg["relics"] = pieces
    return cfg


def _run_team(spec, rec, relic_sets):
    configs = [_build_member(cid, eid, i + 1, rec, relic_sets)
               for i, (cid, eid) in enumerate(spec["members"])]
    if spec.get("hetero"):
        templates = [_build_enemy(f"e{i}", nm, hp, tv, spec["weakness"], elite=el)
                     for i, (nm, hp, tv, el) in enumerate(spec["hetero"])]
        random.seed(spec["seed"])
        st = simulate(configs, templates[0], max_av=1000.0,
                      num_enemies=len(templates), enemy_templates=templates)
    else:
        enemy = _build_enemy("battle", "木桩", 500000, 200, spec["weakness"])
        random.seed(spec["seed"])
        st = simulate(configs, enemy, max_av=1000.0)
    return {
        "units": [[u.char.id, round(u.total_damage_dealt, 6), round(u.current_hp, 6)]
                  for u in st.units],
        "cycles": st.cycles,
        "action_counts": dict(sorted(st.action_counts.items())),
    }


def _recommend_digest():
    rows = {}
    for p in _roster():
        cid = p.stem
        rolls = recommend_substats(load_character(cid, "data/characters"))
        total = sum(rolls.values())
        assert total == TOTAL_ROLLS, f"recommend total {total} != {TOTAL_ROLLS}: {cid}"
        rows[cid] = dict(sorted(rolls.items()))
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _solo_digest():
    """全名册 92 角色单人 300AV vs 全零抗木桩：每个角色的注册/AI/Hook 路径进锚点。"""
    rows = {}
    for p in _roster():
        cid = p.stem
        try:
            random.seed(0)
            st = simulate(
                [{"char": load_character(cid, "data/characters"), "position": 1}],
                _build_enemy("dummy", "木桩", 500000, 200, [], zero_res=True),
                max_av=300.0)
            rows[cid] = [round(sum(u.total_damage_dealt for u in st.units), 6),
                         st.cycles, sum(st.action_counts.values())]
        except Exception as e:  # 空壳/无技能角色可能异常——记入 digest 同样是行为锚点
            rows[cid] = ["ERR", type(e).__name__]
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build_snapshot():
    rec = json.loads((DATA / "recommendations.json").read_text(encoding="utf-8"))
    relic_sets = _load_relic_sets()
    return {
        "schema_version": SCHEMA_VERSION,
        "teams": {name: _run_team(spec, rec, relic_sets)
                  for name, spec in TEAMS.items()},
        "roster_count": len(_roster()),
        "recommend_digest": _recommend_digest(),
        "solo_digest": _solo_digest(),
    }


def _first_diffs(old, new, limit=12):
    out = []

    def walk(a, b, path):
        if len(out) >= limit:
            return
        if type(a) is not type(b):
            out.append(f"{path}: {a!r} -> {b!r}")
            return
        if isinstance(a, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k, "<absent>"), b.get(k, "<absent>"), f"{path}.{k}")
        elif isinstance(a, list):
            if len(a) != len(b):
                out.append(f"{path}: len {len(a)} -> {len(b)}")
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")
        elif a != b:
            out.append(f"{path}: {a!r} -> {b!r}")

    walk(old, new, "$")
    return out


def main():
    os.chdir(ROOT)
    update = "--update" in sys.argv
    snap = build_snapshot()

    if update:
        snap["generated_at"] = datetime.now().isoformat(timespec="seconds")
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"baseline updated -> {BASELINE}")
        for name, r in snap["teams"].items():
            print(f"  {name}: cycles={r['cycles']} "
                  f"total={round(sum(u[1] for u in r['units']), 2)}")
        return 0

    if not BASELINE.exists():
        print("baseline missing: run `python tests/smoke_baseline.py --update` once, "
              "commit the file, then always use --check")
        return 2
    old = json.loads(BASELINE.read_text(encoding="utf-8"))
    if old.get("schema_version") != SCHEMA_VERSION:
        print(f"schema_version mismatch: baseline {old.get('schema_version')} "
              f"vs current {SCHEMA_VERSION} — regenerate with --update (approved rounds only)")
        return 2
    failed = [k for k in ("teams", "roster_count", "recommend_digest", "solo_digest")
              if old.get(k) != snap[k]]
    if failed:
        print("BASELINE DRIFT in: " + ", ".join(failed))
        for line in _first_diffs({k: old.get(k) for k in failed},
                                 {k: snap[k] for k in failed}):
            print("  " + line)
        return 1
    print(f"baseline check OK: {len(snap['teams'])} teams + roster {snap['roster_count']} "
          f"(recommend/solo digests identical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
