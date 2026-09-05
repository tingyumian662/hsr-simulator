"""REST API — 数据列表 + 队伍模拟"""
import functools
import json, logging, os
from pathlib import Path

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Request, HTTPException
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Optional

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import LightCone, RelicPiece, RelicSet
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate
from engine.constants import (RELIC_MAIN_STAT_VALUES, SUB_STAT_VALUES, StatType,
                              SUBSTAT_ROLL_FACTOR, SUBSTAT_KEY_MAP, WEAKNESS_ELEMENTS)

router = APIRouter()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# v7.3: 主词条满级值/副词条中档值统一取自 engine.constants（此前三处硬编码副本）;
# 副词条键含效果命中（9 键契约, 项目主裁决）
# v7.17.0: 主词条双键收录（name 大写形 + value 原形）——前端与 recommendations.json 的
# 主词条键为 value 形（ATK_percent）而速度为 name 形（SPD_PERCENT）, 此前仅按 name 收录
# 使 ATK/HP/DEF% 主词条数值恒取 0（本轮键集契约测试发现的既有缺陷, 修复;
# 同引擎"SPD_percent 与 SPD_PERCENT 兼容"不变量）
MAIN_VALUES = {}
for _pool in RELIC_MAIN_STAT_VALUES.values():
    for _st, _val in _pool.items():
        MAIN_VALUES.setdefault(_st.name, _val)
        MAIN_VALUES.setdefault(_st.value, _val)

# v7.5: 副词条中档值 ×SUBSTAT_ROLL_FACTOR（与推荐器 _mid 同口径）
SUB_MID_VALUES = {st.value: vals[1] * SUBSTAT_ROLL_FACTOR
                  for st, vals in SUB_STAT_VALUES.items()}  # 引擎内部键（SPD_percent 小写 p）

# 前端副词条键 → 引擎内部键（仅速度大小写差异）——v7.17.0 M7 键集单源:
# 定义唯一在 engine.constants.SUBSTAT_KEY_MAP（FRONTEND_ROLL_KEYS 序）

def _main_stat_of(key):
    """name 形（ATK_PERCENT/SPD_PERCENT）或 value 形（ATK_percent/SPD_percent）键 → StatType
    成员; 无法识别时原样返回（与旧 getattr(..., k, k) 回退一致）。"""
    try:
        return StatType(key)
    except ValueError:
        return getattr(StatType, key, key)


# 主词条类型映射（v7.3 口径；M2 提升为模块级供 preview/simulate 共用）
MAIN_STAT_TYPE = {k: _main_stat_of(k) for k in MAIN_VALUES}


def _build_relic_pieces(cfg: dict, sub_rolls: dict) -> list:
    """preview/simulate 共用：副词条键换算 + 六件 RelicPiece 构建。

    v7.3: 前端键→引擎键统一走 SUBSTAT_KEY_MAP（含效果命中）; 中档值取 constants。
    M2 收敛：此前两端点各持一份逐字相同的副本。"""
    sub_per_piece = {}
    for k, v in sub_rolls.items():
        if v > 0:
            ek = SUBSTAT_KEY_MAP.get(k, k)
            sub_per_piece[ek] = round(v * SUB_MID_VALUES.get(ek, 2.5) / 6.0, 2)

    set4, set2 = cfg.get("set4", ""), cfg.get("set2", "")
    pieces = []
    for slot, sn, mt, mv in [
        ("head", set4, StatType.HP_FLAT, 705),
        ("hands", set4, StatType.ATK_FLAT, 352),
        ("body", set4, MAIN_STAT_TYPE.get(cfg.get("body", ""), ""),
         MAIN_VALUES.get(cfg.get("body", ""), 0)),
        ("feet", set4, MAIN_STAT_TYPE.get(cfg.get("feet", ""), ""),
         MAIN_VALUES.get(cfg.get("feet", ""), 0)),
        ("planar_sphere", set2, MAIN_STAT_TYPE.get(cfg.get("sphere", ""), ""),
         MAIN_VALUES.get(cfg.get("sphere", ""), 0)),
        ("link_rope", set2, MAIN_STAT_TYPE.get(cfg.get("rope", ""), ""),
         MAIN_VALUES.get(cfg.get("rope", ""), 0)),
    ]:
        pieces.append(RelicPiece(slot=slot, set_name=sn, main_stat_type=mt,
                                 main_stat_value=mv, sub_stats=sub_per_piece))
    return pieces

