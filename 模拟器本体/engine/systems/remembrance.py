"""记忆命途子系统 — 忆灵召唤/控制/消失 + 忆质/至暗之谜管理

仅在队伍包含记忆命途角色时由 simulate() 条件激活。
"""
import copy
import random
from dataclasses import dataclass, field
from engine.models.memsprite import MemSprite
from engine.core.damage import calculate_damage
from engine.hooks.base import HookRegistry


def _enemy_for_damage(enemy):
    """Use combat_sim's shared dynamic vulnerability view without import cycles."""
    from engine.core.combat_sim import _enemy_for_damage as helper
    return helper(enemy)

AV_PER_TURN = 10000.0
ENERGY_GAIN = {"basic_attack": 20, "skill": 30, "ultimate": 5}


def _gain_yizhi(state, u, amt):
    """忆质获得统一入口（v5.1: 长夜月E2 每获得忆质额外+2）
    v5.7: 跨越≥16时触发天赋——解除自身控制 + 「长夜」立即行动（迷梦后可再次触发）"""
    old = u.yizhi
    if u.eidolon_rank >= 2:
        amt += 2
    u.yizhi += amt
    if u.char.id == 'changyeyue' and old < 16 <= u.yizhi:
        # 解除自身控制类负面状态
        removed = [s for s in u.statuses if getattr(s, 'category', '') == 'control']
        for s in removed:
            u.statuses.remove(s)
            state.log.append(f'  忆质≥16: {u.char.name}解除{s.name}')
        # 「长夜」立即行动（每轮一次; 迷梦释放后长夜消失, 重新召唤时复位）
        if u.memsprite_unit and u.memsprite_unit.is_alive \
                and not u.extra.get('cy_immediate_done') \
                and not state.extra.get('_ms_acting'):
            rem = state.extra.get('_rem_sys')
            if rem:
                u.extra['cy_immediate_done'] = True
                rem._force_memsprite_action(state, u, u.memsprite_unit)
                state.log.append('  忆质≥16: 「长夜」立即行动!')
    return amt


def _team_memsprite_def_pen(state) -> float:
    """Resolve the strongest active team-wide memsprite DEF penetration effect."""
    from engine.core.combat_sim import _build_effective_stats

    result = 0.0
    for owner in state.units:
        lc = getattr(owner, 'lightcone', None)
        if not owner.is_alive or not lc or lc.path != owner.char.path:
            continue
        if not any(getattr(effect, 'target', '') == 'all_allies'
                   and 'DEF_PEN_MEMSPRITE' in (effect.attributes or {})
                   for effect in lc.effects):
            continue
        result = max(result, _build_effective_stats(owner, state).DEF_PEN_MEMSPRITE)
    return result


# ════════ 献予诗系统（黄金裔史诗）════════
# 需求源: 角色技能介绍/昔涟.txt:86-99 + xilian.json _gold_offspring_effects
# 分发: 德谬歌"此诗，献予一切生命"对黄金裔目标触发专属诗; 空壳角色占位等录入

POEM_NAMES = {
    "trailblazer_remembrance": "创世", "aglaea": "浪漫", "mydei": "纷争",
    "xiadie": "生死", "fengjin": "天空", "changyeyue": "岁月",
    "tribbie": "门径", "anaxa": "理性", "cipher": "诡计",
    "phainon": "负世", "hysilens": "海洋", "cerydra": "律法",
    "dan_heng_permansor_terrae": "大地",
}

# 空壳角色占位标记键（角色录入后 POEM_EFFECTS 换真函数即可激活）
POEM_PLACEHOLDER_KEYS = {
    "tribbie": "menjing", "anaxa": "lixing", "cipher": "guiji",
    "phainon": "fushi", "hysilens": "haiyang", "cerydra": "lvfa",
    "dan_heng_permansor_terrae": "dadi",
}


def _cleanse_controls(state):
    """v5.7: 解除我方全体控制类负面状态（昔涟忆灵天赋·你好世界; 通用净化复用）"""
    for eu in state.units:
        if not eu.is_alive:
            continue
        removed = [s for s in eu.statuses if getattr(s, 'category', '') == 'control']
        for s in removed:
            eu.statuses.remove(s)
            state.log.append(f'  净化: {eu.char.name}解除{s.name}')


def _select_xilian_target(state):
    """献予目标选择（数据驱动优先级, 不硬编码进昔涟AI）:
    存活非昔涟 → 未获诗黄金裔（整局生效优先→单次生效, 同档按队伍位置）
    → 已获诗黄金裔按位置循环 → 无黄金裔取非黄金裔"""
    from engine.core.character_utils import has_poem, is_gold_offspring
    allies = [eu for eu in state.units if eu.is_alive and eu.char.id != 'xilian']
    if not allies:
        return None
    gold = [eu for eu in allies if is_gold_offspring(eu)]
    if gold:
        ungifted = [eu for eu in gold if not has_poem(eu)]
        pool = ungifted or gold
        # 整局生效诗篇优先（POEM_PERSISTENT 数据驱动）; 同档按队伍位置稳定
        pool = sorted(pool, key=lambda eu: (not POEM_PERSISTENT.get(eu.char.id, False), eu.position))
        return pool[0]
    return allies[0]


def _poem_placeholder(state, target):
    """空壳黄金裔占位: 挂标记+日志, 等角色录入后自动激活"""
    key = POEM_PLACEHOLDER_KEYS.get(target.char.id)
    if key:
        target.extra[f'poem_{key}'] = 'placeholder'
    name = POEM_NAMES.get(target.char.id, '?')
    state.log.append(f'  献予「{name}」之诗: {target.char.name}(占位, 待角色录入)')


def _romantic_apply(aglaea):
    """浪漫之诗增强: 阿格莱雅+衣匠伤害+72%无视36%防御（至退出至高之姿）"""
    if aglaea.extra.get('romantic_applied'):
        return
    aglaea.base_stats.DMG_BONUS_ALL += 0.72
    aglaea.base_stats.DEF_PEN += 0.36
    ms = aglaea.memsprite_unit
    if ms and ms.is_alive:
        ms.base_stats.DMG_BONUS_ALL += 0.72
        ms.base_stats.DEF_PEN += 0.36
    aglaea.extra['romantic_applied'] = True


def _poem_shengsi(state, summoner, ms_unit, xiadie):
    """献予「生死」之诗(整场): 新蕊可溢出至200%（68000 cap, 召唤死龙时消费溢出强化晦翼）"""
    xiadie.extra['poem_shengsi'] = True
    state.log.append('  献予「生死」之诗: 新蕊上限200%')


def _poem_suiyue(state, summoner, ms_unit, cy):
    """献予「岁月」之诗(整场): 迷梦+18%; 战技/终结技后+1忆质; 战技CD额外+长夜月暴伤12%"""
    cy.extra['poem_suiyue'] = True
    state.log.append('  献予「岁月」之诗: 迷梦+18%, 忆质+1, 战技CD强化')


def _poem_tiankong(state, summoner, ms_unit, fj):
    """献予「天空」之诗(持续层): 回24能量(层数由 _use_memsprite_skill 顶部每忆灵技+2统一叠加)"""
    # v6.2.1: 统一回能入口（Codex P2-5: 直写绕过 ER/on_energy_change/迷迷充能 bank）
    from engine.core.combat_sim import _gain_energy
    _gain_energy(fj, 24.0, state=state)
    state.log.append(f'  献予「天空」之诗: 风堇回24能量 (能量={fj.current_energy:.0f})')


def _poem_langman(state, summoner, ms_unit, aglaea):
    """献予「浪漫」之诗(单次): 衣匠速度拉满+【浪漫】token; 攻击后回70能量; 双方72%/36%至退出至高之姿"""
    aglaea.extra['poem_langman'] = 1
    ms = aglaea.memsprite_unit
    if ms and ms.is_alive:
        ms.extra['spd_stack'] = 7 if aglaea.eidolon_rank >= 4 else 6
        state.log.append('  献予「浪漫」之诗: 衣匠速度叠满')
    else:
        aglaea.extra['poem_langman_spd_pending'] = True  # 衣匠不在场→召唤时补
    if aglaea.is_sovereign:
        _romantic_apply(aglaea)
    state.log.append('  献予「浪漫」之诗: 阿格莱雅获得【浪漫】')


def _poem_fenzheng(state, summoner, ms_unit, mydei):
    """献予「纷争」之诗(单次): 解控(简化:清负属性buff); 血仇中→免费弑神登神+暴伤200%; 否则拉条100%"""
    from engine.core.combat_sim import _use_skill
    # 解控（引擎无我方控制系统, 简化清理负属性buff）
    cleared = 0
    for b in list(mydei.buffs):
        if any(v < 0 for v in b.attributes.values()):
            mydei.buffs.remove(b)
            cleared += 1
            break
    if mydei.extra.get('is_blood_debt'):
        old_cd = mydei.base_stats.CRIT_DMG
        mydei.base_stats.CRIT_DMG += 2.0
        mydei.extra['poem_fenzheng_free'] = True  # 免费施放(不耗充能)
        try:
            _use_skill(mydei, state, 'skill_shenshen')
        finally:
            mydei.base_stats.CRIT_DMG = old_cd
            mydei.extra.pop('poem_fenzheng_free', None)
        state.log.append('  献予「纷争」之诗: 血仇→免费弑神登神(暴伤+200%)')
    else:
        from engine.core.combat_sim import _guest_advance_blocked
        navs = state.extra.get('navs', {})
        uidx = state.units.index(mydei)
        if uidx in navs and not _guest_advance_blocked(state, summoner, mydei):
            navs[uidx] = state.current_av
        state.log.append('  献予「纷争」之诗: 万敌行动提前100%')
    if cleared:
        state.log.append('  献予「纷争」之诗: 解除控制(简化)')


def _poem_fushi(state, summoner, ms_unit, phainon):
    """献予「负世」之诗(整场, 白厄): 火种+6 + 变身时永续燃烧(暴伤+72%上限/CR+16%/毁伤+4)"""
    from engine.core.combat_sim import _phainon_gain_huozhong, _phainon_gain_huishang
    _phainon_gain_huozhong(state, phainon, 6)
    phainon.extra['poem_fushi'] = True
    _phainon_gain_huishang(state, phainon, 4)
    phainon.base_stats.CRIT_DMG += 0.72
    phainon.base_stats.CRIT_RATE += 0.16
    state.log.append('  献予「负世」之诗: 火种+6+毁伤+4+永续燃烧(暴伤72%/CR16%)')


def _poem_haiyang(state, summoner, ms_unit, hysilens):
    """献予「海洋」之诗(整场, 海瑟音): 暖流+60能; 伤害+120%; DOT立即结算60/80%"""
    from engine.core.combat_sim import _gain_energy
    _gain_energy(hysilens, 60.0, state=state)
    hysilens.extra['poem_haiyang'] = True
    hysilens.base_stats.DMG_BONUS_ALL += 1.20
    state.log.append('  献予「海洋」之诗: 暖流+60能+伤害+120%')


def _poem_lixing(state, summoner, ms_unit, anaxa):
    """献予「理性」之诗(单次, 那刻夏): 回1SP+立即行动+战技伤害次数+3+真知"""
    from engine.core.combat_sim import _gain_skill_points, _guest_advance_blocked
    _gain_skill_points(state, 1)
    navs = state.extra.get('navs', {})
    i = state.units.index(anaxa)
    if i in navs and not _guest_advance_blocked(state, summoner, anaxa):
        navs[i] = state.current_av
    anaxa.extra['poem_lixing'] = True
    state.log.append('  献予「理性」之诗: 回1SP+立即行动+战技+3次')


def _poem_guiji(state, summoner, ms_unit, cipher):
    """献予「诡计」之诗(整场, 赛飞儿): 伤害+36%; 老主顾DEF-20%/其他-12%"""
    cipher.extra['poem_guiji'] = True
    cipher.base_stats.DMG_BONUS_ALL += 0.36
    state.log.append('  献予「诡计」之诗: 赛飞儿伤害+36%+敌方DEF降低')


def _poem_menjing(state, summoner, ms_unit, tribbie):
    """献予「门径」之诗(整场, 缇宝): 无视12%防御; 结界附加伤害额外+1次"""
    tribbie.extra['poem_menjing'] = True
    tribbie.base_stats.DEF_PEN += 0.12
    state.log.append('  献予「门径」之诗: 缇宝无视12%防御+结界附加+1次')


