"""欢愉子系统 — 笑点/阿哈/好活当赏/隐藏分/无敌玩家

仅在队伍包含欢愉命途角色时由 simulate() 条件激活。
直接操作 SimState/SimUnit 原生字段。
"""
import copy
import random
from engine.models.elation import calc_aha_speed
from engine.core.damage import calculate_damage
from engine.core.combat_engine import (
    _use_skill, _char_phase, _obs_phase, _ensure_phase_tables,
)
from engine.core.combat_engine import _commit_enemy_damage, _gain_energy, _gain_skill_points
from engine.runtime import TimedBuff, _enemy_for_damage

# 常量
HS_PER_CR = 0.004      # 每隐藏分暴击率
HS_PER_CD = 0.008      # 每溢出隐藏分暴伤
CR_CAP = 1.0
HS_TRACE_THRESHOLD_LOW = 20
HS_TRACE_THRESHOLD_HIGH = 40
HS_TRACE_BONUS = 20
HS_MAX = 300       # 终结技解锁阈值60 + 可溢出240


class ElationSystem:
    """欢愉子系统协调器"""

    # -- 初始化 --

    def init_battle(self, state, units):
        _ensure_phase_tables(state)
        log = state.log
        elation_units = [u for u in units if u.char.path == "欢愉"]

        state.laugh_points = len(elation_units)
        # v6.7: 开局好活走统一包装（绯英方向2互转: 开局+20好活→+20能量）
        for eu in elation_units:
            self.grant_good_show(state, eu.char.id, 20.0, duration=2, source="battle_start")
        log.append(f'[Init] 笑点={state.laugh_points:.0f}')
        log.append('[Init] 好活当赏: 全队+20层(2回合)')

        # v7.15.0 观察相位: 三欢愉秘技（开拓者/爻光/火花; 处理器在角色包, 顺序=原内联）
        _obs_phase(state, 'init_tb_tech', None)
        _obs_phase(state, 'init_yaoguang_tech', None)
        _obs_phase(state, 'init_sparxie_tech', None)

        spds = [u.base_stats.SPD for u in elation_units]
        state.aha_speed = calc_aha_speed(spds)
        state.aha_next_av = 10000.0 / state.aha_speed
        log.append(f'[Init] Aha速度={state.aha_speed:.1f} 首次={state.aha_next_av:.0f}AV')

    # -- 好活当赏统一入口（v6.7） --

    def grant_good_show(self, state, char_id, amount, duration=2, source=""):
        """好活当赏统一入口（v6.7 绯英互转/E2乘区/行迹3转移包装）。

        - 绯英行迹3·瞰众乐: 参演编号<146的队友获得好活→50%转自身（E2额外50%）
        - 绯英天赋方向2: 获得好活→等值能量（单次≤100, _eva_convert_lock 防递归;
          锁内产生的能量不再反向转好活, 见 TRACE evanescia_energy_convert）
        - 返回模型层 GoodShowInstance
        """
        _ensure_phase_tables(state)
        # v7.15.0 观察相位: 绯英转移/互转（→新duration|None）+ 爻光鸿运鳞集（+1）
        d2 = _obs_phase(state, 'goodshow_eva', None, char_id=char_id, amount=amount,
                        duration=duration)
        if d2 is not None:
            duration = d2
        yao_ext = _obs_phase(state, 'goodshow_yaoguang', None, char_id=char_id)
        if yao_ext is not None:
            duration += yao_ext
        return state.elation_state.grant_good_show(
            char_id, amount, duration=duration, source=source)

    # -- 回合推进 --

    def tick_good_show_turn(self, state, unit):
        """Expire only the acting owner's Good Show at regular-turn end."""
        _ensure_phase_tables(state)
        cid = unit.char.id
        lost = state.elation_state.tick_good_show(cid)
        if lost <= 0:
            return
        # v7.15.0 观察相位 goodshow_expire: 绯英行迹2·开不败（到期转移; 处理器内含自身守卫）
        _obs_phase(state, 'goodshow_expire', unit, lost=lost)

    def tick_turn_start(self, state, unit):
        _ensure_phase_tables(state)
        for attr in ('tb_cd_buff_turns', 'yao_res_pen_turns'):
            val = getattr(unit, attr, 0)
            if val > 0:
                setattr(unit, attr, val - 1)
        # v7.15.0 相位 field_tick: 爻光结界只在爻光自身行动时递减（守卫在处理器内）
        _char_phase(state, unit, 'field_tick')

    def tick_turn(self, state, unit):
        """Compatibility entry for direct callers advancing one full turn."""
        self.tick_turn_start(state, unit)
        self.tick_good_show_turn(state, unit)

    def check_aha(self, state, unit_av, max_av):
        return (state.laugh_points > 0 and state.aha_next_av <= unit_av
                and state.aha_next_av < max_av)

    def execute_aha(self, state):
        _ensure_phase_tables(state)
        n = state.laugh_points
        if n <= 0:
            return
        state.log.append(f'[Aha] AV={state.current_av:.0f} 笑点={n:.0f}')

        elation_units = sorted(
            [u for u in state.units if u.char.cast_number > 0 and u.is_alive],
            key=lambda u: u.char.cast_number)

        for u in elation_units:
            _use_skill(u, state, "elation_skill")
            # v7.15.0 相位 aha_trace: 阿哈时刻银狼特殊行迹（HS 结算在处理器内）
            _char_phase(state, u, 'aha_trace', n=n)

        state.laugh_points = 0.0
        for u in state.units:
            if u.char.path == "欢愉" and u.is_alive:
                self.grant_good_show(state, u.char.id, n, duration=2, source="aha")
            # v7.15.0 相位 aha_hs_gain: 阿哈结算银狼隐藏分+笑点数
            _char_phase(state, u, 'aha_hs_gain', n=n)
        state.log.append(f'  全队+{n:.0f}好活当赏(2回合), 银狼HS+{n:.0f}')

        # v7.15.0 观察相位 aha_sparxie_settle: 火花星魂（阿哈时刻结束时触发）
        _obs_phase(state, 'aha_sparxie_settle', None, n=n)

        # 爻光终结技阿哈额外回合：恢复全局笑点池 + 清除E4标记
        if state.extra.get('yao_pending_laugh', 0) > 0:
            state.laugh_points = state.extra['yao_pending_laugh']
            state.extra['yao_pending_laugh'] = 0
        state.extra.pop('yao_e4_aha', None)

        state.aha_next_av = state.current_av + 10000.0 / state.aha_speed

    # -- 隐藏分/面板（银狼机制本体已迁 engine.characters.yinlang）--

    @staticmethod
    def _hidden_score_cr(hs):
        return hs * HS_PER_CR

    @staticmethod
    def _hidden_score_cd(hs, base_cr):
        cr_from = hs * HS_PER_CR
        if base_cr + cr_from <= CR_CAP:
            return 0.0
        return ((base_cr + cr_from - CR_CAP) / HS_PER_CR) * HS_PER_CD

    def gain_hidden_score(self, state, u, amount):
        """统一隐藏分入口；银狼E2阈值消费经 hidden_score_e2 相位（处理器在角色包）。"""
        _ensure_phase_tables(state)
        amount = max(float(amount or 0.0), 0.0)
        before = u.hidden_score
        u.hidden_score = min(HS_MAX, before + amount)
        # v7.15.0 相位 hidden_score_e2: 银狼E2 120分阈值→额外回合（守卫在处理器内）
        _char_phase(state, u, 'hidden_score_e2', before=before)
        return amount

    def eff_stats(self, u, state=None, base_stats=None):
        _ensure_phase_tables(state)
        s = copy.deepcopy(base_stats if base_stats is not None else u.base_stats)
        s.CRIT_RATE = min(s.CRIT_RATE + self._hidden_score_cr(u.hidden_score), CR_CAP)
        s.CRIT_DMG += self._hidden_score_cd(u.hidden_score, u.base_stats.CRIT_RATE)
        # v6.10.3 P1-3: 爻光终结技全抗穿24%+E1欢愉无视防御20% 已移至 combat_engine _build_effective_stats 动态消费
        # 银狼行迹1：有效速度160起欢愉度+50%，每超1点再+2%，上限100%。
        effective_spd = s.SPD + s._base_SPD * s.SPD_PERCENT
        # v7.15.0 相位 eff_stats_yinlang: 行迹1 欢愉度加成（→新s|None）
        s2 = _char_phase(state, u, 'eff_stats_yinlang', s=s, effective_spd=effective_spd)
        if s2 is not None:
            s = s2
        # 闪耀功勋4件套: 笑点→欢愉DEF穿透 (每5笑点+1%, 上限10层)
        if state and hasattr(u, '_active_relic_conditions') and \
           "elation_laugh_def_pen_stack" in getattr(u, '_active_relic_conditions', set()):
            laugh_stacks = min(int(state.laugh_points // 5), 10)
            extra_defpen = laugh_stacks * 0.01
            s.DEF_PEN_BY_TYPE['elation'] = s.DEF_PEN_BY_TYPE.get('elation', 0) + extra_defpen
        # v6.7 火花: 行迹2每笑点全队暴伤+8%(上限80%); E1每笑点全队抗穿+1.5%(上限15%);
        # 行迹3 ATK>2000每超100→自身欢愉度+5%(上限80%)
        if state:
            # v7.15.0 观察相位 eff_stats_sparxie: 行迹2/E1/行迹3 全队面板（→新s|None）
            s2 = _obs_phase(state, 'eff_stats_sparxie', u, s=s)
            if s2 is not None:
                s = s2
        return s