# 生成 ID 映射（文件名→中文名）
def _build_file_index(subdir):
    """列出目录中所有非_开头的JSON文件"""
    d = DATA_DIR / subdir
    if not d.exists():
        return []
    return sorted([f for f in os.listdir(d) if f.endswith('.json') and not f.startswith('_')])


# ═══ v7.19.1: 进程内数据缓存（web 层专用, 引擎零改动）═══
# _load_json_cached 按 (path, mtime_ns, size) 键控原始 dict——文件编辑即失效;
# 派生表（光锥索引/套装表/角色对象）lru_cache 按目录签名缓存, 目录内增删改文件即重建。
# 共享安全依据（勘察 2026-09-05）: api/引擎全链路对 Character/RelicSet 只读,
# simulate() 第一行 deepcopy(configs) 自带隔离。

_JSON_CACHE: dict = {}


def _load_json_cached(path) -> dict:
    p = str(path)
    st = os.stat(p)
    key = (p, st.st_mtime_ns, st.st_size)
    val = _JSON_CACHE.get(key)
    if val is None:
        with open(p, encoding="utf-8") as fh:
            val = json.load(fh)
        for stale in [k for k in _JSON_CACHE if k[0] == p and k != key]:
            del _JSON_CACHE[stale]
        _JSON_CACHE[key] = val
    return val


def _dir_sig(subdir) -> tuple:
    """目录签名 = sorted (文件名, mtime_ns)——派生表缓存键"""
    d = DATA_DIR / subdir
    if not d.exists():
        return ()
    return tuple((f, (d / f).stat().st_mtime_ns) for f in _build_file_index(subdir))


@functools.lru_cache(maxsize=4)
def _lightcone_index(sig) -> dict:
    """Map canonical JSON ids and legacy filename stems to trusted data files."""
    index = {}
    for filename, _mtime in sig:
        data = _load_json_cached(DATA_DIR / "light_cones" / filename)
        stem = Path(filename).stem
        index[data.get("id", stem)] = filename
        index.setdefault(stem, filename)
    return index


@functools.lru_cache(maxsize=512)
def _lightcone_obj(lightcone_id: str, sig, rank: int = 0):
    index = _lightcone_index(sig)
    if lightcone_id not in index:
        return None
    lc = LightCone.from_json(str(DATA_DIR / "light_cones" / index[lightcone_id]))
    if rank:  # v7.20.0: 显式叠影 1-5（0=JSON 默认档）; 工厂内覆写后再缓存, 防共享对象串档
        lc.rank = rank
    return lc


# v7.20.0 档位审计（批0）: 无 values 数组但引擎按 rank 算术缩放的光锥
_RANK_SCALED_NO_VALUES = {"resolution_shines_as_pearls_of_sweat"}


def _lc_rank_info(lc_raw: dict) -> dict:
    """v7.20.0: 光锥叠影数据面——scaled=数值随叠影档缩放; default=选择时的默认档。

    - scaled（values 五档或 rank 算术 handler）: 默认按稀有度（4★→5, 5★→1, 项目主规则）;
    - 单档: 默认=JSON 顶层 rank 字段（录入校准档——抓取批量 4★=5/抽卡5★=1/商店赠送5★=5,
      例外 landaus_choice=1; 数值不随叠影缩放, 所见即所算）。"""
    scaled = (any(e.get("values") for e in lc_raw.get("effects", []))
              or lc_raw.get("id") in _RANK_SCALED_NO_VALUES)
    if scaled:
        default = 1 if lc_raw.get("rarity", 5) >= 5 else 5
    else:
        default = int(lc_raw.get("rank", 1) or 1)
    return {"rank_scaled": scaled, "default_rank": default}


@functools.lru_cache(maxsize=512)
def _character_obj(char_id: str, sig):
    return load_character(char_id)


@functools.lru_cache(maxsize=4)
def _relic_sets(sig) -> dict:
    sets = {}
    for filename, _mtime in sig:
        try:
            rs = RelicSet.from_json(str(DATA_DIR / "relics" / filename))
            sets[rs.name] = rs
        except Exception:
            pass  # 跳过损坏的遗器文件
    return sets


