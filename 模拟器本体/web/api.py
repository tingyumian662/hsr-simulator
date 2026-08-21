"""REST API — 数据列表 + 队伍模拟"""
import json, logging, os
from pathlib import Path

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Optional

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, RelicPiece, RelicSet
from engine.core.attributes import compute_combat_stats
from engine.core.combat_sim import simulate

router = APIRouter()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 生成 ID 映射（文件名→中文名）
def _build_file_index(subdir):
    """列出目录中所有非_开头的JSON文件"""
    d = DATA_DIR / subdir
    if not d.exists():
        return []
    return sorted([f for f in os.listdir(d) if f.endswith('.json') and not f.startswith('_')])


def _build_lightcone_index():
    """Map canonical JSON ids and legacy filename stems to trusted data files."""
    index = {}
    for filename in _build_file_index("light_cones"):
        path = DATA_DIR / "light_cones" / filename
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        stem = path.stem
        index[data.get("id", stem)] = path
        index.setdefault(stem, path)
    return index


def _load_character_or_404(char_id: str):
    """从角色数据索引加载请求角色，并规范化不存在时的响应。"""
    try:
        return load_character(char_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"角色 {char_id} 不存在")


def _load_lightcone_or_422(lightcone_id: Optional[str]):
    """从光锥数据索引加载请求光锥，避免用户 ID 参与路径解析。"""
    if not lightcone_id:
        return None
    index = _build_lightcone_index()
    if lightcone_id not in index:
        raise HTTPException(status_code=422, detail=f"光锥 {lightcone_id} 不存在")
    return LightCone.from_json(str(index[lightcone_id]))


# ==== API ====

@router.get("/list")
async def list_data():
    """返回所有可选角色/光锥/遗器"""
    chars = []
    for f in _build_file_index("characters"):
        fp = DATA_DIR / "characters" / f
        with open(fp, encoding='utf-8') as fh:
            c = json.load(fh)
        chars.append({
            "id": c.get("id", f.replace(".json", "")),
            "name": c["name"], "element": c["element"], "path": c["path"],
            "rarity": c.get("rarity", 5), "max_energy": c.get("max_energy", 0),
            # v5.2 问题7: 完成度暴露（full=有技能数据, shell=空壳）
            "completeness": "full" if c.get("skills") else "shell",
        })

    lcs = []
    for f in _build_file_index("light_cones"):
        fp = DATA_DIR / "light_cones" / f
        with open(fp, encoding='utf-8') as fh:
            lc = json.load(fh)
        lcs.append({
            "id": lc.get("id", f.replace(".json", "")),
            "name": lc["name"], "path": lc["path"], "rarity": lc.get("rarity", 5),
            # v5.2 问题7: 未建模效果数（unsupported 保持常驻近似）
            "unsupported_effects": sum(
                1 for e in lc.get("effects", [])
                if e.get("condition_code") == "unsupported"),
        })

    relic_files = _build_file_index("relics")
    outer_relics = []  # 外圈（有四件套效果）
    inner_relics = []  # 内圈（仅二件套）
    for f in relic_files:
        fp = DATA_DIR / "relics" / f
        with open(fp, encoding='utf-8') as fh:
            r = json.load(fh)
        has_4pc = any(e.get("pieces_required", 0) == 4 for e in r.get("effects", []))
        entry = {"name": r["name"]}
        if has_4pc:
            outer_relics.append(entry)
        else:
            inner_relics.append(entry)

    # v6.8: 角色推荐装备映射（专属光锥/遗器套装/主词条; 缺失文件→空 dict 容错）
    recommendations = {}
    rec_fp = DATA_DIR / "recommendations.json"
    if rec_fp.exists():
        try:
            with open(rec_fp, encoding='utf-8') as fh:
                recommendations = json.load(fh)
        except (json.JSONDecodeError, OSError):
            recommendations = {}

    return {"characters": chars, "light_cones": lcs, "outer_relics": outer_relics,
            "inner_relics": inner_relics, "recommendations": recommendations}


# v5.2 问题6: 嵌套请求模型与数值范围校验（校验失败自动 422）
WEAKNESS_ELEMENTS = {"物理", "火", "冰", "雷", "风", "量子", "虚数"}