def _poem_lvfa(state, summoner, ms_unit, cerydra):
    """献予「律法」之诗(整场, 刻律德菈): 军功者暴伤+30%; 奇袭结束后充能+1"""
    cerydra.extra['poem_lvfa'] = True
    state.log.append('  献予「律法」之诗: 军功者暴伤+30%+奇袭后充能+1')


def _poem_dadi(state, summoner, ms_unit, dht):
    """献予「大地」之诗(丹恒·腾荒): 龙灵3次攻击附加同袍护盾80%伤害; 同袍伤害+24%"""
    dht.extra['poem_dadi'] = True
    dht.extra['poem_dadi_attacks'] = 3
    state.log.append('  献予「大地」之诗: 龙灵附加+同袍伤害+24%')


def _poem_chuangshi(state, summoner, ms_unit, tbr):
    """献予「创世」之诗(整场): ATK+德谬歌HP16%, CR+德谬歌CR72%(迷迷); 强化普攻后→德谬歌花与箭"""
    if tbr.extra.get('poem_chuangshi_applied'):
        return
    ms = summoner.memsprite_unit
    atk_bonus = ms.max_hp * 0.16
    cr_bonus = ms.base_stats.CRIT_RATE * 0.72
    tbr.base_stats.ATK += atk_bonus
    tbr.base_stats.CRIT_RATE += cr_bonus
    if tbr.memsprite_unit and tbr.memsprite_unit.is_alive:
        tbr.memsprite_unit.base_stats.ATK += atk_bonus
        tbr.memsprite_unit.base_stats.CRIT_RATE += cr_bonus
    tbr.extra.update(poem_chuangshi=True, poem_chuangshi_applied=True,
                     poem_chuangshi_atk=atk_bonus, poem_chuangshi_cr=cr_bonus)
    state.log.append(f'  献予「创世」之诗: 开拓者ATK+{atk_bonus:.0f}, CR+{cr_bonus*100:.1f}%')


# 诗表: char_id → 诗效果函数; None = 空壳占位
POEM_EFFECTS: dict = {
    "trailblazer_remembrance": _poem_chuangshi,
    "aglaea": _poem_langman,
    "mydei": _poem_fenzheng,
    "xiadie": _poem_shengsi,
    "fengjin": _poem_tiankong,
    "changyeyue": _poem_suiyue,
    "tribbie": _poem_menjing, "anaxa": _poem_lixing, "cipher": _poem_guiji,
    "phainon": _poem_fushi, "hysilens": _poem_haiyang, "cerydra": _poem_lvfa,
    "dan_heng_permansor_terrae": _poem_dadi,
}

# 诗篇生效类型（整局/单次; 来源: 角色技能介绍/昔涟.txt 各诗描述）
# 数据驱动德谬歌"此诗献予"的目标优先级——用户确认: 不硬编码进昔涟AI,
# 不同队伍选择原则有变化, 优先级由本表（可按需调整）与 _select_xilian_target 共同决定
POEM_PERSISTENT: dict = {
    "trailblazer_remembrance": True,   # 创世: 整场生效
    "aglaea": False,                   # 浪漫: 单次生效
    "mydei": False,                    # 纷争: 单次生效
    "xiadie": True,                    # 生死: 整场生效
    "fengjin": True,                   # 天空: 忆灵技被动层 + 施放回能
    "changyeyue": True,                # 岁月: 整场生效
    "tribbie": True,                   # 门径: 整场生效
    "cerydra": True,                   # 律法: 整场生效
    "dan_heng_permansor_terrae": True, # 大地: 整场生效
    "anaxa": False,                    # 理性: 单次生效
    "cipher": True,                    # 诡计: 整场生效
    "hysilens": True,                  # 海洋: 整场生效
    "phainon": True,                     # v6.6c P2: 负世整场生效（此前缺失, 诗篇目标选择可能漏白厄）
}


@dataclass
class MemSpriteUnit:
    """战斗中忆灵的运行时状态"""
    data: MemSprite                    # 忆灵数据模型
    summoner_id: str                   # 召唤者 character.id
    current_hp: float = 0.0
    max_hp: float = 0.0
    current_energy: float = 0.0
    is_alive: bool = True
    is_summoned: bool = True
    total_damage_dealt: float = 0.0
    base_stats: object = None          # CombatStats
    cumulative_healing: float = 0.0    # 累计治疗值（小伊卡乌云乌云伤害来源）
    has_future: bool = False           # 【未来】标记（忆灵持有的不会被消耗）
    shield: float = 0.0                # 护盾值（v5.0 P5）
    buffs: list = field(default_factory=list)  # v5.6.1: 忆灵暂存增益（英豪4pc/行迹2 等忆灵侧 buff）
    runtime_spd: float = None          # v5.2 问题1: 运行时速度, 避免写回 MemSprite 配置
    runtime_is_backup: bool = None     # v5.2 问题1: 运行时后援标记, 避免写回 MemSprite 配置
    extra: dict = field(default_factory=dict)

    def take_damage(self, dmg: float):
        self.current_hp = max(0.0, self.current_hp - dmg)
        if self.current_hp <= 0:
            self.is_alive = False

    @property
    def char(self):
        """兼容 _use_skill 的 .char 引用 — 返回 data"""
        return self.data

    @property
    def name(self):
        return self.data.name

    @property
    def action_spd(self):
        """v5.2: 行动用速度（运行时优先, 不写配置）"""
        return self.runtime_spd if self.runtime_spd is not None else self.data.base_SPD

    @property
    def is_backup(self):
        """v5.2: 后援标记（运行时优先, 不写配置）"""
        return self.runtime_is_backup if self.runtime_is_backup is not None else self.data.is_backup


def _ms_effective_stats(ms_unit, state=None):
    """忆灵有效面板: 基础面板 + 暂存 buff（v5.6.1: 忆灵 buff 系统接入——
    英豪4pc/长夜月行迹2 等忆灵侧增益经此生效, 死龙等无 buffs 的忆灵行为不变）
    v6.2: 织锦"及忆灵"段——装备者光锥织锦层数→忆灵暴伤+9%/层（time_woven_into_gold）"""
    from engine.core.combat_sim import _apply_stat
    s = copy.deepcopy(ms_unit.base_stats)
    for b in getattr(ms_unit, 'buffs', []):
        for attr, val in b.attributes.items():
            _apply_stat(s, attr, val)
    if state is not None:
        summoner = next((x for x in state.units
                         if x.char.id == ms_unit.summoner_id), None)
        lc = getattr(summoner, 'lightcone', None)
        if lc and lc.id == 'time_woven_into_gold':
            stack = summoner.lc_stacks.get('time_woven_into_gold::zhijin', 0)
            if stack > 0:
                s.CRIT_DMG += 0.09 * stack
    return s


