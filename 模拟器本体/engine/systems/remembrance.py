"""记忆命途子系统 — 忆灵召唤/控制/消失 + 忆质/至暗之谜管理

仅在队伍包含记忆命途角色时由 simulate() 条件激活。
"""
import copy
import random
from dataclasses import dataclass, field
from engine.models.memsprite import MemSprite
from engine.core.damage import calculate_damage
from engine.hooks.base import HookRegistry
from engine.runtime import AV_PER_TURN, ENERGY_GAIN
from engine.core.combat_engine import (_char_phase, _ensure_phase_tables, _obs_phase,
                                   _apply_break_debuff, _apply_stat, _build_effective_stats, _commit_enemy_damage, _effective_spd, _gain_energy, _gain_skill_points, _record_enemy_kill, _skill_level_factor, _tick_buffs)
from engine.runtime import TimedBuff, _enemy_for_damage, _select_targets, _set_av, _stamp_av_key
from engine.core.character_utils import count_remembrance, is_gold_offspring


def _gain_yizhi(state, u, amt):
    """忆质获得统一入口（v5.1: 长夜月E2 每获得忆质额外+2）
    v5.7: 跨越≥16时触发天赋——解除自身控制 + 「长夜」立即行动（迷梦后可再次触发）"""
    old = u.yizhi
    if u.eidolon_rank >= 2:
        amt += 2
    u.yizhi += amt
    # v7.16.0 相位 yizhi_gain: 长夜月忆质跨16天赋（守卫在处理器内）
    _ensure_phase_tables(state)
    _char_phase(state, u, 'yizhi_gain', old=old)
    return amt


def _team_memsprite_def_pen(state) -> float:
    """Resolve the strongest active team-wide memsprite DEF penetration effect."""

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
# 通用净化助手（诗篇/召唤共用; 诗篇本体已迁角色包 POEM 导出）

def _cleanse_controls(state):
    """v5.7: 解除我方全体控制类负面状态（昔涟忆灵天赋·你好世界; 通用净化复用）"""
    for eu in state.units:
        if not eu.is_alive:
            continue
        removed = [s for s in eu.statuses if getattr(s, 'category', '') == 'control']
        for s in removed:
            eu.statuses.remove(s)
            state.log.append(f'  净化: {eu.char.name}解除{s.name}')


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


def _exit_darkness(state, unit):
    """v6.2.1: 退出至暗之谜对称还原（Harness P1-2, 此前只清标志→永久漂移）"""
    unit.is_darkness = False
    for e in state.enemies:
        e.vulnerability = max(0.0, getattr(e, 'vulnerability', 0.0) - 0.30)
    unit.base_stats.DMG_BONUS_ALL -= 0.60
    if unit.memsprite_unit:
        unit.memsprite_unit.base_stats.DMG_BONUS_ALL -= 0.60
    state.log.append('  退出【至暗之谜】(敌方易伤-30%, 双方增伤-60%)')


def _dispatch_memsprite_support_events(state, summoner, skill):
    """Dispatch post-action events for non-damaging memsprite support skills."""
    from engine.core.combat_engine import _process_lc_effects

    state.extra['lc_attack_targets'] = 0
    state.extra['lc_last_memsprite_target'] = getattr(skill, 'target', '')
    _process_lc_effects(summoner, state, "on_memsprite_attack")
    state.hooks.trigger_all("on_memsprite_attack", u=summoner, state=state)
    _process_lc_effects(summoner, state, "on_memsprite_skill")