class TeamMember(BaseModel):
    char_id: str = Field(min_length=1, max_length=64)
    lc_id: Optional[str] = None
    eidolon: int = Field(0, ge=0, le=6)
    relics: dict = {}
    substats: dict = {}
    total_rolls: int = Field(30, ge=1, le=50)


class EnemyConfig(BaseModel):
    name: str = "敌人"                # v6.5: 个体名称（异构列表用）
    elite: bool = False                # v6.5: 精英=每回合双动（actions_per_turn=2）
    hp: float = Field(50000, ge=0)
    atk: float = Field(500, ge=0)
    def_: float = Field(800, ge=0, alias="def")
    toughness: float = Field(20, ge=0)
    spd: float = Field(80, ge=0)
    effect_res: float = Field(0.0, ge=0, le=1)
    count: int = Field(1, ge=1, le=10)
    weakness: list[str] = []
    attacks: list = []

    @property
    def def_val(self) -> float:
        return self.def_


class SimRequest(BaseModel):
    team: list[TeamMember] = Field(min_length=1, max_length=8)
    enemy: EnemyConfig = Field(default_factory=EnemyConfig)
    enemies: list[EnemyConfig] = []   # v6.5: 异构敌人列表（非空时逐只独立配置, 覆盖 enemy.count）
    max_av: float = Field(1000.0, gt=0)


@router.post("/recommend")
async def recommend_substats(req: SimRequest):
    """根据角色当前配装推荐最优副词条分配"""
    from engine.constants import StatType

    MAIN_VALUES = {
        "CRIT_RATE": 32.4, "CRIT_DMG": 64.8, "ATK_percent": 43.2, "HP_percent": 43.2,
        "DEF_percent": 54.0, "SPD_PERCENT": 25.0, "BREAK_EFFECT": 64.8, "ENERGY_REGEN": 19.4,
        "HEAL_BONUS": 34.5, "EFFECT_HIT_RATE": 43.2,
        "DMG_BONUS_PHYSICAL": 38.8, "DMG_BONUS_FIRE": 38.8, "DMG_BONUS_ICE": 38.8,
        "DMG_BONUS_LIGHTNING": 38.8, "DMG_BONUS_WIND": 38.8, "DMG_BONUS_QUANTUM": 38.8,
        "DMG_BONUS_IMAGINARY": 38.8,
    }
    MAIN_STAT_TYPE = {k: getattr(StatType, k, k) for k in MAIN_VALUES}

    relic_sets = {}
    for f in _build_file_index("relics"):
        try:
            rs = RelicSet.from_json(str(DATA_DIR / "relics" / f))
            relic_sets[rs.name] = rs
        except Exception:
            pass  # 跳过损坏的遗器文件

    results = []
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = _load_character_or_404(cid)
        lc = _load_lightcone_or_422(member.lc_id)

        cfg = member.relics
        set4, set2 = cfg.get("set4", ""), cfg.get("set2", "")
        body_type = MAIN_STAT_TYPE.get(cfg.get("body", ""), "")
        feet_type = MAIN_STAT_TYPE.get(cfg.get("feet", ""), "")
        sphere_type = MAIN_STAT_TYPE.get(cfg.get("sphere", ""), "")
        rope_type = MAIN_STAT_TYPE.get(cfg.get("rope", ""), "")

        # 构建基础遗器（无副词条）
        pieces = []
        for slot, sn, mt, mv in [
            ("head", set4, StatType.HP_FLAT, 705),
            ("hands", set4, StatType.ATK_FLAT, 352),
            ("body", set4, body_type, MAIN_VALUES.get(cfg.get("body", ""), 0)),
            ("feet", set4, feet_type, MAIN_VALUES.get(cfg.get("feet", ""), 0)),
            ("planar_sphere", set2, sphere_type, MAIN_VALUES.get(cfg.get("sphere", ""), 0)),
            ("link_rope", set2, rope_type, MAIN_VALUES.get(cfg.get("rope", ""), 0)),
        ]:
            pieces.append(RelicPiece(slot=slot, set_name=sn, main_stat_type=mt, main_stat_value=mv))

        total_rolls = member.total_rolls

        # 统一推荐入口：技能结构定位 + SPD阈值约束 + 边际效益贪心
        # v6.11 阶段2: 完整响应含 weights/constraints/graduation（旧字段保持兼容）
        from engine.core.relic_optimizer import recommend_substats_full
        try:
            full = recommend_substats_full(char, lc, pieces, relic_sets, total_rolls)
        except Exception as e:
            # v5.5: 记录日志防静默失败（此前 except: continue 导致角色从推荐中消失无痕迹）
            logger.exception('recommend_substats 失败: char=%s err=%s', char.name, e)
            raise HTTPException(status_code=500, detail="recommendation failed") from e

        results.append({
            "char_name": char.name,
            "rolls": full["rolls"],
            "total": sum(full["rolls"].values()),
            "weights": full["weights"],
            "constraints": full["constraints"],
            "graduation": full["graduation"],
        })

    return {"recommendations": results}