class RemembranceSystem:
    """记忆命途子系统协调器"""

    def __init__(self):
        self.suppressed_actions: dict = {}  # {char_id: turns_left}

    # ── 初始化 ──

    def init_battle(self, state, units):
        """战斗开始：处理各记忆角色天赋"""
        for u in units:
            if u.char.path != "记忆":
                continue
            # 长夜月：战斗开始自动召唤忆灵
            if u.char.id == "changyeyue":
                ms_data = u.char.memsprite
                if ms_data:
                    self.summon_memsprite(state, u, ms_data)
                    # v6.2.1: 统一回能入口（Codex P2-5 审计: 直写绕过 ER/能量事件/迷迷 bank）
                    from engine.core.combat_sim import _gain_energy
                    _gain_energy(u, 70.0, state=state)
                    _gain_yizhi(state, u, 1)  # v5.1: E2 每获得忆质+2
                    state.log.append(f'[Init] 长夜月天赋: 召唤长夜(SPD={ms_data.base_SPD}), +70能量, +1忆质')
                    u.base_stats.CRIT_RATE += 0.35
                    if u.memsprite_unit:
                        u.memsprite_unit.base_stats.CRIT_RATE += 0.35
            # 遐蝶：战斗开始不自动召唤，由终结技触发

    # ── 忆灵生命周期 ──

    def summon_memsprite(self, state, summoner, ms_data: MemSprite, hp_override=None):
        """召唤忆灵（若已在场则回血，否则创建）
        hp_override: v6.3.0 遐蝶秘技——死龙 HP=新蕊上限50%（非终结技的 34000 满额）"""
        existing = next((m for m in state.memsprites
                         if m.data.name == ms_data.name and m.summoner_id == summoner.char.id), None)
        if existing:
            # v5.7: 阿格莱雅已在场回血走 JSON heal 效果（target=memsprite 50%），
            # 通用分支再回50%会与 heal effect 叠加成100%（实机50%）；长夜月/风堇保留此语义
            if summoner.char.id != 'aglaea':
                existing.current_hp = min(existing.max_hp, existing.current_hp + existing.max_hp * 0.50)
                state.log.append(f'  {ms_data.name}已在场→回血50% (HP={existing.current_hp:.0f}/{existing.max_hp:.0f})')
            return existing

        # 遐蝶死龙特殊HP：基于新蕊上限（34000），不继承召唤者
        # v6.3.0: hp_override 供秘技路径（死龙 HP=新蕊上限50%）
        if summoner.char.id == "xiadie":
            dragon_hp = hp_override or 34000.0
            ms_stats = copy.deepcopy(summoner.base_stats)
            ms_stats.HP = dragon_hp
            ms_unit = MemSpriteUnit(
                data=ms_data, summoner_id=summoner.char.id,
                max_hp=dragon_hp, current_hp=dragon_hp,
                base_stats=ms_stats,
            )
            ms_unit.current_energy = 0
            ms_unit.runtime_is_backup = True  # v5.2: 后援单位（运行时标记, 不写配置）
            ms_unit.extra['flame_mult'] = 24.0  # 焰息倍率递增起点
            state.memsprites.append(ms_unit)
            summoner.memsprite_unit = ms_unit
            # 遐蝶E2: 召唤→+2炽意(抵扣焰息HP消耗), 行动提前100%, 下次强化战技+30%新蕊
            if summoner.eidolon_rank >= 2:
                summoner.extra['chiyi'] = 2
                summoner.extra['xiadie_e2_skill_pending'] = True
                navs = state.extra.get('navs', {})
                uid = state.units.index(summoner)
                if uid in navs:
                    from engine.core.combat_sim import _set_av
                    _set_av(state, navs, uid, state.current_av)  # v6.2.1b P3-1: 统一入口补戳
                state.log.append('  遐蝶E2: +2炽意, 行动提前100%')
            # 献予「生死」之诗: 消费终结技前捕获的溢出新蕊→晦翼倍率加成(每1%→+0.24, ≤2敌→+0.48)
            overflow = summoner.extra.pop('shengsi_overflow', 0.0)
            if overflow > 0:
                pct = overflow / 34000.0 * 100.0
                n_enemies = len(state.alive_enemies() or state.enemies)
                bonus = pct * (0.48 if n_enemies <= 2 else 0.24)
                ms_unit.extra['huiyi_mult_bonus'] = bonus
                state.log.append(f'  献予「生死」之诗: 消耗溢出{overflow:.0f}→晦翼倍率+{bonus:.1f}%')
            # 死龙0行动值留在Y轴（后到先动→排在最先）。回到遐蝶常规回合→强化战技→之后死龙回合
            ms_unit.extra['next_av'] = state.current_av
            from engine.core.combat_sim import _stamp_av_key
            _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳, 同AV并列才能后到先动
            state.log.append(f'  召唤死龙 HP={dragon_hp:.0f} SPD={ms_data.base_SPD} (后援, Y轴行动条)')
            state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                     summoner=summoner, ms_unit=ms_unit)
            return ms_unit

        # 迷迷: HP=开拓者80%生命上限+640, SPD=130固定, ATK=同开拓者
        if summoner.char.id == 'trailblazer_remembrance':
            ms_stats = copy.deepcopy(summoner.base_stats)
            ms_stats.HP = summoner.base_stats.HP * 0.80 + 640
            ms_stats.SPD = 130
            ms_stats.ATK = summoner.base_stats.ATK
            ms_unit = MemSpriteUnit(
                data=ms_data, summoner_id=summoner.char.id,
                max_hp=ms_stats.HP, current_hp=ms_stats.HP,
                base_stats=ms_stats,
            )
            ms_unit.current_energy = 0
            ms_unit.extra['charge'] = 0.0  # 迷迷充能 0-100%
            state.memsprites.append(ms_unit)
            summoner.memsprite_unit = ms_unit
            state.log.append(f'  召唤迷迷 HP={ms_stats.HP:.0f} SPD={ms_stats.SPD:.0f} '
                             f'(开拓者HP×80%+640, SPD=130)')
            # 献予「创世」之诗: 迷迷重召→补挂存量ATK/CR加成
            if summoner.extra.get('poem_chuangshi'):
                ms_unit.base_stats.ATK += summoner.extra.get('poem_chuangshi_atk', 0.0)
                ms_unit.base_stats.CRIT_RATE += summoner.extra.get('poem_chuangshi_cr', 0.0)
            # 忆灵天赋·迷迷加油: 召唤时+50%充能
            ch = self._mimi_charge_gain(state, ms_unit, 50)
            state.log.append(f'  迷迷加油: 充能+50% → {ch:.0f}%')
            # 行迹2·追念之权杖: 首次召唤+40%充能
            if not summoner.extra.get('tbr_summoned'):
                summoner.extra['tbr_summoned'] = True
                ch = self._mimi_charge_gain(state, ms_unit, 40)
                state.log.append(f'  追念之权杖: 首次召唤充能+40% → {ch:.0f}%')
            # 忆灵天赋·伙伴一起: 全队暴伤 += 迷迷12%暴伤 + 24%
            cd_bonus = ms_stats.CRIT_DMG * 0.12 + 0.24
            for eu in state.units:
                if eu.is_alive:
                    eu.base_stats.CRIT_DMG += cd_bonus
            state.log.append(f'  伙伴一起: 全队暴伤+{cd_bonus*100:.1f}%')
            state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                     summoner=summoner, ms_unit=ms_unit)
            return ms_unit

        # 衣匠：HP=阿格莱雅66%生命上限+720，SPD=阿格莱雅35%(动态)
        if summoner.char.id == 'aglaea':
            from engine.core.combat_sim import _effective_spd
            ms_stats = copy.deepcopy(summoner.base_stats)
            ms_stats.HP = summoner.base_stats.HP * 0.66 + 720
            ms_stats.SPD = _effective_spd(summoner, state) * 0.35
            ms_stats.ATK = summoner.base_stats.ATK
            ms_unit = MemSpriteUnit(
                data=ms_data, summoner_id=summoner.char.id,
                max_hp=ms_stats.HP, current_hp=ms_stats.HP,
                base_stats=ms_stats,
            )
            ms_unit.current_energy = 0
            # 织运之竭: 上次消失保留的速度层(最多1层)
            retained = summoner.extra.get('aglaea_retained_spd', 0)
            ms_unit.extra['spd_stack'] = retained
            summoner.extra['aglaea_retained_spd'] = 0
            state.memsprites.append(ms_unit)
            summoner.memsprite_unit = ms_unit
            state.log.append(f'  召唤衣匠 HP={ms_stats.HP:.0f} SPD={ms_stats.SPD:.0f} '
                             f'(阿格莱雅HP×66%+720, SPD×35%)'
                             + (f' 织运之竭恢复{retained}层速度' if retained else ''))
            # 忆灵天赋·飞驰之夏: 衣匠被召唤时自身行动提前100%
            ms_unit.extra['next_av'] = state.current_av
            from engine.core.combat_sim import _stamp_av_key
            _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
            state.log.append('  飞驰之夏: 衣匠行动提前100%')
            # v5.7: 召唤衣匠（战技/终结技）→阿格莱雅自身立即行动（阿格莱雅.txt 战技/终结技）
            navs = state.extra.get('navs', {})
            uid = state.units.index(summoner)
            if uid in navs:
                from engine.core.combat_sim import _set_av
                _set_av(state, navs, uid, state.current_av)  # v6.2.1b P3-1: 统一入口补戳
                state.log.append('  召唤衣匠: 自身立即行动')
            state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                     summoner=summoner, ms_unit=ms_unit)
            return ms_unit

        # v6.11.1 知更鸟·晴歌: 战技召唤贝茜/天赋气氛阈值召唤啾米·派丁（三忆灵并存, 独立逻辑）
        if summoner.char.id == 'robin_summeretto':
            return self._qingge_summon_variant(state, summoner, ms_data, '贝茜')

        # 普通忆灵：属性继承召唤者
        ms_stats = copy.deepcopy(summoner.base_stats)
        for attr_key, ratio in ms_data.inherit_ratios.items():
            if isinstance(ratio, (int, float)):
                if attr_key == "HP":
                    ms_stats.HP = summoner.base_stats.HP * ratio
                elif attr_key == "ATK":
                    ms_stats.ATK = summoner.base_stats.ATK * ratio
                elif attr_key == "DEF":
                    ms_stats.DEF = summoner.base_stats.DEF * ratio

        # v5.7 长夜月E2: 长夜月与「长夜」暴击伤害+40%（copy 后单点应用, 双方各加一次;
        # init_battle 首次召唤先于 enter_battle hook, 故在此内联而非 hook）
        if summoner.char.id == 'changyeyue' and summoner.eidolon_rank >= 2 \
                and not summoner.extra.get('cy_e2_cd_applied'):
            summoner.base_stats.CRIT_DMG += 0.40
            ms_stats.CRIT_DMG += 0.40
            summoner.extra['cy_e2_cd_applied'] = True
            state.log.append('  长夜月E2: 双方暴击伤害+40%')

        ms_unit = MemSpriteUnit(
            data=ms_data,
            summoner_id=summoner.char.id,
            max_hp=ms_stats.HP,
            current_hp=ms_stats.HP,
            base_stats=ms_stats,
        )
        ms_unit.current_energy = 0

        # v5.7 长夜月忆灵天赋·孤独浮游漆黑: 「长夜」在场时双方伤害+50%（永久, 消失移除）
        if summoner.char.id == 'changyeyue':
            from engine.core.combat_sim import TimedBuff
            for holder in (summoner, ms_unit):
                holder.buffs.append(TimedBuff(
                    source_id='changyeyue', attributes={'DMG_BONUS_ALL': 50.0},
                    remaining_turns=-1, param_id='changyeyue_night_abyss',
                    source_name='孤独浮游漆黑'))
            # 忆质≥16 立即行动标记复位（迷梦后重新召唤可再次触发）
            summoner.extra['cy_immediate_done'] = False
            state.log.append('  孤独浮游漆黑: 长夜在场, 双方伤害+50%')

        # 德谬歌天赋·等待，在所有的过去: 在场时昔涟与德谬歌生命上限+24%
        if summoner.char.id == 'xilian':
            ms_unit.max_hp = ms_unit.max_hp * 1.24
            ms_unit.current_hp = ms_unit.current_hp * 1.24
            ms_unit.base_stats.HP = ms_unit.max_hp
            summoner.base_stats.HP = summoner.base_stats.HP * 1.24
            summoner.max_hp = summoner.max_hp * 1.24
            summoner.current_hp = min(summoner.max_hp, summoner.current_hp)
            state.log.append(f'  等待，在所有的过去: 昔涟+德谬歌HP上限+24% (德谬歌HP={ms_unit.max_hp:.0f})')
            # 献予真我之诗: 德谬歌被召唤时+1故事
            summoner.story_points += 1
            state.log.append(f'  献予真我之诗: 德谬歌被召唤→故事+1 ({summoner.story_points}/3)')
            # v5.7 忆灵天赋·你好世界♪: 德谬歌被召唤时解除我方全体控制类负面状态
            _cleanse_controls(state)

        # v5.7 风堇忆灵天赋·展翼奔向日辉: 小伊卡被召唤→风堇+15能量, 首次召唤额外+30
        if summoner.char.id == 'fengjin':
            first = not summoner.extra.get('fengjin_first_summon', False)
            gain = 45 if first else 15
            from engine.core.combat_sim import _gain_energy
            _gain_energy(summoner, gain, state=state)
            summoner.extra['fengjin_first_summon'] = True
            state.log.append(f'  展翼奔向日辉: 风堇+{gain}能量')

        state.memsprites.append(ms_unit)
        summoner.memsprite_unit = ms_unit
        state.log.append(f'  召唤忆灵「{ms_data.name}」HP={ms_stats.HP:.0f} SPD={ms_data.base_SPD}')

        # 与夜，形影，不离：立即行动（行动后设置下次行动值——
        # 若不写 next_av，候选生成会按"当前时刻+AV/SPD"重算，与召唤者同速时
        # 永远同 AV 且 stamp 落后 → 忆灵行动被饿死）
        self._force_memsprite_action(state, summoner, ms_unit)
        ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / max(ms_unit.action_spd, 1.0)
        from engine.core.combat_sim import _stamp_av_key
        _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
        state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                 summoner=summoner, ms_unit=ms_unit)
        return ms_unit

    def despawn_memsprite(self, state, summoner, ms_unit, reason: str = ""):
        """忆灵消失，触发 on_despawn 效果"""
        if ms_unit not in state.memsprites:
            return
        ms_name = ms_unit.data.name
        lost_hp = ms_unit.current_hp  # 消失前剩余HP
        state.memsprites.remove(ms_unit)
        summoner.memsprite_unit = None

        # v5.4 光锥忆灵消失事件（致长夜的星光: 任意我方忆灵消失→持有者回8能量）
        from engine.core.combat_sim import _process_lc_effects
        for unit in state.units:
            if unit.is_alive:
                _process_lc_effects(unit, state, "on_memsprite_despawn")

        # 遐蝶天赋: 忆灵消失(迷梦自爆/被消灭)剩余HP→新蕊/死龙回血
        # 死龙除外: 自爆消失不算新蕊(自爆结束才判定死龙不在场)，死龙自爆后剩余=1点
        if summoner.char.id == 'xiadie' and lost_hp > 0 and ms_name != '死龙':
            from engine.core.combat_sim import _xiadie_absorb_hp_loss
            _xiadie_absorb_hp_loss(state, lost_hp, f'{ms_name}消失')

        # 长夜月专属：与你，再见，无期: SPD+10% + 每点忆质+1%(上限40)
        if summoner.char.id == 'changyeyue':
            # v6.2.1: 忆质用量读清零前快照（迷梦块先清零再 despawn, 直接读=0）
            yizhi_consumed = summoner.extra.pop('yizhi_consumed_snapshot', summoner.yizhi)
            spd_bonus = 10 + min(yizhi_consumed, 40)
            # v6.2.1: 加速无法叠加——旧加成先回减再施新（此前永久叠加漂移）
            old_amt = summoner.extra.pop('night_spd_bonus_amt', 0.0)
            if old_amt > 0:
                summoner.base_stats.SPD -= old_amt
            amt = summoner.base_stats._base_SPD * (spd_bonus / 100.0)
            summoner.base_stats.SPD += amt
            summoner.extra['night_spd_bonus_amt'] = amt
            state.log.append(f'  {ms_name}消失→长夜月SPD+{spd_bonus}%(下回合移除), 累计忆质={yizhi_consumed}')
            summoner.extra['night_spd_bonus_turns'] = 1
            # v5.7 孤独浮游漆黑: 「长夜」消失→移除双方+50%伤害
            summoner.buffs = [b for b in summoner.buffs
                              if getattr(b, 'param_id', '') != 'changyeyue_night_abyss']
        # 衣匠消失: 退出至高之姿 + 枯草之盈(+20能量) + 织运之竭(速度层保留1层)
        elif summoner.char.id == 'aglaea':
            summoner.is_sovereign = False
            summoner.extra['countdown_turns'] = 0
            # v6.2.1: 退出至高之姿对称回减（Harness P2-4, 此前永久留存）
            atk_bonus = summoner.extra.pop('sovereign_atk_bonus', 0.0)
            if atk_bonus > 0:
                summoner.base_stats.ATK -= atk_bonus
                ms_unit.base_stats.ATK -= atk_bonus
                state.log.append(f'  短视之惩回收: 攻击力-{atk_bonus:.0f}')
            spd_bonus = summoner.extra.pop('sovereign_spd_bonus', 0.0)
            if spd_bonus > 0:
                summoner.base_stats.SPD -= spd_bonus
                state.log.append(f'  至高之姿回收: 速度-{spd_bonus:.0f}')
            if summoner.eidolon_rank >= 6:
                summoner.base_stats.RES_PEN['雷'] -= 0.20
                ms_unit.base_stats.RES_PEN['雷'] -= 0.20
                state.log.append('  E6回收: 雷抗穿透-20%')
            # 献予「浪漫」之诗: 退出至高→移除双方72%/36%增强(只减自己加的, 不动E2层)
            if summoner.extra.get('romantic_applied'):
                summoner.base_stats.DMG_BONUS_ALL -= 0.72
                summoner.base_stats.DEF_PEN -= 0.36
                ms_unit.base_stats.DMG_BONUS_ALL -= 0.72
                ms_unit.base_stats.DEF_PEN -= 0.36
                summoner.extra['romantic_applied'] = False
                state.log.append('  献予「浪漫」之诗: 退出至高→增强移除')
            # 枯草之盈: 衣匠消失时阿格莱雅恢复20点能量（v6.2.1: 统一回能入口）
            from engine.core.combat_sim import _gain_energy
            _gain_energy(summoner, 20.0, state=state)
            state.log.append(f'  衣匠消失→阿格莱雅退出【至高之姿】, +20能量')
            # 织运之竭: 速度层保留1层，下次召唤恢复
            stack = ms_unit.extra.get('spd_stack', 0)
            if stack > 0:
                summoner.extra['aglaea_retained_spd'] = 1
                state.log.append('  织运之竭: 速度层保留1层')
        # v5.7 开拓者·记忆忆灵天赋·遗憾不留: 迷迷消失→开拓者行动提前25%
        elif summoner.char.id == 'trailblazer_remembrance':
            from engine.core.combat_sim import AV_PER_TURN, _effective_spd
            navs = state.extra.get('navs', {})
            for i, eu in enumerate(state.units):
                if eu is summoner and i in navs:
                    navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(summoner, state)) * 0.25)
                    break
            state.log.append('  遗憾不留: 迷迷消失→开拓者行动提前25%')
        # v5.7 风堇忆灵天赋·坠落然后飞翔: 小伊卡消失→风堇行动提前30%
        elif summoner.char.id == 'fengjin':
            from engine.core.combat_sim import AV_PER_TURN, _effective_spd
            navs = state.extra.get('navs', {})
            for i, eu in enumerate(state.units):
                if eu is summoner and i in navs:
                    navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(summoner, state)) * 0.30)
                    break
            state.log.append('  坠落然后飞翔: 小伊卡消失→风堇行动提前30%')
        else:
            state.log.append(f'  {ms_name}消失')

    def _dragon_flame_once(self, state, summoner, ms_unit):
        """死龙单次喷吐(Y轴行动): 消耗25%生命上限, HP≤25%→主动降至1点→自爆(晦翼)。
        倍率递增: 24→28→34→34(两档后封顶)。只要HP>1就稳定能喷一次。"""
        if not ms_unit.is_alive or ms_unit.current_hp <= 1:
            return
        hp_pct = 25.0
        base_multiplier = ms_unit.extra.get('flame_mult', 24.0)
        multiplier = base_multiplier
        # 行迹1·西风的驻足: 施放战技(乌黯)后焰息伤害+30%/层, 叠6层, 回合末消失
        flame_stack = summoner.extra.get('xiadie_flame_stack', 0)
        if flame_stack > 0:
            multiplier = multiplier * (1.0 + 0.30 * flame_stack)
        will_destruct = ms_unit.current_hp <= ms_unit.max_hp * 0.25
        # 释放焰息(消耗25%生命上限，最低降至1点)
        cost = ms_unit.max_hp * (hp_pct / 100.0)
        # 遐蝶E2炽意: 抵扣焰息HP消耗（不扣HP, 仍正常喷吐与自爆判定）
        if summoner.extra.get('chiyi', 0) > 0:
            summoner.extra['chiyi'] -= 1
            cost = 0
            state.log.append(f'  炽意抵扣: 焰息不消耗HP (剩余{summoner.extra["chiyi"]}层)')
        ms_unit.current_hp = max(1, ms_unit.current_hp - cost)
        dmg, speed_boost = self._calc_flame_damage(state, summoner, ms_unit, multiplier)
        if speed_boost:
            ms_unit.extra['xiadie_spd_boost'] = 1
            state.log.append('  倒置的火炬: 死龙速度+100%(下次行动)')
        ms_unit.total_damage_dealt += dmg
        summoner.total_damage_dealt += dmg
        state.log.append(f'  焰息: {dmg:.0f} (倍率{multiplier:.0f}%) HP={ms_unit.current_hp:.0f}/{ms_unit.max_hp:.0f}')
        # 倍率递增: 24→28→34(两档后封顶)。行迹的临时增伤不能写回基础序列。
        ms_unit.extra['flame_mult'] = min(
            34.0, base_multiplier + (6 if base_multiplier >= 28 else 4))
        # HP≤25%的该次喷吐后→已降至1点→触发晦翼自爆
        if will_destruct:
            state.log.append(f'  HP≤25%: 已降至1点, 触发晦翼')
            self._trigger_dragon_death(state, summoner, ms_unit)

    @staticmethod
    def _xiadie_e1_mult(state, t):
        """遐蝶E1: 敌HP≤80%/50%→死龙伤害120%/140%"""
        bp = state.extra.get('enemy_blueprint') or state.enemies[0]
        ratio = t.HP / bp.HP if bp.HP > 0 else 1.0
        return 1.40 if ratio <= 0.50 else (1.20 if ratio <= 0.80 else 1.0)

    def _calc_flame_damage(self, state, summoner, ms_unit, multiplier):
        """焰息伤害：遐蝶生命上限% × multiplier（用户确认: 死龙伤害倍率全部按遐蝶生命计算,
        非死龙HP=34000; v5.6.1: 忆灵有效面板含暂存 buff）"""
        alive = [e for e in state.enemies if e.HP > 0] or state.enemies
        total = 0.0
        speed_boost = False
        from engine.core.combat_sim import _commit_enemy_damage
        for t in alive:
            mult = multiplier
            if summoner.eidolon_rank >= 1:
                mult = mult * self._xiadie_e1_mult(state, t)
            d = calculate_damage(_ms_effective_stats(ms_unit, state), t, summoner.max_hp, mult,
                                "direct", "量子", 80, summoner.base_stats.CRIT_RATE >= 0.5, crit_mode="expected")
            total += d.final_damage
            _, killed = _commit_enemy_damage(state, summoner, t, d.final_damage)
            if killed:
                speed_boost = True
            elif d.final_damage <= 0:
                # 当前模型中“无法削减生命”只会表现为最终伤害为零。
                speed_boost = True
        return total, speed_boost

    def _trigger_dragon_death(self, state, summoner, ms_unit):
        """死龙消失→灼掠幽墟的晦翼：6次弹射(死龙HP×40%)+全队回血"""
        alive = state.alive_enemies() or state.enemies
        bounce_count = 9 if summoner.eidolon_rank >= 6 else 6  # E6: 弹射+3
        total = 0.0
        # 献予「生死」之诗: 溢出消费的晦翼倍率加成
        huiyi_bonus = ms_unit.extra.get('huiyi_mult_bonus', 0.0)
        from engine.core.combat_sim import _commit_enemy_damage
        for _ in range(bounce_count):
            alive_now = [e for e in alive if e.HP > 0]
            if not alive_now:
                break
            t = random.choice(alive_now)
            mult = 40.0 + huiyi_bonus
            if summoner.eidolon_rank >= 1:
                mult = mult * self._xiadie_e1_mult(state, t)
            d = calculate_damage(_ms_effective_stats(ms_unit, state), t, summoner.max_hp, mult,
                                "direct", "量子", 80, summoner.base_stats.CRIT_RATE >= 0.5, crit_mode="expected")
            total += d.final_damage
            _commit_enemy_damage(state, summoner, t, d.final_damage)
        ms_unit.total_damage_dealt += total
        summoner.total_damage_dealt += total
        # 全队回血6%HP+800（自爆期间死龙在场判定→不触发收容的暗潮转化新蕊）
        for eu in state.units:
            if eu.is_alive:
                heal = summoner.base_stats.HP * 0.06 + 800
                eu.current_hp = min(eu.max_hp, eu.current_hp + heal)
        state.log.append(f'  灼掠幽墟的晦翼: {total:.0f} ({bounce_count}次弹射) + 全队回血(不攒新蕊)')
        # 解除境界
        if state.realm_owner == 'xiadie':
            for e in state.enemies:
                for elem in e.element_res:
                    e.element_res[elem] += 0.20
            state.realm_owner = ''
            state.realm_turns = 0
            state.log.append('  解除【遗世冥域】')
        # 移除死龙
        self.despawn_memsprite(state, summoner, ms_unit)

    def _force_memsprite_action(self, state, summoner, ms_unit):
        """忆灵立即行动"""
        state.log.append(f'  「{ms_unit.data.name}」立即行动!')
        # v6.2.1: 行动后重写排程（Harness P2-2: 旧 next_av 残留→按原排程再动一次双行动）
        if ms_unit.action_spd > 0:
            from engine.core.combat_sim import _stamp_av_key, AV_PER_TURN
            ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / ms_unit.action_spd
            _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
        self._memsprite_ai(state, summoner, ms_unit)

    # ── 忆灵AI ──

    def _memsprite_ai(self, state, summoner, ms_unit):
        """忆灵AI：迷迷按充能调度; 长夜优先迷梦(yizhi≥16) > 普攻"""
        if not ms_unit.is_alive:
            return
        # 迷迷: 充能<100%→坏人麻烦; 100%→我会帮你
        if summoner.char.id == 'trailblazer_remembrance':
            self._tbr_memsprite_ai(state, summoner, ms_unit)
            return
        # 德谬歌: 选择释放花与箭/此诗献予（扣12追忆）——召唤立即行动/额外回合共用
        if summoner.char.id == 'xilian':
            self._xilian_memsprite_action(state, summoner, ms_unit)
            return
        # v6.11.1 晴空乐手: 回合开始时施放忆灵技·叽叽啾啾四重奏
        if summoner.char.id == 'robin_summeretto':
            if "memsprite_basic" in ms_unit.data.skills:
                self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")
            return
        # 检查是否可以放迷梦: yizhi≥16 且召唤者不处于控制状态
        can_mimeng = summoner.yizhi >= 16

        if can_mimeng and "memsprite_skill" in ms_unit.data.skills:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_skill")
        elif "memsprite_basic" in ms_unit.data.skills:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")

    @staticmethod
    def _dispatch_memsprite_support_events(state, summoner, skill):
        """Dispatch post-action events for non-damaging memsprite support skills."""
        from engine.core.combat_sim import _process_lc_effects

        state.extra['lc_attack_targets'] = 0
        state.extra['lc_last_memsprite_target'] = getattr(skill, 'target', '')
        _process_lc_effects(summoner, state, "on_memsprite_attack")
        state.hooks.trigger_all("on_memsprite_attack", u=summoner, state=state)
        _process_lc_effects(summoner, state, "on_memsprite_skill")

    def _use_memsprite_skill(self, state, summoner, ms_unit, skill_key: str):
        """忆灵使用技能"""
        skill = ms_unit.data.skills.get(skill_key)
        if not skill:
            return
        # v5.7: 忆灵行动中标记（_gain_yizhi 跨16的立即行动防嵌套）
        state.extra['_ms_acting'] = True
        try:
            result = self._use_memsprite_skill_inner(state, summoner, ms_unit, skill_key, skill)
            self._decrement_memsprite_skill_buffs(state, summoner, ms_unit)
            return result
        finally:
            state.extra['_ms_acting'] = False

    @staticmethod
    def _memsprite_kill_check(state, summoner, t, before_hp):
        """v6.2.1: 忆灵伤害击杀检测（Harness P1-5: 对齐死龙 670-672 与 _use_skill 管线）"""
        if before_hp > 0 and t.HP <= 0:
            state.extra['killed_this_action'] = state.extra.get('killed_this_action', 0) + 1
            from engine.core.combat_sim import _process_lc_effects, _record_enemy_kill
            _record_enemy_kill(state)
            state.hooks.trigger(summoner.char.id, "on_kill", u=summoner, state=state, enemy=t)
            from engine.core.combat_sim import _process_lc_effects
            _process_lc_effects(summoner, state, "on_kill")

    @staticmethod
    def _decrement_memsprite_skill_buffs(state, summoner, ms_unit):
        """德谬歌/小伊卡技能结算后，自身有限持续效果减少一回合。"""
        if summoner.char.id not in ('xilian', 'fengjin') or not ms_unit.is_alive:
            return
        kept = []
        for b in ms_unit.buffs:
            if b.remaining_turns > 0:
                b.remaining_turns -= 1
            # -1 等负数表示永久；0 表示本次结算后到期。
            if b.remaining_turns != 0:
                kept.append(b)
        removed = len(ms_unit.buffs) - len(kept)
        ms_unit.buffs[:] = kept
        if removed:
            state.log.append(f'  {ms_unit.data.name}技能后: 自身持续效果-1({removed}层到期移除)')

    def _use_memsprite_skill_inner(self, state, summoner, ms_unit, skill_key, skill):
        """忆灵使用技能（主体, v5.7 拆出以包 _ms_acting 防护标记）"""
        from engine.core.combat_sim import _apply_tbr_support

        # 长夜月行迹3·烛火起烛火熄（用户确认实机）: 长夜月或**任意我方忆灵**施放技能
        # → 长夜月恢复5能量+1忆质（迷梦频率与SP回源的关键; 长夜月忆灵自己的忆质也在此统一）
        cy = next((x for x in state.units
                   if x.char.id == 'changyeyue' and x.is_alive), None)
        if cy:
            from engine.core.combat_sim import _gain_energy
            _gain_energy(cy, 5.0, state=state)
            _gain_yizhi(state, cy, 1)

        # 献予「天空」之诗: 德谬歌施放忆灵技→风堇+2层（覆盖所有忆灵技路径, 含此诗献予本身）
        if summoner.char.id == 'xilian':
            fj = next((x for x in state.units if x.char.id == 'fengjin' and x.is_alive), None)
            if fj and 'poem_tiankong' in fj.extra:
                fj.extra['poem_tiankong'] = fj.extra.get('poem_tiankong', 0) + 2
                state.log.append(f'  献予「天空」之诗: 风堇+2层 ({fj.extra["poem_tiankong"]}层)')

        # 此诗献予一切生命: 无倍率的辅助技 → 给队友上增伤
        if not skill.multipliers and summoner.char.id == 'xilian':
            self._xilian_support_skill(state, summoner, ms_unit)
            self._dispatch_memsprite_support_events(state, summoner, skill)
            return
        # 我会！帮你！: 迷迷充能100%→指定单体行动提前100%+声援3回合
        if not skill.multipliers and summoner.char.id == 'trailblazer_remembrance':
            self._tbr_support_skill(state, summoner, ms_unit)
            self._dispatch_memsprite_support_events(state, summoner, skill)
            return

        alive = [e for e in state.enemies if e.HP > 0] or state.enemies
        if not alive:
            return

        # 默认选目标（mult 无 target 字段时的回退；优先上次目标）
        # v5.7: 衣匠自动选目标优先【间隙织线】状态敌人（阿格莱雅.txt 忆灵天赋）
        default_targets = None
        if skill.target == "all_enemies":
            default_targets = alive
        elif skill.target == "single_enemy":
            if summoner.char.id == 'aglaea':
                g = next((e for e in alive if e.extra.get('gossamer')), None)
                if g:
                    default_targets = [g]
            if default_targets is None:
                if summoner.last_target_id:
                    t = next((e for e in alive if e.id == summoner.last_target_id), None)
                    default_targets = [t] if t else [alive[0]]
                else:
                    default_targets = [alive[0]]
        else:
            default_targets = [alive[0]]
        targets = default_targets

        total_dmg = 0.0
        yizhi = summoner.yizhi

        # 长夜月E1: 敌方4/3/2/1名→我方忆灵伤害120/125/130/150%（在场生效, 覆盖全队忆灵）
        cy_mult = 1.0
        cy_owner = next((x for x in state.units
                         if x.char.id == 'changyeyue' and x.is_alive), None)
        if cy_owner and cy_owner.eidolon_rank >= 1:
            n = max(min(len(alive), 4), 1)
            cy_mult = {4: 1.2, 3: 1.25, 2: 1.3, 1: 1.5}[n]

        # 长夜月的战技/秘技 buff 存在于角色身上，忆灵必须在伤害结算时
        # 显式取得该暴伤；仅战技 buff 会额外启用行迹1。
        # v5.6.1: 基础 = 忆灵有效面板（含暂存 buff）
        ms_stats = _ms_effective_stats(ms_unit, state)
        # v5.4 忆灵面板 LC 修正：已激活的团队效果作用于所有我方忆灵。
        ms_memsp = _team_memsprite_def_pen(state)
        if ms_memsp:
            ms_stats = copy.deepcopy(ms_stats)
            ms_stats.DEF_PEN += ms_memsp
        t1_cy = next((x for x in state.units
                      if x.char.id == 'changyeyue' and x.is_alive), None)
        cy_cd_buffs = []
        if t1_cy:
            cy_cd_buffs = [
                b for b in t1_cy.buffs
                if getattr(b, 'param_id', '') in ('changyeyue_skill_cd',
                                                  'changyeyue_tech_cd')
            ]
        if cy_cd_buffs:
            ms_stats = copy.deepcopy(ms_stats)
            ms_stats.CRIT_DMG += sum(
                b.attributes.get('CRIT_DMG', 0.0) / 100.0
                for b in cy_cd_buffs
            )

        # 长夜月行迹1·天亮了，雨落了: 战技持续期间, 按队伍记忆命途数量给忆灵暴伤加成
        # (记忆命途 1/2/3/4+ → 忆灵暴伤 +5%/15%/50%/65%; 战技 buff 活跃即生效)
        skill_cd_active = any(
            getattr(b, 'param_id', '') == 'changyeyue_skill_cd'
            for b in cy_cd_buffs
        )
        if t1_cy and skill_cd_active:
            from engine.core.character_utils import count_remembrance
            n = max(min(count_remembrance(state.units), 4), 1)
            t1_bonus = {1: 0.05, 2: 0.15, 3: 0.50, 4: 0.65}[n]
            ms_stats.CRIT_DMG += t1_bonus
            state.log.append(f'  天亮了，雨落了: 忆灵暴伤+{t1_bonus * 100:.0f}% (记忆×{n})')

        for mult in skill.multipliers:
            scaling_hp = ms_stats.HP if hasattr(ms_stats, 'HP') else ms_unit.max_hp
            scale = mult.scale if hasattr(mult, 'scale') else 0
            hits = mult.hits  # v5.3: 解析器已支持 _hits 字段
            # v5.7: 逐倍率目标（衣匠刺纹之陷主110%/相邻66%、迷梦主12%/其他6%）
            from engine.core.combat_sim import _select_targets
            mt = getattr(mult, 'target', '') or skill.target
            targets = _select_targets(alive, mt)

            # 对迷梦：每点忆质倍率
            per_yizhi = getattr(mult, 'per_yizhi', False)
            if per_yizhi:
                scale = scale * yizhi

            # 对追忆：每4点忆质额外倍率
            if skill_key == "memsprite_basic" and yizhi >= 4:
                extra_hits = yizhi // 4
                scale += 10.0 * extra_hits
            # 长夜月E1倍率乘到最终 scale（per_yizhi/追忆计算之后）
            scale *= cy_mult
            # v6.11.1 晴歌: E5忆灵技等级+1(每级+5%), E6忆灵技倍率×2
            # v7.0.0 A3: E5改读 skill_level_boost 消除双重来源(解析器统一入口)
            if summoner.char.id == 'robin_summeretto' and skill_key == 'memsprite_basic':
                ms_boost = 1.0 + 0.05 * (
                    summoner.extra.get('skill_level_boost', {}) or {}).get(
                    'memsprite_skill', 0)
                scale *= ms_boost
                if summoner.eidolon_rank >= 6:
                    scale *= 2.0
            # 献予「岁月」之诗: 迷梦伤害+18%
            if skill_key == "memsprite_skill" and summoner.char.id == 'changyeyue' \
                    and summoner.extra.get('poem_suiyue'):
                scale *= 1.18

            # 弹射: _hits>1 时对随机单体多次攻击（坏人麻烦4次弹射）
            if hits > 1:
                for _ in range(hits):
                    # v6.2.1: 每段重新选择存活目标（Codex P1-1 同类: 固定列表会命中尸体）
                    alive_now = [e for e in alive if e.HP > 0]
                    if not alive_now:
                        break
                    t = random.choice(alive_now)
                    d = calculate_damage(
                        ms_stats, _enemy_for_damage(t), scaling_hp, scale,
                        mult.damage_type if hasattr(mult, 'damage_type') else "direct",
                        mult.element if hasattr(mult, 'element') else summoner.char.element,
                        80, ms_stats.CRIT_RATE >= 0.5,
                        skill_type="basic" if skill_key == "memsprite_basic" else "skill",
                        true_dmg_ratio=state.realm_true_dmg,
                    crit_mode="expected")
                    total_dmg += d.final_damage
                    from engine.core.combat_sim import _commit_enemy_damage
                    _commit_enemy_damage(
                        state, summoner, t, d.final_damage,
                        cipher_record_amount=(
                            d.final_damage / (1.0 + state.realm_true_dmg)))
                    total_dmg += _apply_tbr_support(state, summoner, t, d.final_damage)
                    from engine.core.combat_sim import _apply_luandie
                    _apply_luandie(state, t)
                continue

            for t in targets:
                d = calculate_damage(
                    ms_stats, _enemy_for_damage(t), scaling_hp, scale,
                    mult.damage_type if hasattr(mult, 'damage_type') else "direct",
                    mult.element if hasattr(mult, 'element') else summoner.char.element,
                    80, ms_stats.CRIT_RATE >= 0.5,
                    skill_type="basic" if skill_key == "memsprite_basic" else "skill",
                    true_dmg_ratio=state.realm_true_dmg,
                crit_mode="expected")
                total_dmg += d.final_damage
                # v5.7: 迷迷的声援逐段触发（E1: 声援效果对该目标的忆灵/忆师也生效）
                total_dmg += _apply_tbr_support(state, summoner, t, d.final_damage)
                # 衣匠: 攻击织线目标→附加12%/30%攻击雷伤 + 自身速度+55点(最多6层)
                if summoner.char.id == 'aglaea' and t.extra.get('gossamer'):
                    # E6: 速度>160/240/320 → 连携攻击伤害+10%/30%/60%
                    e6_mult = 1.0
                    if summoner.eidolon_rank >= 6:
                        spd = summoner.base_stats.SPD
                        if spd > 320:
                            e6_mult = 1.60
                        elif spd > 240:
                            e6_mult = 1.30
                        elif spd > 160:
                            e6_mult = 1.10
                    add_d = calculate_damage(
                        ms_stats, _enemy_for_damage(t), ms_stats.ATK, 30.0 * e6_mult,
                        "direct", "雷", 80, ms_stats.CRIT_RATE >= 0.5,
                        skill_type="basic" if skill_key == "memsprite_basic" else "skill",
                    crit_mode="expected")
                    # E1: 织线目标受伤+15% 已单点于 _enemy_for_damage（v5.7）; 此处保留回能
                    if summoner.eidolon_rank >= 1:
                        from engine.core.combat_sim import _gain_energy
                        _gain_energy(summoner, 20.0, state=state)
                        state.log.append(f'  E1: 攻击织线目标+20能量 ({summoner.current_energy:.0f})')
                    total_dmg += add_d.final_damage
                    from engine.core.combat_sim import _commit_enemy_damage
                    _commit_enemy_damage(state, summoner, t, add_d.final_damage)
                    state.log.append(f'  间隙织线附加: {add_d.final_damage:.0f}(30%ATK×{e6_mult:.2f})')
                    # 忆灵天赋·泪水锻造的匠躯: 速度叠层(E4: 上限+1→7)
                    stack = ms_unit.extra.get('spd_stack', 0)
                    max_stack = 7 if summoner.eidolon_rank >= 4 else 6
                    if stack < max_stack:
                        ms_unit.extra['spd_stack'] = stack + 1
                        state.log.append(f'  衣匠速度叠层+1 ({stack+1}/{max_stack})')
                    # 献予「浪漫」之诗: 衣匠攻击后消耗【浪漫】回70能量（pop 防重复）
                    if summoner.extra.pop('poem_langman', None):
                        from engine.core.combat_sim import _gain_energy
                        _gain_energy(summoner, 70.0, state=state)
                        state.log.append(f'  献予「浪漫」之诗: 衣匠攻击回70能量 ({summoner.current_energy:.0f})')
                from engine.core.combat_sim import _commit_enemy_damage
                _commit_enemy_damage(
                    state, summoner, t, d.final_damage,
                    cipher_record_amount=(
                        d.final_damage / (1.0 + state.realm_true_dmg)))
                from engine.core.combat_sim import _apply_luandie
                _apply_luandie(state, t)

        # v6.11.1 晴歌忆灵技结算: 晴歌+20能量; E1记录→真伤(HP最高敌)后记录减半
        if summoner.char.id == 'robin_summeretto' and skill_key == 'memsprite_basic':
            from engine.core.combat_sim import _gain_energy, _commit_enemy_damage
            _gain_energy(summoner, 20.0, state=state)
            state.log.append(f'  忆灵技: 晴歌+20能量 ({summoner.current_energy:.0f})')
            if summoner.eidolon_rank >= 1:
                record = summoner.extra.get('qingge_record', 0.0)
                # v7.1.0 P2: 目标取忆灵技AoE结算后的存活敌——结算前快照可能已全体阵亡,
                # 此时本次不触发(记录不减半), 不再对尸体提交真伤
                alive_now = [e for e in state.enemies if getattr(e, 'HP', 0) > 0]
                if record > 0 and alive_now:
                    atmo = summoner.extra.get('qingge_atmo', 0.0)
                    true_dmg = record * (0.11 + atmo * 0.001)
                    hp_top = max(alive_now, key=lambda e: e.HP)
                    _commit_enemy_damage(state, summoner, hp_top, true_dmg,
                                         damage_type='true_damage')
                    summoner.extra['qingge_record'] = record * 0.50
                    state.log.append(f'  晴歌E1: 真伤{true_dmg:.0f} → HP最高敌'
                                     f'(记录{record:.0f}×11%+气氛{atmo:.0f}×0.1%), 记录减半')

        # v6.11.1 晴歌天赋: 任意我方忆灵攻击→晴歌气氛+1（特邀嘉宾持有者的召唤物→额外+2）
        # v7.0.0 A4: 晴歌自己的忆灵攻击 via_memsprite=True → E2/律动按忆灵施放技能触发
        if total_dmg > 0:
            from engine.core.combat_sim import _qingge_find, _qingge_on_ally_attack
            if _qingge_find(state) is not None:
                _qingge_on_ally_attack(state, summoner, via_memsprite=True)

        # 献予真我之诗: 花与箭时每1个不同队友来源→额外1次60%HP弹射
        if skill_key == "memsprite_basic" and summoner.char.id == 'xilian':
            from engine.core.combat_sim import AV_PER_TURN, _effective_spd
            sources = summoner.extra.get('zhuiyi_sources', set())
            # E1: 真我之诗触发→+6追忆, 弹射次数+12
            if summoner.eidolon_rank >= 1:
                summoner.zhuiyi = min(27, summoner.zhuiyi + 6)
                state.log.append(f'  昔涟E1: 真我之诗+6追忆 → {summoner.zhuiyi:.0f}/27')
            # E4: 花与箭叠层(0-24), 弹射倍率+6%/层
            if summoner.eidolon_rank >= 4:
                stacks = min(24, summoner.extra.get('xilian_e4_stacks', 0) + 1)
                summoner.extra['xilian_e4_stacks'] = stacks
                state.log.append(f'  昔涟E4: 花与箭叠层+1 → {stacks}/24')
            e4_mult = 60.0 + 6.0 * summoner.extra.get('xilian_e4_stacks', 0)
            bounce_count = len(sources) + (12 if summoner.eidolon_rank >= 1 else 0)
            # E6: 献予触发计数→首次敌DEF-20%, 二次全队拉条24%
            if summoner.eidolon_rank >= 6:
                gift = state.extra.get('xilian_gift_count', 0) + 1
                state.extra['xilian_gift_count'] = gift
                if gift == 1:
                    for e in state.enemies:
                        e.DEF *= 0.80
                    bp = state.extra.get('enemy_blueprint')
                    if bp:
                        bp.DEF *= 0.80  # 同步蓝图, 防波次重生还原
                    state.log.append('  昔涟E6: 献予触发→敌方DEF-20%')
                elif gift == 2:
                    from engine.core.combat_sim import _guest_advance_blocked
                    navs = state.extra.get('navs', {})
                    for i, eu in enumerate(state.units):
                        if eu.is_alive and i in navs \
                                and not _guest_advance_blocked(state, summoner, eu):
                            navs[i] = max(0, navs[i] - (AV_PER_TURN / _effective_spd(eu, state)) * 0.24)
                    state.log.append('  昔涟E6: 献予触发2次→全队拉条24%')
            # v6.2.1: 复用共享逐段管线（Codex P1-2: 此前绕过 _enemy_for_damage/声援/击杀检测）
            for _ in range(bounce_count):
                alive_now = [e for e in alive if e.HP > 0]
                if not alive_now:
                    break
                t = random.choice(alive_now)
                d = calculate_damage(
                    ms_stats, _enemy_for_damage(t), ms_unit.max_hp, e4_mult,
                    "direct", ms_unit.data.element or summoner.char.element,
                    80, ms_stats.CRIT_RATE >= 0.5,
                    skill_type="basic",
                crit_mode="expected")
                total_dmg += d.final_damage
                from engine.core.combat_sim import _commit_enemy_damage
                _commit_enemy_damage(state, summoner, t, d.final_damage)
                total_dmg += _apply_tbr_support(state, summoner, t, d.final_damage)
                from engine.core.combat_sim import _apply_luandie
                _apply_luandie(state, t)
                state.log.append(f'  献予真我之诗弹射: {d.final_damage:.0f}({e4_mult:.0f}%HP)')

        ms_unit.total_damage_dealt += total_dmg
        summoner.total_damage_dealt += total_dmg  # 忆灵伤害计入召唤者
        # v5.0.1: 记录本次命中数；光锥忆灵攻击事件在目标上下文写入后统一派发。
        from engine.core.combat_sim import _process_lc_effects
        state.extra['lc_attack_targets'] = 1
        # v5.2 问题3a: 遗器忆灵攻击事件（英豪4pc 忆灵CD——u=召唤者）
        state.hooks.trigger_all("on_memsprite_attack", u=summoner, state=state,
                                ms_unit=ms_unit)
        _process_lc_effects(summoner, state, "on_memsprite_skill")

        # 迷迷【坏人！麻烦！】后+5%充能（袖珍的事诗）
        if summoner.char.id == 'trailblazer_remembrance' and skill_key == "memsprite_basic":
            ch = self._mimi_charge_gain(state, ms_unit, 5)
            state.log.append(f'  袖珍的事诗: 充能+5% → {ch:.0f}%')

        # 削韧计算
        for eff in skill.effects:
            etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
            if etype != 'toughness_reduction':
                continue
            base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
            efficiency = getattr(ms_stats, 'TOUGHNESS_EFFICIENCY', 1.0)
            # 长夜月E4: 在场时我方忆灵削韧效率+25%; 长夜自身再+25%
            cy_owner = next((x for x in state.units
                             if x.char.id == 'changyeyue' and x.is_alive), None)
            if cy_owner and cy_owner.eidolon_rank >= 4:
                efficiency *= 1.25
                if summoner.char.id == 'changyeyue':
                    efficiency *= 1.25
            toughness_dmg = base_toughness * efficiency
            eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')
            from engine.core.combat_sim import _select_targets

            def _tough_one(t, td):
                nonlocal total_dmg
                break_element = ms_unit.data.element or summoner.char.element
                if t.toughness > 0 and t.max_toughness > 0:
                    t.toughness = max(0, t.toughness - td)
                    if t.toughness <= 0 and not t.is_broken:
                        t.is_broken = True
                        # 击破伤害结算
                        bd = calculate_damage(ms_stats, t, 0, 0, "break", break_element, 80, False)
                        from engine.core.combat_sim import _commit_enemy_damage
                        _commit_enemy_damage(state, summoner, t, bd.final_damage)
                        ms_unit.total_damage_dealt += bd.final_damage
                        total_dmg += bd.final_damage
                        state.log.append(f'  击破弱点! {t.name or t.id} 击破={bd.final_damage:.0f}({break_element})')
                        t.extra['av_delayed'] = 2500.0
                        from engine.core.combat_sim import _apply_break_debuff
                        _apply_break_debuff(t, break_element, summoner, state)
                        # v6.2.1: 目标型击破光锥上下文（Codex P1-3: 缺此键时多敌下命中错目标）
                        state.extra['lc_break_enemy'] = t
                        state.hooks.trigger(summoner.char.id, "on_weakness_break", u=summoner, state=state)
                        # v6.2.1: 对齐 _use_skill 击破管线（Harness P1-5: 忆灵击破漏光锥事件与全队广播）
                        _process_lc_effects(summoner, state, "on_weakness_break")
                        state.hooks.trigger_all("on_any_weakness_break", u=summoner, actor=summoner,
                                                state=state, enemy=t, skill_key=skill_key)
                elif t.is_broken:
                    # v5.0 P7: 忆灵超击破（命中已击破目标按削韧值结算）
                    sbd = calculate_damage(ms_stats, t, 0, 0, "super_break",
                                           break_element, 80, False,
                                           toughness_dmg=td)
                    from engine.core.combat_sim import _commit_enemy_damage
                    _commit_enemy_damage(state, summoner, t, sbd.final_damage)
                    ms_unit.total_damage_dealt += sbd.final_damage
                    total_dmg += sbd.final_damage
                    state.log.append(f'  超击破: {t.name or t.id} {sbd.final_damage:.0f}(削韧{td:.0f})')

            if eff_target == 'bounce':
                # v5.7: 忆灵弹射削韧（坏人麻烦 4跳×5: 每跳均分随机目标）
                _hits = skill.multipliers[0].hits if skill.multipliers else 1
                per_hit = toughness_dmg / _hits
                for _ in range(_hits):
                    _tough_one(random.choice(alive), per_hit)
            else:
                for t in _select_targets(alive, eff_target):
                    _tough_one(t, toughness_dmg)

        # 能量
        ms_unit.current_energy = min(999, ms_unit.current_energy + ENERGY_GAIN.get(skill_key, 0))

        state.log.append(f'[{state.current_av:6.0f}AV] 「{ms_unit.data.name}」{skill.name}: {total_dmg:.0f}')

        # v5.4 光锥忆灵攻击事件（爱如此刻永恒等, 由召唤者LC处理）
        from engine.core.combat_sim import _process_lc_effects
        state.extra['lc_last_memsprite_target'] = getattr(skill, 'target', '')
        _process_lc_effects(summoner, state, "on_memsprite_attack")

        # 追忆后：+1忆质，记录目标（v5.1: E2 每获得忆质+2 经 _gain_yizhi）
        if skill_key == "memsprite_basic":
            # v5.6.1: 忆质由 _use_memsprite_skill 统一入口（行迹3 任意忆灵施放技能+1）覆盖
            if targets:
                summoner.last_target_id = targets[0].id

        # 迷梦后：消耗全部忆质+HP，消失
        if skill_key == "memsprite_skill":
            consumed = yizhi
            # v6.2.1: despawn 前快照忆质用量（despawn 内读取, 清零后不可再读）
            summoner.extra['yizhi_consumed_snapshot'] = consumed
            summoner.yizhi = 0
            # 至暗之谜充能-1
            if summoner.darkness_charges > 0:
                summoner.darkness_charges -= 1
            # E6回收（v5.1: 经 _gain_yizhi, E2 额外+2）
            if summoner.eidolon_rank >= 6:
                recover = int(consumed * 0.30)
                _gain_yizhi(state, summoner, recover)
                state.log.append(f'  长夜月E6: 回收{recover}忆质')
            # 行迹2: 回1SP
            from engine.core.combat_sim import _gain_skill_points
            _gain_skill_points(state)
            self.despawn_memsprite(state, summoner, ms_unit)
            # 至暗之谜退出检查
            if summoner.darkness_charges <= 0 and summoner.is_darkness:
                self._exit_darkness(state, summoner)

    # ── 回合推进 ──

    def _exit_darkness(self, state, unit):
        """v6.2.1: 退出至暗之谜对称还原（Harness P1-2, 此前只清标志→永久漂移）"""
        unit.is_darkness = False
        for e in state.enemies:
            e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.30)
        unit.base_stats.DMG_BONUS_ALL -= 0.60
        if unit.memsprite_unit:
            unit.memsprite_unit.base_stats.DMG_BONUS_ALL -= 0.60
        state.log.append('  退出【至暗之谜】(敌方易伤-30%, 双方增伤-60%)')

    def tick_turn(self, state, unit):
        """回合开始：至暗之谜倒计时、SPD bonus清除、雨过天晴倒计时"""
        # v5.7: 境界倒计时跟随境界主人回合递减（遐蝶遗世冥域3回合;
        # 此前 realm_turns 只赋值不消费=永久, 实机"每回合开始减1"; -1=永久不递减）
        if state.realm_owner and state.realm_turns > 0 and unit.char.id == state.realm_owner:
            state.realm_turns -= 1
            if state.realm_turns <= 0:
                if state.realm_owner == 'xiadie':
                    for e in state.enemies:
                        for elem in e.element_res:
                            e.element_res[elem] += 0.20
                state.realm_owner = ''
                state.realm_turns = 0
                state.realm_true_dmg = 0
                state.log.append('  境界到期解除')
        # v7.2.0: 昔涟结界独立倒计时（无境界技能, 与境界系统解耦）——
        # 跟随昔涟回合递减, 归零解除; -1=涟漪后永久
        if unit.char.id == 'xilian':
            ft = state.extra.get('xilian_field_turns', 0)
            if ft > 0:
                ft -= 1
                state.extra['xilian_field_turns'] = ft
                if ft <= 0:
                    state.realm_true_dmg = 0
                    state.log.append('  昔涟结界到期解除')
        if unit.char.id == "changyeyue":
            # 至暗之谜回合倒计时
            if unit.is_darkness and unit.darkness_charges <= 0:
                self._exit_darkness(state, unit)
            # v6.2.1: 与你再见无期 SPD 加成到期回减（此前只减计数器不回减→永久漂移）
            spd_turns = unit.extra.get('night_spd_bonus_turns', 0)
            if spd_turns > 0:
                spd_turns -= 1
                if spd_turns <= 0:
                    amt = unit.extra.pop('night_spd_bonus_amt', 0.0)
                    if amt > 0:
                        unit.base_stats.SPD -= amt
                        state.log.append(f'  与你再见无期到期: SPD-{amt:.0f}')
                    unit.extra['night_spd_bonus_turns'] = 0
                else:
                    unit.extra['night_spd_bonus_turns'] = spd_turns
        if unit.char.id == "fengjin":
            turns = unit.extra.get('clear_sky_turns', 0)
            if turns > 0:
                turns -= 1
                unit.extra['clear_sky_turns'] = turns
                if turns <= 0:
                    unit.extra['clear_sky_turns'] = 0
                    # v5.7: 退出雨过天晴→全队HP上限回退原值（v6.2.1: 含忆灵）
                    for eu in list(state.units) + list(state.memsprites):
                        orig = eu.extra.pop('clear_sky_orig_maxhp', None)
                        if orig is not None and eu.is_alive:
                            eu.max_hp = orig
                            eu.current_hp = min(orig, eu.current_hp)
                    state.log.append('  退出【雨过天晴】(HP上限回退, 含忆灵)')
            # v6.3.0: 秘技·天气正好 HP 上限加成 2 回合到期回退
            tech_turns = unit.extra.get('tech_maxhp_turns', 0)
            if tech_turns > 0:
                tech_turns -= 1
                unit.extra['tech_maxhp_turns'] = tech_turns
                if tech_turns <= 0:
                    for eu in list(state.units) + list(state.memsprites):
                        orig = eu.extra.pop('tech_orig_maxhp', None)
                        if orig is not None and eu.is_alive:
                            eu.max_hp = orig
                            eu.current_hp = min(eu.max_hp, eu.current_hp)
                    state.log.append('  秘技·天气正好: 全队生命上限回退')

    def get_next_memsprite_av(self, state, current_av):
        """返回最早行动的忆灵 (unit, av)，无则(None, inf)。跳过SPD=0的界外忆灵。"""
        best, best_av = None, float('inf')
        for ms in state.memsprites:
            if not ms.is_alive:
                continue
            if ms.action_spd <= 0:
                continue  # 界外忆灵(SPD=0)，仅通过额外回合行动
            ms_av = ms.extra.get('next_av', current_av + AV_PER_TURN / ms.action_spd)
            if ms_av < best_av:
                best_av, best = ms_av, ms
        return best, best_av

    def _mimi_charge_gain(self, state, ms, gain):
        """迷迷充能统一入口: 满后再次获得充能=拉条(行动提前插队)"""
        old = ms.extra.get('charge', 0)
        new = min(100, old + gain)
        ms.extra['charge'] = new
        if old >= 100 and gain > 0:
            # 充能已满再获充能: 拉条插队（例3规则）
            from engine.core.combat_sim import _set_av, AV_PER_TURN
            key = ('ms', id(ms))
            ms.extra['next_av'] = state.current_av
            state.extra.setdefault('av_stamp', {})
            state.extra['stamp_counter'] = state.extra.get('stamp_counter', 0) + 1
            state.extra['av_stamp'][key] = state.extra['stamp_counter']
            state.log.append(f'  充能已满→迷迷拉条插队!')
        return new

    def tbr_ai(self, u, state, **kw):
        """开拓者·记忆AI: 能量满→终结技(+1史诗); 史诗+迷迷在场→强化普攻; SP>0→战技; 否则普攻"""
        from engine.core.combat_sim import _use_skill
        # 未完的尾声: 持史诗且迷迷在场→普攻强化为明天一同写下
        epic = u.extra.get('tbr_epic', 0)
        has_mimi = u.memsprite_unit and u.memsprite_unit.is_alive
        if u.current_energy >= u.char.max_energy:
            _use_skill(u, state, "ultimate")
            return
        if epic > 0 and has_mimi:
            _use_skill(u, state, "basic_attack_enhanced")
            return
        if state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")

    def aglaea_ai(self, u, state, **kw):
        """阿格莱雅AI: 能量满→终结技(至高之姿); 至高之姿→强化普攻(不能战技); SP>0→战技(召唤/回血衣匠); 否则普攻"""
        from engine.core.combat_sim import _use_skill
        # 同步衣匠速度(阿格莱雅速度变化时衣匠跟随)
        self._aglaea_sync_memsprite(u, state)
        # E2: 行动时无视防御14%×3层
        if u.eidolon_rank >= 2:
            stack = u.extra.get('aglaea_e2_stack', 0)
            if stack < 3:
                u.extra['aglaea_e2_stack'] = stack + 1
                u.base_stats.DEF_PEN += 0.14
                if u.memsprite_unit:
                    u.memsprite_unit.base_stats.DEF_PEN += 0.14
                state.log.append(f'  E2: 无视防御+14% ({stack+1}/3)')
        # 至高之姿: 普攻强化为孤锋千吻，无法施放战技
        if u.is_sovereign:
            _use_skill(u, state, "basic_attack_enhanced")
            return
        if u.current_energy >= u.char.max_energy:
            _use_skill(u, state, "ultimate")
            return
        if state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")

    def _aglaea_sync_memsprite(self, u, state):
        """衣匠速度=阿格莱雅35%(动态同步)"""
        ms = u.memsprite_unit
        if not ms or u.char.id != 'aglaea':
            return
        from engine.core.combat_sim import _effective_spd
        base_spd = _effective_spd(u, state) * 0.35
        # 加上忆灵天赋速度叠层(每层55点)
        stack = ms.extra.get('spd_stack', 0)
        ms.base_stats.SPD = base_spd + stack * 55
        ms.runtime_spd = ms.base_stats.SPD  # v5.2: 写运行时字段, 不污染 MemSprite 配置

    def _qingge_summon_variant(self, state, summoner, ms_data, name):
        """晴歌专用: 召唤/维护唯一「晴空乐手」忆灵实体。
        战技路径(贝茜档): 实体已在场→回血100%晴空乐手HP上限(Lv10)+晴歌气氛+6, 不重复召唤。
        首次召唤: 创建唯一实体(成员档位1)。
        v7.1.0 项目主澄清: 三只忆灵仅表示角色状态, 实机按一只忆灵计算——
        啾米/派丁登台是成员档位切换(combat_sim._qingge_check_variant_spawn), 不再创建新实体。"""
        import copy as _copy
        from engine.core.combat_sim import (_gain_energy, _qingge_gain_atmo,
                                            _qingge_check_variant_spawn,
                                            _qingge_check_fever,
                                            _qingge_refresh_fever_effects)
        existing = next((m for m in state.memsprites
                         if m.summoner_id == 'robin_summeretto' and m.is_alive), None)
        if existing is not None:
            t = existing
            # v7.0.0 A3: E3战技+2→Lv12 每级+5%惯例消费
            from engine.core.combat_sim import _skill_level_factor
            heal = t.max_hp * 1.0 * _skill_level_factor(summoner, 'skill')
            t.current_hp = min(t.max_hp, t.current_hp + heal)
            _qingge_gain_atmo(state, 6.0, cause='战技·晴空乐手已在场')
            state.log.append(f'  战技: {t.data.name}已在场→回血{heal:.0f} '
                             f'(HP={t.current_hp:.0f}/{t.max_hp:.0f}) + 晴歌气氛+6')
            return t
        data = _copy.deepcopy(ms_data)
        data.name = '晴空乐手'
        ms_stats = _copy.deepcopy(summoner.base_stats)
        ms_stats.HP = summoner.base_stats.HP * 0.70
        ms_stats.SPD = summoner.base_stats.SPD * 1.80
        ms_stats.CRIT_RATE += 0.50  # 行迹1·重构谐乐: 晴空乐手CR+50%
        ms_unit = MemSpriteUnit(
            data=data, summoner_id=summoner.char.id,
            max_hp=ms_stats.HP, current_hp=ms_stats.HP,
            base_stats=ms_stats,
        )
        ms_unit.current_energy = 0
        ms_unit.runtime_spd = 0.0  # Fever前不在行动条(界外), 进Fever时激活
        ms_unit.extra['qingge_members'] = 1  # 成员档位1=贝茜
        state.memsprites.append(ms_unit)
        summoner.memsprite_unit = ms_unit
        # 忆灵天赋·贴近海的心跳: 被召唤→晴歌+20能量
        _gain_energy(summoner, 20.0, state=state)
        state.log.append(f'  召唤「晴空乐手」贝茜 HP={ms_stats.HP:.0f} (晴歌HP×70%)'
                         f' + 贴近海的心跳: 晴歌+20能量')
        state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                summoner=summoner, ms_unit=ms_unit)
        # 首次入场时已攒的气氛可能已达升档阈值→升档(可能直接全员登台进Fever)
        _qingge_check_variant_spawn(state, summoner)
        _qingge_check_fever(state, summoner)
        # 成员数易伤/Fever动态效果随档位变化刷新
        _qingge_refresh_fever_effects(state)
        return ms_unit

    def qingge_ai(self, u, state, **kw):
        """晴歌AI: Fever期不进自己回合(行动条已摘除, 保险跳过); 满能量终结技由phase-1拦截;
        SP>0→战技(召唤贝茜/在场回血+气氛), SP=0→普攻。"""
        from engine.core.combat_sim import _use_skill
        if u.extra.get('qingge_fever'):
            return
        if state.skill_points > 0:
            _use_skill(u, state, 'skill')
        else:
            _use_skill(u, state, 'basic_attack')

    def fengjin_ai(self, u, state, **kw):
        """风堇AI: 战技流（用户确认: 实机基本不释放普攻）——SP>0→战技(治疗), SP=0→普攻。
        终结技由 phase-1 拦截入 X 轴队列（雨过天晴在 _ult_post 处理）。
        雨过天晴(3回合): 每次行动后小伊卡额外回合入队→乌云乌云+天赋追加治疗"""
        from engine.core.combat_sim import _use_skill
        if state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")
        self._fengjin_extra_turn(state, u)

    def _fengjin_extra_turn(self, state, u):
        """雨过天晴: 小伊卡额外回合入 X 轴队列（治疗在 X 轴执行时进行, v6.2.1 拆分防双份）"""
        if u.extra.get('clear_sky_turns', 0) <= 0:
            return
        ms = u.memsprite_unit
        if not ms or not ms.is_alive:
            return
        # 入 X 轴队列（避免重复入队）; 天赋追加治疗由 X 轴执行处统一结算
        # v6.2.1: 此前此处即奶一次 + X 轴执行再奶一次 = 双份（Harness P2-1）
        if not any(x is ms for x, k in state.extra.get('extra_turns', [])):
            state.extra.setdefault('extra_turns', []).append((ms, 'extra'))
            state.log.append(f'  小伊卡额外回合入队')

    def _xilian_sync_memsprite_hp(self, u):
        """德谬歌HP同步: 昔涟HP%变化→德谬歌HP%同步（等待，在所有的过去）"""
        ms = u.memsprite_unit
        if not ms or not ms.is_alive or u.char.id != 'xilian':
            return
        if u.max_hp <= 0 or ms.max_hp <= 0:
            return
        ms.current_hp = ms.max_hp * (u.current_hp / u.max_hp)

    def _xilian_support_skill(self, state, summoner, ms_unit):
        """此诗，献予一切生命: 非黄金裔→伤害+40%/2回合(对忆灵生效)。黄金裔→触发专属献予诗"""
        from engine.core.character_utils import is_gold_offspring
        from engine.core.combat_sim import TimedBuff

        target = _select_xilian_target(state)
        if not target:
            return

        # 黄金裔: 触发专属献予诗（未录入角色→占位标记, 等角色录入后激活）
        if is_gold_offspring(target):
            fn = POEM_EFFECTS.get(target.char.id)
            if fn:
                fn(state, summoner, ms_unit, target)
                self._record_xilian_e2_gift(state, target)
            else:
                _poem_placeholder(state, target)
            summoner.last_target_id = target.char.id
            return

        # 非黄金裔: 伤害+40%/2回合（该效果对其忆灵也生效）
        tb = TimedBuff(source_id=summoner.char.id, attributes={"DMG_BONUS_ALL": 40.0},
                       remaining_turns=2, source_name="此诗，献予一切生命")
        target.buffs.append(tb)
        if target.memsprite_unit and target.memsprite_unit.is_alive:
            ms_tb = TimedBuff(source_id=summoner.char.id, attributes={"DMG_BONUS_ALL": 40.0},
                              remaining_turns=2, source_name="此诗，献予一切生命")
            target.memsprite_unit.buffs.append(ms_tb)
        self._record_xilian_e2_gift(state, target)
        summoner.last_target_id = target.char.id
        state.log.append(f'  此诗献予一切生命: {target.char.name}+40%伤害(2回合)')

    @staticmethod
    def _record_xilian_e2_gift(state, target):
        """仅在德谬歌确实施加可消费增益后记录昔涟E2角色数。"""
        xilian = next((x for x in state.units if x.char.id == 'xilian' and x.is_alive), None)
        if not xilian or xilian.eidolon_rank < 2:
            return
        gifted = state.extra.setdefault('xilian_e2_gifted', set())
        if target.char.id in gifted:
            return
        gifted.add(target.char.id)
        if state.extra.get('xilian_field_turns'):  # v7.2.0: 结界独立判定(无境界系统)
            state.realm_true_dmg = min(0.48, 0.24 + 0.06 * len(gifted))
            state.log.append(f'  昔涟E2: 获增益角色+1({target.char.name})→结界真伤{state.realm_true_dmg:.2f}')

    def xilian_ai(self, u, state, **kw):
        """昔涟AI: 常态→战技(+3追忆),≥24→终结技; 涟漪→强化普攻,≥12→一如初见"""
        from engine.core.combat_sim import _use_skill
        # HP同步: 昔涟HP%变化→德谬歌同步
        self._xilian_sync_memsprite_hp(u)
        # 涟漪态（实机: 常规回合只能释放向着爱与明天;
        # 忆灵技释放类似终结技——追忆≥12 随时经 X 轴触发, 不走常规回合）
        if u.is_ripple:
            _use_skill(u, state, "basic_attack_enhanced")
            return
        # 常态: 优先终结技(单场1次)，SP不足时普攻(+1SP)
        if u.zhuiyi >= 24 and not u.extra.get('xilian_ult_used'):
            _use_skill(u, state, "ultimate")
            return
        if state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")
        # 战技后给队友上未来
        for eu in state.units:
            if eu.is_alive and eu.char.id != 'xilian':
                eu.has_future = True

    def _tbr_support_skill(self, state, summoner, ms_unit):
        """我会！帮你！: 指定我方单体行动提前100% + 【迷迷的声援】3回合。
        声援: 每造成1次伤害→额外28%真伤。对自身施放不触发行动提前。"""
        from engine.core.combat_sim import TimedBuff
        # 选目标: 优先主C(非自己)，否则自己
        targets = [eu for eu in state.units if eu.is_alive and eu.char.id != 'trailblazer_remembrance']
        if not targets:
            targets = [eu for eu in state.units if eu.is_alive]
        if not targets:
            return
        target = targets[0]
        # 声援3回合
        attrs = {"_tbr_support": 1}
        # v5.7 E1: 持有声援者暴击率+10%, 且声援效果对该目标的忆灵/忆师也生效
        if summoner.eidolon_rank >= 1:
            attrs["CRIT_RATE"] = 10.0
        tb = TimedBuff(source_id=summoner.char.id,
                       attributes=attrs, remaining_turns=3,
                       source_name="迷迷的声援")
        target.buffs.append(tb)
        if summoner.eidolon_rank >= 1 and target.memsprite_unit \
                and target.memsprite_unit.is_alive:
            target.memsprite_unit.buffs.append(TimedBuff(
                source_id=summoner.char.id,
                attributes={"CRIT_RATE": 10.0, "_tbr_support": 1}, remaining_turns=3,
                source_name="迷迷的声援(E1对忆灵)"))
        # 行动提前100%（非自身）
        if target.char.id != 'trailblazer_remembrance':
            from engine.core.combat_sim import _guest_advance_blocked
            navs = state.extra.get('navs', {})
            for i, eu in enumerate(state.units):
                if eu is target and i in navs \
                        and not _guest_advance_blocked(state, summoner, eu):
                    navs[i] = state.current_av
                    break
        # 充能清零（100%已消耗）
        ms_unit.extra['charge'] = 0
        state.log.append(f'  我会！帮你！: {target.char.name}行动提前100%+【迷迷的声援】3回合')

    def _tbr_memsprite_ai(self, state, summoner, ms_unit):
        """迷迷行动: 充能<100%→坏人麻烦; 充能100%→我会帮你"""
        from engine.core.combat_sim import _use_skill
        charge = ms_unit.extra.get('charge', 0)
        if charge >= 100:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_support")
        else:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")

    def _xilian_memsprite_action(self, state, summoner, ms_unit):
        """德谬歌行动（实机: 玩家选择释放 花与箭/此诗献予, **每次选择释放扣12点追忆**,
        追忆不足12时玩家可不释放）:
        AI 近似——追忆≥12 时: 诗篇目标存在→【此诗献予】(优先级数据驱动: 未获诗黄金裔
        整局→单次, 见 POEM_PERSISTENT/_select_xilian_target), 否则【花与箭】; 扣12追忆。
        此诗献予不硬编码进昔涟AI（不同队伍选择原则有变化, 由诗表数据决定）。
        献予真我之诗: 故事≥3 → 消耗全部→额外回合自动花与箭（不扣追忆, 实机文本）。"""
        # 献予真我之诗: 故事≥3 → 额外回合自动花与箭（优先于选择释放, 不扣追忆）
        if summoner.story_points >= 3:
            summoner.story_points = 0
            state.log.append('  献予真我之诗: 故事满3→额外回合+花与箭')
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")
            return
        if summoner.zhuiyi < 12:
            state.log.append(f'  德谬歌待机: 追忆{summoner.zhuiyi:.0f}<12, 暂不选择释放')
            return
        summoner.zhuiyi -= 12
        state.log.append(f'  追忆-12 → {summoner.zhuiyi:.0f}/27')
        target = _select_xilian_target(state)
        if target is not None:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_support")
        else:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")

    def xiadie_ai(self, u, state, **kw):
        """遐蝶AI：新蕊<上限→战技(HP消耗,不耗SP)；≥上限→终结技(召唤死龙→焰息→引爆)
        v7.2.0 裁决A: 姬子·启行在场(拓星视界占境界)→终结技永封, 回落战技攒新蕊"""
        from engine.core.combat_sim import _use_skill, _hn_realm_blocks_ult, xiadie_xinrui_cap
        if u.memsprite_unit and u.memsprite_unit.is_alive:
            # 遐蝶E2: 召唤后的下次强化战技+30%新蕊(一次性)
            if u.extra.pop('xiadie_e2_skill_pending', False):
                cap = xiadie_xinrui_cap(u)
                u.xinrui = min(cap, u.xinrui + cap * 0.30)
                state.log.append(f'  遐蝶E2: 强化战技+30%新蕊 → {u.xinrui:.0f}/{cap:.0f}')
            _use_skill(u, state, "skill_dragon")
            return
        if u.xinrui >= xiadie_xinrui_cap(u) and not _hn_realm_blocks_ult(state, u):
            from engine.core.combat_sim import _use_skill
            _use_skill(u, state, "ultimate")
            return
        from engine.core.combat_sim import _use_skill
        _use_skill(u, state, "skill")

    def handle_memsprite_action(self, state, ms_unit, regular_turn=True):
        """处理忆灵在行动条上的行动"""
        summoner = next((u for u in state.units if u.char.id == ms_unit.summoner_id), None)
        if not summoner or not ms_unit.is_alive:
            return
        if regular_turn:
            from engine.core.combat_sim import _tick_buffs
            _tick_buffs(ms_unit)
        # 死龙Y轴行动: 每次行动喷吐一次(倍率递增24→28→34→34), HP≤25%→自爆
        if ms_unit.data.name == '死龙' and summoner.char.id == 'xiadie':
            self._dragon_flame_once(state, summoner, ms_unit)
            return
        # E2: 衣匠行动也叠无视防御层
        if summoner.char.id == 'aglaea' and summoner.eidolon_rank >= 2:
            stack = summoner.extra.get('aglaea_e2_stack', 0)
            if stack < 3:
                summoner.extra['aglaea_e2_stack'] = stack + 1
                summoner.base_stats.DEF_PEN += 0.14
                ms_unit.base_stats.DEF_PEN += 0.14
                state.log.append(f'  E2(衣匠): 无视防御+14% ({stack+1}/3)')
        # 衣匠倒计时: 至高之姿期间回合开始→倒计时减1，归零→衣匠自毁
        if summoner.char.id == 'aglaea' and summoner.is_sovereign:
            countdown = summoner.extra.get('countdown_turns', 0)
            if countdown > 0:
                countdown -= 1
                summoner.extra['countdown_turns'] = countdown
                state.log.append(f'  至高之姿倒计时: {countdown}')
                if countdown <= 0:
                    state.log.append('  倒计时归零→衣匠自毁')
                    self.despawn_memsprite(state, summoner, ms_unit, reason="countdown")
                    return
        # 更新忆灵AV（死龙通常由主循环先更新后行动；保留此分支供直调入口）。
        spd = ms_unit.action_spd
        if summoner.char.id == 'xiadie':
            if summoner.current_hp >= summoner.max_hp * 0.5:
                spd *= 1.4
            if ms_unit.extra.get('xiadie_spd_boost'):
                spd *= 2.0
                ms_unit.extra['xiadie_spd_boost'] = 0  # 1回合后消耗
        ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / max(spd, 1.0)
        from engine.core.combat_sim import _stamp_av_key
        _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳（额外回合路径不经主循环 _set_av）
        self._memsprite_ai(state, summoner, ms_unit)