class RemembranceSystem:
    """记忆命途子系统协调器"""

    def __init__(self):
        self.suppressed_actions: dict = {}  # {char_id: turns_left}

    # ── 初始化 ──

    def init_battle(self, state, units):
        """战斗开始：处理各记忆角色天赋"""
        _ensure_phase_tables(state)
        for u in units:
            if u.char.path != "记忆":
                continue
            # v7.16.0 相位 rem_init: 记忆角色开局天赋（长夜月自动召唤; 遐蝶由终结技触发）
            _char_phase(state, u, 'rem_init')

    # ── 忆灵生命周期 ──

    def summon_memsprite(self, state, summoner, ms_data: MemSprite, hp_override=None):
        """召唤忆灵（若已在场则回血，否则创建）
        hp_override: v6.3.0 遐蝶秘技——死龙 HP=新蕊上限50%（非终结技的 34000 满额）"""
        existing = next((m for m in state.memsprites
                         if m.data.name == ms_data.name and m.summoner_id == summoner.char.id), None)
        _ensure_phase_tables(state)
        if existing:
            # v7.16.0 相位 ms_reheal_skip: 已在场回血豁免（阿格莱雅走 JSON heal, 
            # v5.7 语义: 通用分支再回50%会与 heal effect 叠加成100%）
            if not _char_phase(state, summoner, 'ms_reheal_skip'):
                existing.current_hp = min(existing.max_hp, existing.current_hp + existing.max_hp * 0.50)
                state.log.append(f'  {ms_data.name}已在场→回血50% (HP={existing.current_hp:.0f}/{existing.max_hp:.0f})')
            return existing

        # v7.16.0 相位 ms_build: 角色专属忆灵构建（→ms_unit|None; xiadie 死龙/tbr 迷迷/aglaea 衣匠/robin_summeretto 晴空乐手）
        built = _char_phase(state, summoner, 'ms_build', ms_data=ms_data,
                            hp_override=hp_override)
        if built is not None:
            return built

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

        # v7.16.0 相位 ms_stats_premod: 构建前面板预调（长夜月E2 双方暴伤）
        _char_phase(state, summoner, 'ms_stats_premod', ms_stats=ms_stats)

        ms_unit = MemSpriteUnit(
            data=ms_data,
            summoner_id=summoner.char.id,
            max_hp=ms_stats.HP,
            current_hp=ms_stats.HP,
            base_stats=ms_stats,
        )
        ms_unit.current_energy = 0

        # v7.16.0 相位 ms_created: 创建后角色结算（长夜月漆黑/昔涟HP×1.24/风堇展翼）
        _char_phase(state, summoner, 'ms_created', ms_unit=ms_unit)

        state.memsprites.append(ms_unit)
        summoner.memsprite_unit = ms_unit
        state.log.append(f'  召唤忆灵「{ms_data.name}」HP={ms_stats.HP:.0f} SPD={ms_data.base_SPD}')

        # 与夜，形影，不离：立即行动（行动后设置下次行动值——
        # 若不写 next_av，候选生成会按"当前时刻+AV/SPD"重算，与召唤者同速时
        # 永远同 AV 且 stamp 落后 → 忆灵行动被饿死）
        self._force_memsprite_action(state, summoner, ms_unit)
        ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / max(ms_unit.action_spd, 1.0)
        _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
        state.hooks.trigger_all("on_memsprite_summon", u=summoner, state=state,
                                 summoner=summoner, ms_unit=ms_unit)
        return ms_unit

    def despawn_memsprite(self, state, summoner, ms_unit, reason: str = ""):
        """忆灵消失，触发 on_despawn 效果"""
        _ensure_phase_tables(state)
        if ms_unit not in state.memsprites:
            return
        ms_name = ms_unit.data.name
        lost_hp = ms_unit.current_hp  # 消失前剩余HP
        state.memsprites.remove(ms_unit)
        summoner.memsprite_unit = None

        # v5.4 光锥忆灵消失事件（致长夜的星光: 任意我方忆灵消失→持有者回8能量）
        from engine.core.combat_engine import _process_lc_effects
        for unit in state.units:
            if unit.is_alive:
                _process_lc_effects(unit, state, "on_memsprite_despawn")

        # v7.16.0 相位 ms_despawn_absorb: 消失剩余HP吸收（遐蝶新蕊; 死龙除外守卫在处理器内）
        _char_phase(state, summoner, 'ms_despawn_absorb', lost_hp=lost_hp, ms_name=ms_name)

        # v7.16.0 相位 ms_despawn_settle: 消失结算（→True=已处理; 长夜月/阿格莱雅/开拓者/风堇, None→通用"消失"日志）
        if not _char_phase(state, summoner, 'ms_despawn_settle',
                           ms_unit=ms_unit, ms_name=ms_name):
            state.log.append(f'  {ms_name}消失')

    def _force_memsprite_action(self, state, summoner, ms_unit):
        """忆灵立即行动"""
        state.log.append(f'  「{ms_unit.data.name}」立即行动!')
        # v6.2.1: 行动后重写排程（Harness P2-2: 旧 next_av 残留→按原排程再动一次双行动）
        if ms_unit.action_spd > 0:
            ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / ms_unit.action_spd
            _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳
        self._memsprite_ai(state, summoner, ms_unit)

    # ── 忆灵AI ──

    def _memsprite_ai(self, state, summoner, ms_unit):
        """忆灵AI：迷迷按充能调度; 长夜优先迷梦(yizhi≥16) > 普攻"""
        if not ms_unit.is_alive:
            return
        _ensure_phase_tables(state)
        # v7.16.0 相位 ms_ai: 角色专属忆灵调度（→True=已调度; 迷迷/德谬歌/晴空乐手）
        if _char_phase(state, summoner, 'ms_ai', ms_unit=ms_unit):
            return
        # 检查是否可以放迷梦: yizhi≥16 且召唤者不处于控制状态
        can_mimeng = summoner.yizhi >= 16

        if can_mimeng and "memsprite_skill" in ms_unit.data.skills:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_skill")
        elif "memsprite_basic" in ms_unit.data.skills:
            self._use_memsprite_skill(state, summoner, ms_unit, "memsprite_basic")

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
        """v6.2.1: 忆灵伤害击杀检测（Harness P1-5: 对齐死龙/乌黯击杀口径与技能管线）"""
        if before_hp > 0 and t.HP <= 0:
            state.extra['killed_this_action'] = state.extra.get('killed_this_action', 0) + 1
            from engine.core.combat_engine import _process_lc_effects
            _record_enemy_kill(state)
            state.hooks.trigger(summoner.char.id, "on_kill", u=summoner, state=state, enemy=t)
            from engine.core.combat_engine import _process_lc_effects
            _process_lc_effects(summoner, state, "on_kill")

    @staticmethod
    def _decrement_memsprite_skill_buffs(state, summoner, ms_unit):
        """忆灵技能结算后，自身有限持续效果减少一回合（v7.16.0 相位 ms_buff_tick:
        昔涟/风堇注册, 守卫在处理器内）。"""
        _ensure_phase_tables(state)
        _char_phase(state, summoner, 'ms_buff_tick', ms_unit=ms_unit)

    def _use_memsprite_skill_inner(self, state, summoner, ms_unit, skill_key, skill):
        """忆灵使用技能（主体, v5.7 拆出以包 _ms_acting 防护标记）"""
        from engine.characters.trailblazer_remembrance import _apply_tbr_support

        _ensure_phase_tables(state)
        # v7.16.0 观察相位 ms_cast_cy_tick: 任意我方忆灵施技→长夜月+5能+1忆质
        _obs_phase(state, 'ms_cast_cy_tick', summoner)

        # v7.16.0 相位 ms_cast_xilian: 德谬歌施技→风堇天空层+2
        _char_phase(state, summoner, 'ms_cast_xilian')

        # v7.16.0 相位 ms_support_cast: 无倍率辅助技（→True=已处理; 昔涟此诗献予/迷迷我会帮你）
        if not skill.multipliers and _char_phase(state, summoner, 'ms_support_cast',
                                                 ms_unit=ms_unit, skill=skill):
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
            # v7.16.0 相位 ms_default_target: 单体目标角色偏好（→[t]|None; 阿格莱雅织线）
            _dt = _char_phase(state, summoner, 'ms_default_target', alive=alive)
            if _dt is not None:
                default_targets = _dt
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

        # v7.16.0 观察相位 ms_scale_cy_mult: 长夜月E1 敌方数→忆灵伤害乘区
        cy_mult = _obs_phase(state, 'ms_scale_cy_mult', summoner, alive=alive)
        if cy_mult is None:
            cy_mult = 1.0

        # 长夜月的战技/秘技 buff 存在于角色身上，忆灵必须在伤害结算时
        # 显式取得该暴伤；仅战技 buff 会额外启用行迹1。
        # v5.6.1: 基础 = 忆灵有效面板（含暂存 buff）
        ms_stats = _ms_effective_stats(ms_unit, state)
        # v5.4 忆灵面板 LC 修正：已激活的团队效果作用于所有我方忆灵。
        ms_memsp = _team_memsprite_def_pen(state)
        if ms_memsp:
            ms_stats = copy.deepcopy(ms_stats)
            ms_stats.DEF_PEN += ms_memsp
        # v7.16.0 观察相位 ms_cy_stats_mod: 长夜月 cd buff 注入+行迹1（→ms_stats）
        _ms2 = _obs_phase(state, 'ms_cy_stats_mod', summoner, ms_stats=ms_stats)
        if _ms2 is not None:
            ms_stats = _ms2

        for mult in skill.multipliers:
            scaling_hp = ms_stats.HP if hasattr(ms_stats, 'HP') else ms_unit.max_hp
            scale = mult.scale if hasattr(mult, 'scale') else 0
            hits = mult.hits  # v5.3: 解析器已支持 _hits 字段
            # v5.7: 逐倍率目标（衣匠刺纹之陷主110%/相邻66%、迷梦主12%/其他6%）
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
            # v7.16.0 相位 ms_scale_mod: 循环内倍率覆写（→新scale|None; 晴歌E5/E6、岁月之诗）
            _sc = _char_phase(state, summoner, 'ms_scale_mod', skill_key=skill_key,
                              scale=scale)
            if _sc is not None:
                scale = _sc

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
                    _commit_enemy_damage(
                        state, summoner, t, d.final_damage,
                        cipher_record_amount=(
                            d.final_damage / (1.0 + state.realm_true_dmg)))
                    total_dmg += _apply_tbr_support(state, summoner, t, d.final_damage)
                    from engine.characters.seele import _apply_luandie
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
                # v7.16.0 相位 ms_target_hit_bonus: 命中角色附加（→dmg|None; 衣匠织线附加）
                _add = _char_phase(state, summoner, 'ms_target_hit_bonus',
                                   ms_unit=ms_unit, ms_stats=ms_stats, t=t,
                                   skill_key=skill_key)
                if _add is not None:
                    total_dmg += _add
                _commit_enemy_damage(
                    state, summoner, t, d.final_damage,
                    cipher_record_amount=(
                        d.final_damage / (1.0 + state.realm_true_dmg)))
                from engine.characters.seele import _apply_luandie
                _apply_luandie(state, t)

        # v7.16.0 相位 ms_post_settle: 技能循环后角色结算（晴歌+20能/E1 真伤）
        _char_phase(state, summoner, 'ms_post_settle', skill_key=skill_key)

        # v6.11.1 晴歌天赋: 任意我方忆灵攻击→晴歌气氛+1（特邀嘉宾持有者的召唤物→额外+2）
        # v7.0.0 A4: 晴歌自己的忆灵攻击 via_memsprite=True → E2/律动按忆灵施放技能触发
        if total_dmg > 0:
            from engine.characters.robin_summeretto import (_qingge_find,
                                                           _qingge_on_ally_attack)
            if _qingge_find(state) is not None:
                _qingge_on_ally_attack(state, summoner, via_memsprite=True)

        # v7.16.0 相位 ms_bounce_extra: 追加弹射（→dmg|None; 昔涟献予真我之诗）
        _be = _char_phase(state, summoner, 'ms_bounce_extra', ms_unit=ms_unit,
                          ms_stats=ms_stats, alive=alive, skill_key=skill_key)
        if _be is not None:
            total_dmg += _be

        ms_unit.total_damage_dealt += total_dmg
        summoner.total_damage_dealt += total_dmg  # 忆灵伤害计入召唤者
        # v5.0.1: 记录本次命中数；光锥忆灵攻击事件在目标上下文写入后统一派发。
        from engine.core.combat_engine import _process_lc_effects
        state.extra['lc_attack_targets'] = 1
        # v5.2 问题3a: 遗器忆灵攻击事件（英豪4pc 忆灵CD——u=召唤者）
        state.hooks.trigger_all("on_memsprite_attack", u=summoner, state=state,
                                ms_unit=ms_unit)
        _process_lc_effects(summoner, state, "on_memsprite_skill")

        # v7.16.0 相位 ms_after_attack: 攻击后角色结算（迷迷袖珍的事诗+5%充能）
        _char_phase(state, summoner, 'ms_after_attack', ms_unit=ms_unit,
                    skill_key=skill_key)

        # 削韧计算
        for eff in skill.effects:
            etype = eff.type if hasattr(eff, 'type') else eff.get('type', '')
            if etype != 'toughness_reduction':
                continue
            base_toughness = eff.value if hasattr(eff, 'value') else eff.get('value', 0)
            efficiency = getattr(ms_stats, 'TOUGHNESS_EFFICIENCY', 1.0)
            # v7.16.0 观察相位 ms_tough_eff: 长夜月E4 忆灵削韧乘区（施法者=长夜再×1.25）
            _te = _obs_phase(state, 'ms_tough_eff', summoner)
            if _te is not None:
                efficiency *= _te
            toughness_dmg = base_toughness * efficiency
            eff_target = eff.target if hasattr(eff, 'target') else eff.get('target', 'single_enemy')

            def _tough_one(t, td):
                nonlocal total_dmg
                break_element = ms_unit.data.element or summoner.char.element
                if t.toughness > 0 and t.max_toughness > 0:
                    t.toughness = max(0, t.toughness - td)
                    if t.toughness <= 0 and not t.is_broken:
                        t.is_broken = True
                        # 击破伤害结算
                        bd = calculate_damage(ms_stats, t, 0, 0, "break", break_element, 80, False)
                        _commit_enemy_damage(state, summoner, t, bd.final_damage)
                        ms_unit.total_damage_dealt += bd.final_damage
                        total_dmg += bd.final_damage
                        state.log.append(f'  击破弱点! {t.name or t.id} 击破={bd.final_damage:.0f}({break_element})')
                        t.extra['av_delayed'] = 2500.0
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
        from engine.core.combat_engine import _process_lc_effects
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
            _gain_skill_points(state)
            self.despawn_memsprite(state, summoner, ms_unit)
            # 至暗之谜退出检查
            if summoner.darkness_charges <= 0 and summoner.is_darkness:
                _exit_darkness(state, summoner)

    # ── 回合推进 ──

    def tick_turn(self, state, unit):
        """回合开始：至暗之谜倒计时、SPD bonus清除、雨过天晴倒计时"""
        _ensure_phase_tables(state)
        # v5.7: 境界倒计时跟随境界主人回合递减（遐蝶遗世冥域3回合;
        # 此前 realm_turns 只赋值不消费=永久, 实机"每回合开始减1"; -1=永久不递减）
        if state.realm_owner and state.realm_turns > 0 and unit.char.id == state.realm_owner:
            state.realm_turns -= 1
            if state.realm_turns <= 0:
                # v7.16.0 相位 realm_expire: 境界到期角色专属解除（遐蝶抗性回退）
                _char_phase(state, unit, 'realm_expire')
                state.realm_owner = ''
                state.realm_turns = 0
                state.realm_true_dmg = 0
                state.log.append('  境界到期解除')
        # v7.16.0 相位 turn_tick_rem: 角色回合边界结算（昔涟结界/长夜月至暗+SPD/风堇雨过天晴+秘技; 三互斥 actor）
        _char_phase(state, unit, 'turn_tick_rem')

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

    def handle_memsprite_action(self, state, ms_unit, regular_turn=True):
        """处理忆灵在行动条上的行动"""
        summoner = next((u for u in state.units if u.char.id == ms_unit.summoner_id), None)
        if not summoner or not ms_unit.is_alive:
            return
        if regular_turn:
            _tick_buffs(ms_unit)
        _ensure_phase_tables(state)
        # v7.16.0 相位 ms_action: 忆灵行动角色处理（→True=完全处理; 遐蝶死龙喷吐与
        # 非死龙 spd 排程全包、阿格莱雅 E2 叠层+至高倒计时自毁）
        if _char_phase(state, summoner, 'ms_action', ms_unit=ms_unit):
            return
        # 更新忆灵AV（通用路径; 角色专属修正已在 ms_action 处理器内完成时经 True 短路）
        spd = ms_unit.action_spd
        ms_unit.extra['next_av'] = state.current_av + AV_PER_TURN / max(spd, 1.0)
        _stamp_av_key(state, ('ms', id(ms_unit)))  # v6.2.1b P3-1: 补达成戳（额外回合路径不经主循环 _set_av）
        self._memsprite_ai(state, summoner, ms_unit)