@router.post("/preview")
async def preview_stats(req: SimRequest):
    """预览角色面板（含副词条）"""
    from engine.constants import StatType

    MAIN_VALUES = {
        "CRIT_RATE": 32.4, "CRIT_DMG": 64.8, "ATK_percent": 43.2, "HP_percent": 43.2,
        "DEF_percent": 54.0, "SPD_PERCENT": 25.0, "BREAK_EFFECT": 64.8, "ENERGY_REGEN": 19.4,
        "HEAL_BONUS": 34.5, "EFFECT_HIT_RATE": 43.2,
        "DMG_BONUS_PHYSICAL": 38.8, "DMG_BONUS_FIRE": 38.8, "DMG_BONUS_ICE": 38.8,
        "DMG_BONUS_LIGHTNING": 38.8, "DMG_BONUS_WIND": 38.8, "DMG_BONUS_QUANTUM": 38.8,
        "DMG_BONUS_IMAGINARY": 38.8,
    }
    MAIN_STAT_TYPE = {k: getattr(StatType, k, k) for k in MAIN_VALUES}
    mid_val = {"CRIT_RATE": 3.0, "CRIT_DMG": 5.8, "ATK_percent": 2.5, "HP_percent": 2.5,
               "DEF_percent": 3.1, "SPD_PERCENT": 3.0, "EFFECT_RES": 2.5, "BREAK_EFFECT": 4.8}

    relic_sets = {}
    for f in _build_file_index("relics"):
        try:
            rs = RelicSet.from_json(str(DATA_DIR / "relics" / f))
            relic_sets[rs.name] = rs
        except Exception:
            pass  # 跳过损坏的遗器文件

    results = []
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = _load_character_or_404(cid)
        lc = _load_lightcone_or_422(member.lc_id)

        try:
            cfg = member.relics
            body_type = MAIN_STAT_TYPE.get(cfg.get("body", ""), "")
            feet_type = MAIN_STAT_TYPE.get(cfg.get("feet", ""), "")
            sphere_type = MAIN_STAT_TYPE.get(cfg.get("sphere", ""), "")
            rope_type = MAIN_STAT_TYPE.get(cfg.get("rope", ""), "")

            sub_rolls = member.substats
            # 映射前端大写key→引擎小写key
            KEY_MAP = {"CRIT_RATE":"CRIT_RATE","CRIT_DMG":"CRIT_DMG","ATK_percent":"ATK_percent",
                       "HP_percent":"HP_percent","DEF_percent":"DEF_percent","SPD_PERCENT":"SPD_percent",
                       "EFFECT_RES":"EFFECT_RES","BREAK_EFFECT":"BREAK_EFFECT"}
            sub_per_piece = {}
            for k, v in sub_rolls.items():
                if v > 0:
                    ek = KEY_MAP.get(k, k)
                    sub_per_piece[ek] = round(v * mid_val.get(k, 2.5) / 6.0, 2)

            pieces = []
            for slot, sn, mt, mv in [
                ("head", cfg.get("set4", ""), StatType.HP_FLAT, 705),
                ("hands", cfg.get("set4", ""), StatType.ATK_FLAT, 352),
                ("body", cfg.get("set4", ""), body_type, MAIN_VALUES.get(cfg.get("body", ""), 0)),
                ("feet", cfg.get("set4", ""), feet_type, MAIN_VALUES.get(cfg.get("feet", ""), 0)),
                ("planar_sphere", cfg.get("set2", ""), sphere_type, MAIN_VALUES.get(cfg.get("sphere", ""), 0)),
                ("link_rope", cfg.get("set2", ""), rope_type, MAIN_VALUES.get(cfg.get("rope", ""), 0)),
            ]:
                pieces.append(RelicPiece(slot=slot, set_name=sn, main_stat_type=mt, main_stat_value=mv, sub_stats=sub_per_piece))

            stats = compute_combat_stats(char, lc, pieces, relic_sets)
        except Exception:
            logger.exception('preview 计算失败: char=%s', cid)
            continue
        results.append({
            "name": char.name,
            "HP": round(stats.HP), "ATK": round(stats.ATK), "DEF": round(stats.DEF),
            "SPD": round(stats.SPD, 1),
            "CR": round(stats.CRIT_RATE * 100, 1), "CD": round(stats.CRIT_DMG * 100, 1),
            "RES": round(stats.EFFECT_RES * 100, 1), "ERR": round(stats.ENERGY_REGEN * 100, 1),
            "BE": round(stats.BREAK_EFFECT * 100, 1),
        })

    return {"previews": results}