def _load_character_or_404(char_id: str):
    """从角色数据索引加载请求角色，并规范化不存在时的响应。"""
    try:
        return _character_obj(char_id, _dir_sig("characters"))
    except Exception:
        raise HTTPException(status_code=404, detail=f"角色 {char_id} 不存在")


def _load_lightcone_or_422(lightcone_id: Optional[str], rank: Optional[int] = None):
    """从光锥数据索引加载请求光锥，避免用户 ID 参与路径解析。

    rank: v7.20.0 叠影档（1-5）; None=JSON 默认档（不传字段零行为变化）。"""
    if not lightcone_id:
        return None
    lc = _lightcone_obj(lightcone_id, _dir_sig("light_cones"), rank or 0)
    if lc is None:
        raise HTTPException(status_code=422, detail=f"光锥 {lightcone_id} 不存在")
    return lc


# ==== API ====

@router.get("/list")
async def list_data():
    """返回所有可选角色/光锥/遗器"""
    chars = []
    for f in _build_file_index("characters"):
        fp = DATA_DIR / "characters" / f
        c = _load_json_cached(fp)
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
        lc = _load_json_cached(fp)
        lcs.append({
            "id": lc.get("id", f.replace(".json", "")),
            "name": lc["name"], "path": lc["path"], "rarity": lc.get("rarity", 5),
            # v5.2 问题7: 未建模效果数（unsupported 保持常驻近似）
            "unsupported_effects": sum(
                1 for e in lc.get("effects", [])
                if e.get("condition_code") == "unsupported"),
            # v7.20.0: 叠影数据面（档位审计批0）——前端默认档与"单档"徽标的数据源
            **_lc_rank_info(lc),
        })

    relic_files = _build_file_index("relics")
    outer_relics = []  # 外圈（有四件套效果）
    inner_relics = []  # 内圈（仅二件套）
    for f in relic_files:
        fp = DATA_DIR / "relics" / f
        r = _load_json_cached(fp)
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
            recommendations = _load_json_cached(rec_fp)
        except (json.JSONDecodeError, OSError):
            recommendations = {}

    return {"characters": chars, "light_cones": lcs, "outer_relics": outer_relics,
            "inner_relics": inner_relics, "recommendations": recommendations}


# v7.17.0 M7 键集单源: 前端键集由本端点统一提供, app.js 不再内联键字面量。
# 副词条: 键序与引擎键映射来自 engine.constants（SUBSTAT_KEY_MAP）;
# 输入短键/短标签是纯 web 层概念, 单源在本表（缺契约键即 KeyError, 快速失败）。
_SUBSTAT_FIELD_LABEL = {
    "CRIT_RATE": ("cr", "暴击"), "CRIT_DMG": ("cd", "暴伤"),
    "ATK_percent": ("atk", "攻击%"), "SPD_PERCENT": ("spd", "速度"),
    "HP_percent": ("hp", "生命%"), "EFFECT_RES": ("res", "抵抗"),
    "DEF_percent": ("def", "防御%"), "BREAK_EFFECT": ("be", "击破"),
    "EFFECT_HIT_RATE": ("ehr", "命中"),
}

# 四槽主词条 options: 顺序/标签/默认选中逐字承袭原内联 HTML
# （sphere 无默认 → 首项量子增伤即隐式默认; feet 首项空 "--"）——勿按引擎池序重排。
_MAIN_STAT_OPTIONS = {
    "body": [("CRIT_RATE", "暴击率", False), ("CRIT_DMG", "暴伤", True),
             ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
             ("DEF_percent", "防御%", False), ("HEAL_BONUS", "治疗加成", False),
             ("EFFECT_HIT_RATE", "效果命中", False)],
    "feet": [("", "--", False), ("SPD_PERCENT", "速度", False),
             ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
             ("DEF_percent", "防御%", False)],
    "sphere": [("DMG_BONUS_QUANTUM", "量子增伤", False), ("DMG_BONUS_PHYSICAL", "物理增伤", False),
               ("DMG_BONUS_FIRE", "火增伤", False), ("DMG_BONUS_ICE", "冰增伤", False),
               ("DMG_BONUS_LIGHTNING", "雷增伤", False), ("DMG_BONUS_WIND", "风增伤", False),
               ("DMG_BONUS_IMAGINARY", "虚数增伤", False),
               ("ATK_percent", "攻击%", False), ("HP_percent", "生命%", False),
               ("DEF_percent", "防御%", False)],
    "rope": [("ATK_percent", "攻击%", False), ("ENERGY_REGEN", "充能", True),
             ("HP_percent", "生命%", False), ("DEF_percent", "防御%", False),
             ("BREAK_EFFECT", "击破特攻", False)],
}