@router.post("/simulate")
async def run_simulation(req: SimRequest):
    """运行队伍模拟"""
    from engine.constants import StatType

    # 主词条数值
    MAIN_VALUES = {
        "CRIT_RATE": 32.4, "CRIT_DMG": 64.8, "ATK_percent": 43.2, "HP_percent": 43.2,
        "DEF_percent": 54.0, "SPD_PERCENT": 25.0, "BREAK_EFFECT": 64.8, "ENERGY_REGEN": 19.4,
        "HEAL_BONUS": 34.5, "EFFECT_HIT_RATE": 43.2,
        "DMG_BONUS_PHYSICAL": 38.8, "DMG_BONUS_FIRE": 38.8, "DMG_BONUS_ICE": 38.8,
        "DMG_BONUS_LIGHTNING": 38.8, "DMG_BONUS_WIND": 38.8, "DMG_BONUS_QUANTUM": 38.8,
        "DMG_BONUS_IMAGINARY": 38.8,
    }
    MAIN_STAT_TYPE = {k: getattr(StatType, k, k) for k in MAIN_VALUES}

    # 加载遗器套装
    relic_sets = {}
    for f in _build_file_index("relics"):
        try:
            rs = RelicSet.from_json(str(DATA_DIR / "relics" / f))
            relic_sets[rs.name] = rs
        except Exception:
            pass  # 跳过损坏的遗器文件

    configs = []
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = _load_character_or_404(cid)
        lc = _load_lightcone_or_422(member.lc_id)

        # 遗器
        cfg = member.relics
        set4 = cfg.get("set4", "")
        set2 = cfg.get("set2", "")
        body_type = MAIN_STAT_TYPE.get(cfg.get("body", ""), "")
        feet_type = MAIN_STAT_TYPE.get(cfg.get("feet", ""), "")
        sphere_type = MAIN_STAT_TYPE.get(cfg.get("sphere", ""), "")
        rope_type = MAIN_STAT_TYPE.get(cfg.get("rope", ""), "")

        sub_rolls = member.substats
        mid_val = {"CRIT_RATE": 3.0, "CRIT_DMG": 5.8, "ATK_percent": 2.5, "HP_percent": 2.5,
                   "DEF_percent": 3.1, "SPD_PERCENT": 3.0, "EFFECT_RES": 2.5, "BREAK_EFFECT": 4.8}
        KEY_MAP = {"CRIT_RATE":"CRIT_RATE","CRIT_DMG":"CRIT_DMG","ATK_percent":"ATK_percent",
                   "HP_percent":"HP_percent","DEF_percent":"DEF_percent","SPD_PERCENT":"SPD_percent",
                   "EFFECT_RES":"EFFECT_RES","BREAK_EFFECT":"BREAK_EFFECT"}
        sub_per_piece = {}
        for k, v in sub_rolls.items():
            if v > 0:
                ek = KEY_MAP.get(k, k)
                sub_per_piece[ek] = round(v * mid_val.get(k, 2.5) / 6.0, 2)

        pieces = []
        for slot, sn, mt, mv in [
            ("head", set4, StatType.HP_FLAT, 705),
            ("hands", set4, StatType.ATK_FLAT, 352),
            ("body", set4, body_type, MAIN_VALUES.get(cfg.get("body", ""), 0)),
            ("feet", set4, feet_type, MAIN_VALUES.get(cfg.get("feet", ""), 0)),
            ("planar_sphere", set2, sphere_type, MAIN_VALUES.get(cfg.get("sphere", ""), 0)),
            ("link_rope", set2, rope_type, MAIN_VALUES.get(cfg.get("rope", ""), 0)),
        ]:
            pieces.append(RelicPiece(slot=slot, set_name=sn, main_stat_type=mt, main_stat_value=mv, sub_stats=sub_per_piece))

        eidolon = member.eidolon
        configs.append({
            "char": char, "lightcone": lc, "relics": pieces, "relic_sets": relic_sets,
            "position": i + 1, "eidolon": eidolon,
        })

    # 敌方攻击数据（默认单目标普攻; 可通过请求覆盖 spd/atk/attacks）
    default_attacks = [{"name": "挥击", "element": "物理", "damage_type": "direct",
                        "multiplier": 100.0, "target_type": "single_enemy", "priority": 0}]

    def _build_enemy(ecfg: EnemyConfig) -> Enemy:
        # 敌人（v5.2 问题6: weakness 白名单过滤非法元素）
        weakness = [w for w in ecfg.weakness if w in WEAKNESS_ELEMENTS]
        res = {}
        for elem in ["物理", "火", "冰", "雷", "风", "量子", "虚数"]:
            res[elem] = 0.0 if elem in weakness else 0.20
        return Enemy(
            id="battle", name=ecfg.name, level=80,
            HP=ecfg.hp, ATK=ecfg.atk, DEF=ecfg.def_val,
            SPD=ecfg.spd,
            toughness=ecfg.toughness, max_toughness=ecfg.toughness,
            effect_res=ecfg.effect_res,
            element_res=res,
            attacks=ecfg.attacks or default_attacks,
            actions_per_turn=2 if ecfg.elite else 1,  # v6.5: 精英双动
        )

    # v6.5: 异构敌人列表（每只独立配置 精英/弱点/HP/韧性）; 空则回退单模板×count 旧契约
    if req.enemies:
        enemy_templates = [_build_enemy(ecfg) for ecfg in req.enemies]
        enemy = enemy_templates[0]
        enemy_count = len(enemy_templates)
    else:
        enemy_templates = None
        enemy = _build_enemy(req.enemy)
        enemy_count = req.enemy.count

    try:
        state = await run_in_threadpool(
            simulate, configs, enemy, req.max_av, enemy_count, enemy_templates
        )
    except Exception as e:
        # v5.2 问题6: 堆栈只写服务端日志, 响应不含内部信息（原 200+trace 泄露）
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="模拟失败, 请检查输入配置")

    total = sum(u.total_damage_dealt for u in state.units)
    summary = []
    for u in state.units:
        summary.append({
            "name": u.char.name,
            "damage": round(u.total_damage_dealt),
            "pct": round(u.total_damage_dealt / total * 100, 1) if total > 0 else 0,
            "hp": round(u.current_hp),
            "alive": u.is_alive,
        })

    return {
        "log": state.log,
        "summary": summary,
        "total_damage": round(total),
        "turns": state.turn_count,
        "cycles": getattr(state, 'cycles', 0),  # v5.0 P8 轮次统计
        "action_counts": dict(getattr(state, 'action_counts', {})),
        "enemy_status": [
            {"id": e.id, "hp": round(e.HP), "alive": e.HP > 0, "broken": e.is_broken}
            for e in state.enemies
        ],
        "_debug": {
            "char_ids": [cfg["char"].id for cfg in configs],
            "team_has_remembrance": any(cfg["char"].path == "记忆" for cfg in configs),
            "max_av": req.max_av,
            "num_enemies": enemy_count,
        },
    }