@router.get("/keysets")
async def keysets():
    """前端键集单源: 副词条 9 键契约（键/引擎键/输入短键/短标签）+ 四槽主词条 options。"""
    substats = [{"key": k, "engine_key": v,
                 "field": _SUBSTAT_FIELD_LABEL[k][0], "label": _SUBSTAT_FIELD_LABEL[k][1]}
                for k, v in SUBSTAT_KEY_MAP.items()]
    main_stats = {slot: [{"value": val, "label": label, "selected": selected}
                         for val, label, selected in options]
                  for slot, options in _MAIN_STAT_OPTIONS.items()}
    return {"substats": substats, "main_stats": main_stats}


# v5.2 问题6: 嵌套请求模型与数值范围校验（校验失败自动 422）


class TeamMember(BaseModel):
    char_id: str = Field(min_length=1, max_length=64)
    lc_id: Optional[str] = None
    # v7.20.0: 光锥叠影档 1-5; None=JSON 默认档（不传字段零行为变化, smoke 口径不变）
    lc_rank: Optional[int] = Field(None, ge=1, le=5)
    eidolon: int = Field(0, ge=0, le=6)
    relics: dict = {}
    substats: dict = {}
    # v7.3.1（项目主纠正）: 总词条固定 50; 可调值为有效词条数（默认 30, 上限 50=无非有效词条）
    effective_rolls: int = Field(30, ge=1, le=50)


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
    relic_sets = _relic_sets(_dir_sig("relics"))  # v7.19.1: 目录签名缓存表（损坏文件容错在缓存层）

    results = []
    # v7.19.1: 角色单轮加载复用（此前 team_paths 与成员循环各加载一次, 双读磁盘）
    chars = [_load_character_or_404(m.char_id) for m in req.team]
    team_paths = [c.path for c in chars]  # v7.19.0 队伍命途上下文（组队条件型常驻被动, 口径含自身）
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = chars[i]
        lc = _load_lightcone_or_422(member.lc_id, member.lc_rank)

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

        effective_rolls = member.effective_rolls

        # 统一推荐入口：技能结构定位 + SPD阈值约束 + 边际效益贪心
        # v6.11 阶段2 + v7.3.1: 完整响应含 weights/constraints/graduation（有效词条口径）
        from engine.core.relic_optimizer import recommend_substats_full
        try:
            full = recommend_substats_full(char, lc, pieces, relic_sets,
                                           effective_rolls, team_paths=team_paths)
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
    relic_sets = _relic_sets(_dir_sig("relics"))  # v7.19.1: 目录签名缓存表（损坏文件容错在缓存层）

    results = []
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = _load_character_or_404(cid)
        lc = _load_lightcone_or_422(member.lc_id, member.lc_rank)

        try:
            pieces = _build_relic_pieces(member.relics, member.substats)
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
            "BE": round(stats.BREAK_EFFECT * 100, 1), "EHR": round(stats.EFFECT_HIT_RATE * 100, 1),
        })

    return {"previews": results}


@router.post("/simulate")
async def run_simulation(req: SimRequest):
    """运行队伍模拟"""
    # 加载遗器套装
    relic_sets = _relic_sets(_dir_sig("relics"))  # v7.19.1: 目录签名缓存表（损坏文件容错在缓存层）

    configs = []
    for i, member in enumerate(req.team):
        cid = member.char_id
        char = _load_character_or_404(cid)
        lc = _load_lightcone_or_422(member.lc_id, member.lc_rank)

        # 遗器
        pieces = _build_relic_pieces(member.relics, member.substats)

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
