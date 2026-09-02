"""欢愉子系统 — 笑点/阿哈/好活当赏/隐藏分/无敌玩家

仅在队伍包含欢愉命途角色时由 simulate() 条件激活。
直接操作 SimState/SimUnit 原生字段。
"""
import copy
import random
from engine.models.elation import calc_aha_speed
from engine.core.damage import calculate_damage
from engine.core.combat_sim import _use_skill

# 常量
HS_PER_CR = 0.004      # 每隐藏分暴击率
HS_PER_CD = 0.008      # 每溢出隐藏分暴伤
CR_CAP = 1.0
HS_ULT_COST = 60       # 终结技消耗隐藏分
HS_TALENT_GAIN = 20    # 天赋：进无敌+20HS
HS_LC_GAIN = 20        # 光锥：自释终结技+20HS
HS_ELATION_BASE = 15   # 欢愉技固定+15HS
HS_TRACE_THRESHOLD_LOW = 20
HS_TRACE_THRESHOLD_HIGH = 40
HS_TRACE_BONUS = 20
HS_MAX = 300       # 终结技解锁阈值60 + 可溢出240
INVINCIBLE_MAX = 3     # 无敌玩家强化普攻次数
AHA_EST_CYCLE = 71.0   # 估算阿哈周期AV
AHA_EST_RATE = 9.0     # 估算每周期HS


class ElationSystem:
    """欢愉子系统协调器"""

    # -- 初始化 --

    def init_battle(self, state, units):
        log = state.log
        elation_units = [u for u in units if u.char.path == "欢愉"]

        state.laugh_points = len(elation_units)
        # v6.7: 开局好活走统一包装（绯英方向2互转: 开局+20好活→+20能量）
        for eu in elation_units:
            self.grant_good_show(state, eu.char.id, 20.0, duration=2, source="battle_start")
        log.append(f'[Init] 笑点={state.laugh_points:.0f}')
        log.append('[Init] 好活当赏: 全队+20层(2回合)')

        # 开拓者·欢愉秘技：随机获得开怀大笑(小概率+30%)/忍俊不禁(大概率+20%), 我方全体3回合
        # v6.10.3 P1-4: 此前永久+30%且仅欢愉命途, 与TXT不符
        has_tb = any(u.char.id == "trailblazer_elation" for u in units)
        if has_tb:
            import random as _rnd
            from engine.core.combat_sim import TimedBuff
            # TXT 未给精确权重；暂用 25%/75% 保持“小概率/大概率”关系。
            val = _rnd.choices([0.30, 0.20], weights=[1, 3], k=1)[0]
            for u in units:
                if u.is_alive:
                    u.buffs.append(TimedBuff(source_id="trailblazer_elation",
                                             attributes={"ELATION_LEVEL": val * 100.0},
                                             remaining_turns=3,
                                             param_id="tb_tech_elation",
                                             source_name="开拓者秘技·燃起来了"))
            log.append(f'[Init] 主角秘技: 全队欢愉度+{val*100:.0f}%(3回合)')

        # 爻光秘技：进场自动触发1次战技（不消耗战技点）→ 结界 + 全队欢愉度=爻光欢愉度×20%
        yao = next((u for u in units if u.char.id == "yaoguang"), None)
        if yao:
            from engine.core.combat_sim import _gain_energy, _yaoguang_open_field
            _yaoguang_open_field(state, yao, source='technique')
            state.laugh_points += 3
            _gain_energy(yao, 30.0, state=state)
            log.append('[Init] 爻光秘技: 免SP自动战技, +3笑点, +30能量')

        # 火花秘技（非进战·流量变现）: 全敌50%ATK火伤+回2战技点
        spx = next((u for u in units if u.char.id == "sparxie"), None)
        if spx:
            from engine.core.combat_sim import _gain_skill_points
            from engine.core.damage import calculate_damage
            from engine.core.combat_sim import _commit_enemy_damage
            stats = spx.base_stats
            for e in state.enemies:
                d = calculate_damage(stats, e, stats.ATK, 50.0, 'direct', '火', 80, False,
                                     crit_mode='expected')
                _commit_enemy_damage(state, spx, e, d.final_damage)
                spx.total_damage_dealt += d.final_damage
            _gain_skill_points(state, 2)
            log.append('[Init] 火花秘技: 全敌50%ATK火伤 + 回2战技点')

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
        target = next((x for x in state.units if x.char.id == char_id), None)
        eva = next((x for x in state.units if x.char.id == 'evanescia' and x.is_alive), None)
        # 行迹3·瞰众乐: 队友（参演编号<146 非绯英）获得好活 → 50%转绯英
        if (eva and target and char_id != 'evanescia'
                and (target.char.cast_number or 0) < 146):
            transfer = amount * 0.5
            if eva.eidolon_rank >= 2:
                transfer *= 1.5  # E2: 触发瞰众乐额外+50%
            self.grant_good_show(state, 'evanescia', transfer, duration=duration,
                                 source='evanescia_trace3')
            state.log.append(f'  绯英行迹3·瞰众乐: {char_id}好活{amount:.0f}→绯英+{transfer:.0f}')
        # 天赋方向2: 绯英获得好活→等值能量（单次≤100）
        if char_id == 'evanescia' and eva is not None \
                and not state.extra.get('_eva_convert_lock'):
            state.extra['_eva_convert_lock'] = True
            try:
                from engine.core.combat_sim import _gain_energy
                _gain_energy(eva, min(float(amount), 100.0), state=state)
            finally:
                state.extra['_eva_convert_lock'] = False
            # E6: 好活当赏持续时间+1回合
            if eva.eidolon_rank >= 6:
                duration += 1
        # v6.10.3 P1-3: 爻光行迹2·鸿运鳞集——爻光获得好活当赏时持续时间+1回合(2→3)
        if char_id == 'yaoguang':
            yao = next((x for x in state.units
                        if x.char.id == 'yaoguang' and x.is_alive), None)
            if yao and any(getattr(t, 'hook_name', '') == 'yaoguang_goodshow_extend'
                           for t in (yao.char.traces or [])):
                duration += 1
        return state.elation_state.grant_good_show(
            char_id, amount, duration=duration, source=source)

    # -- 回合推进 --

    def tick_good_show_turn(self, state, unit):
        """Expire only the acting owner's Good Show at regular-turn end."""
        cid = unit.char.id
        lost = state.elation_state.tick_good_show(cid)
        if lost <= 0 or cid == 'evanescia':
            return
        eva = next((x for x in state.units
                    if x.char.id == 'evanescia' and x.is_alive), None)
        if not eva:
            return
        transfer = lost * 0.5
        if eva.eidolon_rank >= 2:
            transfer *= 2.0
        self.grant_good_show(state, 'evanescia', transfer, duration=2,
                             source='evanescia_trace2')
        state.log.append(f'  绯英行迹2·开不败: {cid}好活到期{lost:.0f}→绯英+{transfer:.0f}')

    def tick_turn_start(self, state, unit):
        for attr in ('tb_cd_buff_turns', 'yao_res_pen_turns'):
            val = getattr(unit, attr, 0)
            if val > 0:
                setattr(unit, attr, val - 1)
        # 爻光结界只在爻光自身行动时递减
        if state.yao_field_active and unit.char.id == 'yaoguang':
            state.yao_field_turns -= 1
            if state.yao_field_turns <= 0:
                from engine.core.combat_sim import _yaoguang_close_field
                _yaoguang_close_field(state)

    def tick_turn(self, state, unit):
        """Compatibility entry for direct callers advancing one full turn."""
        self.tick_turn_start(state, unit)
        self.tick_good_show_turn(state, unit)

    def check_aha(self, state, unit_av, max_av):
        return (state.laugh_points > 0 and state.aha_next_av <= unit_av
                and state.aha_next_av < max_av)

    def execute_aha(self, state):
        n = state.laugh_points
        if n <= 0:
            return
        state.log.append(f'[Aha] AV={state.current_av:.0f} 笑点={n:.0f}')

        elation_units = sorted(
            [u for u in state.units if u.char.cast_number > 0 and u.is_alive],
            key=lambda u: u.char.cast_number)

        for u in elation_units:
            _use_skill(u, state, "elation_skill")
            if u.char.id == "yinlang":
                if n >= HS_TRACE_THRESHOLD_LOW:
                    bonus = HS_TRACE_BONUS * (2 if n >= HS_TRACE_THRESHOLD_HIGH else 1)
                    self.gain_hidden_score(state, u, bonus)
                    state.log.append(f'  银狼特殊行迹: HS+{bonus} (笑点={n:.0f})')

        state.laugh_points = 0.0
        for u in state.units:
            if u.char.path == "欢愉" and u.is_alive:
                self.grant_good_show(state, u.char.id, n, duration=2, source="aha")
            if u.char.id == "yinlang":
                self.gain_hidden_score(state, u, n)
        state.log.append(f'  全队+{n:.0f}好活当赏(2回合), 银狼HS+{n:.0f}')

        # v6.7 火花星魂（阿哈时刻结束时触发）
        spx = next((x for x in state.units if x.char.id == 'sparxie' and x.is_alive), None)
        if spx:
            if spx.eidolon_rank >= 1:
                state.laugh_points += 5
                state.log.append('  火花E1: 阿哈时刻结束+5笑点')
            if spx.eidolon_rank >= 2:
                from engine.core.combat_sim import _gain_skill_points
                state.extra.setdefault('extra_turns', []).append((spx, 'sparxie_e2'))
                state.extra['sparxie_burst_points'] = \
                    state.extra.get('sparxie_burst_points', 0.0) + 2
                state.log.append('  火花E2: 阿哈时刻结束+1额外回合+2爆点')

        # 爻光终结技阿哈额外回合：恢复全局笑点池 + 清除E4标记
        if state.extra.get('yao_pending_laugh', 0) > 0:
            state.laugh_points = state.extra['yao_pending_laugh']
            state.extra['yao_pending_laugh'] = 0
        state.extra.pop('yao_e4_aha', None)

        state.aha_next_av = state.current_av + 10000.0 / state.aha_speed

    # -- 银狼专属 --

    def est_hs_gain(self, units, av_window):
        sv = next((u for u in units if u.char.id == "yinlang"), None)
        return (av_window / AHA_EST_CYCLE) * AHA_EST_RATE if sv else 0.0

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
        """统一隐藏分入口，并消费银狼E2的120分阈值。"""
        amount = max(float(amount or 0.0), 0.0)
        before = u.hidden_score
        u.hidden_score = min(HS_MAX, before + amount)
        if u.char.id != 'yinlang' or u.eidolon_rank < 2 or not u.invincible_active:
            return amount
        threshold = u.extra.get('yinlang_e2_next_threshold', 120.0)
        while before < threshold <= u.hidden_score:
            state.extra.setdefault('extra_turns', []).append((u, 'yinlang_e2'))
            u.invincible_basics_done = max(0, u.invincible_basics_done - 1)
            threshold += 120.0
            state.log.append('  银狼E2: 隐藏分达到120阈值→额外回合+恢复强化普攻')
        u.extra['yinlang_e2_next_threshold'] = threshold
        return amount

    def eff_stats(self, u, state=None, base_stats=None):
        s = copy.deepcopy(base_stats if base_stats is not None else u.base_stats)
        s.CRIT_RATE = min(s.CRIT_RATE + self._hidden_score_cr(u.hidden_score), CR_CAP)
        s.CRIT_DMG += self._hidden_score_cd(u.hidden_score, u.base_stats.CRIT_RATE)
        # v6.10.3 P1-3: 爻光终结技全抗穿24%+E1欢愉无视防御20% 已移至 combat_sim _build_effective_stats 动态消费
        # 银狼行迹1：有效速度160起欢愉度+50%，每超1点再+2%，上限100%。
        effective_spd = s.SPD + s._base_SPD * s.SPD_PERCENT
        if u.char.id == 'yinlang' and effective_spd >= 160:
            s.ELATION_LEVEL += min(1.0, 0.50 + (effective_spd - 160.0) * 0.02)
        # 闪耀功勋4件套: 笑点→欢愉DEF穿透 (每5笑点+1%, 上限10层)
        if state and hasattr(u, '_active_relic_conditions') and \
           "elation_laugh_def_pen_stack" in getattr(u, '_active_relic_conditions', set()):
            laugh_stacks = min(int(state.laugh_points // 5), 10)
            extra_defpen = laugh_stacks * 0.01
            s.DEF_PEN_BY_TYPE['elation'] = s.DEF_PEN_BY_TYPE.get('elation', 0) + extra_defpen
        # v6.7 火花: 行迹2每笑点全队暴伤+8%(上限80%); E1每笑点全队抗穿+1.5%(上限15%);
        # 行迹3 ATK>2000每超100→自身欢愉度+5%(上限80%)
        if state:
            spx = next((x for x in state.units
                        if x.char.id == 'sparxie' and x.is_alive), None)
            if spx:
                laugh_cap = min(int(state.laugh_points), 10)
                s.CRIT_DMG += 0.08 * laugh_cap  # 行迹2
                if spx.eidolon_rank >= 1:
                    s.RES_PEN_ALL += 0.015 * laugh_cap  # E1
                if u.char.id == 'sparxie':
                    extra_elation = 0.05 * min(max(int((s.ATK - 2000) / 100), 0), 16)
                    if extra_elation > 0:
                        s.ELATION_LEVEL += extra_elation  # 行迹3
        return s

    def silver_ult(self, u, state):
        hs = u.hidden_score
        if hs < HS_ULT_COST:
            state.log.append(f'  [BUG] HS={hs:.0f}时开大被阻止')
            return
        if u.eidolon_rank >= 2:
            threshold = 120.0
            while threshold <= hs:
                state.extra.setdefault('extra_turns', []).append((u, 'yinlang_e2'))
                threshold += 120.0
            u.extra['yinlang_e2_next_threshold'] = threshold
        u.hidden_score = hs - HS_ULT_COST + HS_TALENT_GAIN
        u.invincible_active = True
        u.invincible_basics_done = 0
        u.extra['yinlang_blindbox_prob'] = 1.0
        from engine.core.combat_sim import _silver_wolf_apply_entry_effects
        _silver_wolf_apply_entry_effects(state)
        if u.eidolon_rank >= 2:
            for buff in u.buffs:
                if getattr(buff, 'remaining_turns', -1) >= 0:
                    buff.remaining_turns += 1

        lc_bonus = 0
        if not u.lc_ult_used:
            state.laugh_points += HS_LC_GAIN
            self.gain_hidden_score(state, u, HS_LC_GAIN)
            u.lc_ult_used = True
            lc_bonus = HS_LC_GAIN

        ha = u.hidden_score
        state.log.append(
            f'[{state.current_av:6.0f}AV] 银狼 无敌玩家启动! '
            f'HS={hs:.0f}->扣{HS_ULT_COST}->+天赋{HS_TALENT_GAIN}+LC{lc_bonus}={ha:.0f} '
            f'CR+{self._hidden_score_cr(ha)*100:.1f}% '
            f'CD+{self._hidden_score_cd(ha,u.base_stats.CRIT_RATE)*100:.1f}%')

    def silver_blindbox(self, u, state, *, force=False, laugh_n_override=None):
        """Trigger Silver Wolf's good-show blindbox when a skill point is spent."""
        if not force and not u.invincible_active:
            return 0.0
        if not force and state.elation_state.get_good_show_total(u.char.id) <= 0:
            return 0.0
        probability = 1.0 if force else u.extra.get('yinlang_blindbox_prob', 1.0)
        if random.random() > probability:
            return 0.0
        if not force:
            u.extra['yinlang_blindbox_prob'] = probability * 0.20
        from engine.core.combat_sim import _commit_enemy_damage, _enemy_for_damage, _gain_skill_points
        alive = state.alive_enemies()
        if not alive:
            return 0.0
        stats = self.eff_stats(u, state)
        laugh_n = laugh_n_override if laugh_n_override is not None else u.hidden_score
        base_damage = sum(
            calculate_damage(stats, _enemy_for_damage(target), 0, 90.0, 'elation',
                             u.char.element, 80, stats.CRIT_RATE >= 0.5,
                             laugh_n=laugh_n, crit_mode='expected').final_damage
            for target in alive
        )
        total = 0.0
        if base_damage > 0:
            share = base_damage / len(alive)
            for target in list(alive):
                _commit_enemy_damage(state, u, target, share)
            total += base_damage
        effect_roll = random.random()
        if effect_roll < 0.33:
            target = max((target for target in alive if target.HP > 0),
                         key=lambda target: target.HP, default=None)
            if target is not None:
                extra = base_damage * 0.20
                _commit_enemy_damage(state, u, target, extra,
                                     damage_type='true_damage',
                                     record_cipher=False)
                total += extra
                effect = f'大剑(+{extra:.0f}真伤)'
            else:
                effect = '大剑(无存活目标)'
        elif effect_roll < 0.66:
            _gain_skill_points(state, 2)
            effect = '炸弹(+2SP)'
        else:
            state.laugh_points += 3
            self.gain_hidden_score(state, u, 3)
            effect = '怪味豆(+3笑点)'
        u.total_damage_dealt += total
        next_probability = probability * 0.20 if not force else probability
        state.log.append(f'  银狼头号补给盲盒: {total:.0f} [{effect}] 概率{probability:.0%}->{next_probability:.0%}')
        return total

    def silver_technique_wave(self, u, state):
        """秘技召唤物：每个波次开始固定触发一次盲盒，欢愉计数固定为99。"""
        if not u.is_alive:
            return 0.0
        return self.silver_blindbox(u, state, force=True, laugh_n_override=99.0)

    def silver_enhanced_basic(self, u, state):
        from engine.core.combat_sim import _commit_enemy_damage, _enemy_for_damage
        s = self.eff_stats(u, state)
        damage_mult = 1.0 + min(int(u.hidden_score / 60), 2) * 0.15
        s.DAMAGE_MULTIPLIER *= damage_mult
        if u.eidolon_rank >= 6:
            s.LAUGH_BOOST += 0.50
        td, hs = 0.0, u.hidden_score
        has_gs = state.elation_state.get_good_show_total(u.char.id) > 0
        is_crit = s.CRIT_RATE >= 0.5

        # 100 段弹射
        for _ in range(100):
            alive = state.alive_enemies()
            if not alive:
                break
            t = random.choice(alive)
            dmg_type = "elation" if has_gs else "direct"
            scaling = 0 if has_gs else s.ATK
            laugh_n = state.elation_state.get_good_show_total(u.char.id) if has_gs else 0
            d = calculate_damage(s, _enemy_for_damage(t), scaling, 2.4, dmg_type,
                                 u.char.element, 80, is_crit,
                                 laugh_n=laugh_n, crit_mode="expected")
            _commit_enemy_damage(state, u, t, d.final_damage)
            td += d.final_damage

        # 3 次盲盒：成功概率按上次成功后的20%递减，基础伤害由敌方全体均分。
        bb_dmg, bb_parts = 0.0, []
        alive = state.alive_enemies() or state.enemies
        for _ in range(3):
            probability = u.extra.get('yinlang_blindbox_prob', 1.0)
            if random.random() > probability:
                bb_parts.append('未触发盲盒')
                continue
            u.extra['yinlang_blindbox_prob'] = probability * 0.20
            bh = sum(calculate_damage(s, _enemy_for_damage(t), 0, 90.0, "elation",
                                      u.char.element, 80, is_crit,
                                      laugh_n=hs, crit_mode="expected").final_damage
                      for t in alive if t.HP > 0)
            bb_dmg += bh
            live_targets = [t for t in alive if t.HP > 0]
            if live_targets and bh > 0:
                share = bh / len(live_targets)
                for target in live_targets:
                    _commit_enemy_damage(state, u, target, share)
            roll = random.random()
            if roll < 0.33:
                td += bh * 0.20
                if live_targets:
                    sword_target = max(live_targets, key=lambda target: target.HP)
                    _commit_enemy_damage(state, u, sword_target, bh * 0.20,
                                         damage_type='true_damage',
                                         record_cipher=False)
                bb_parts.append(f'大剑(+{bh*0.20:.0f}真伤)')
            elif roll < 0.66:
                from engine.core.combat_sim import _gain_skill_points
                _gain_skill_points(state, 2)
                bb_parts.append('炸弹(+2SP)')
            else:
                state.laugh_points += 3
                self.gain_hidden_score(state, u, 3)
                hs = u.hidden_score
                bb_parts.append('怪味豆(+3笑点)')
        td += bb_dmg

        # 最后一击
        for t in (state.alive_enemies() or state.enemies):
            if t.HP <= 0:
                continue
            dmg_type = "elation" if has_gs else "direct"
            scaling = 0 if has_gs else s.ATK
            laugh_n = state.elation_state.get_good_show_total(u.char.id) if has_gs else 0
            d = calculate_damage(s, _enemy_for_damage(t), scaling, 100.0, dmg_type,
                                 u.char.element, 80, is_crit,
                                 laugh_n=laugh_n, crit_mode="expected")
            _commit_enemy_damage(state, u, t, d.final_damage)
            td += d.final_damage

        u.total_damage_dealt += td
        u.invincible_basics_done += 1
        n = u.invincible_basics_done
        u.damage_log.append((f"强化普攻#{n}", td, "enhanced_basic"))
        state.log.append(
            f'[{state.current_av:6.0f}AV] {u.char.name} 强化普攻#{n}: {td:.0f} '
            f'(HS={hs:.0f}, x{damage_mult:.2f}) 盲盒伤害={bb_dmg:.0f} [{",".join(bb_parts)}]')
        from engine.core.combat_sim import _qingge_notify_attack
        _qingge_notify_attack(state, u, dealt=td > 0)  # v7.1.0 P1: 不经_use_skill的强化普攻补气氛

        if n >= INVINCIBLE_MAX:
            u.invincible_active = False
            u.invincible_basics_done = 0
            retained = u.hidden_score * 0.20 if u.eidolon_rank >= 1 else 0.0
            u.hidden_score = retained
            u.lc_ult_used = False
            u.extra['yinlang_blindbox_prob'] = 1.0
            u.extra.pop('yinlang_e2_next_threshold', None)
            from engine.core.combat_sim import _silver_wolf_apply_entry_effects
            _silver_wolf_apply_entry_effects(state)
            state.log.append(f'  退出无敌玩家，隐藏分保留{retained:.0f}，LC重置')


# ---- 角色 AI（统一签名: fn(unit, state, *, elation, **ctx)） ----

def _yl_ai(u, state, *, elation, max_av, navs, uidx, **__):
    if u.invincible_active:
        elation.silver_enhanced_basic(u, state)
    elif u.hidden_score >= HS_ULT_COST:
        # 动态开大阈值: 开大后剩余HS需≥120才能吃满30%独立乘区
        # 开大消耗60, 行迹返还20, 光锥返还20(如有) → 阈值=60+120-20-光锥返还=160-光锥返还
        lc_refund = HS_LC_GAIN if not u.lc_ult_used else 0
        hs_threshold = HS_ULT_COST + 120 - HS_TALENT_GAIN - lc_refund  # 140 或 160
        remaining = max_av - state.current_av
        # 近结束时允许提前开大(不掉轴)
        can_early = remaining < 350 and u.hidden_score >= HS_ULT_COST + HS_TALENT_GAIN
        if u.hidden_score >= hs_threshold or can_early:
            elation.silver_ult(u, state)
            navs[uidx] = state.current_av
        elif state.skill_points > 0:
            _use_skill(u, state, "skill")
        else:
            _use_skill(u, state, "basic_attack")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _yg_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        # v6.10.3 P1-3 E0/E1分离: 阿哈额外回合固定计入笑点 E0=20 / E1=40;
        # 保存全局笑点→临时替换→主循环处理阿哈→恢复全局笑点（不消耗原笑点）
        fixed = 40 if u.eidolon_rank >= 1 else 20
        saved_laugh = state.laugh_points
        state.laugh_points = fixed
        state.aha_next_av = state.current_av  # 强制阿哈立即行动
        # 全队全抗穿24% (3回合); E1 额外欢愉伤害无视防御20%（面板消费）
        for eu in state.units:
            if eu.is_alive:
                eu.yao_res_pen_turns = 3
        if u.eidolon_rank >= 4:
            state.extra['yao_e4_aha'] = True  # E4: 本次阿哈回合全体欢愉技伤害×1.5
        state.log.append(f'  爻光终结技: 阿哈额外回合固定{fixed}笑点, 全队全抗穿24%(3回合)' +
                         (' +E1无视防御20%' if u.eidolon_rank >= 1 else '') +
                         (' +E4欢愉技×1.5' if u.eidolon_rank >= 4 else ''))
        # 标记：阿哈处理后恢复全局笑点
        state.extra['yao_pending_laugh'] = saved_laugh
    elif state.skill_points > 0 and not state.yao_field_active:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _tb_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        # v6.10.3 P1-4: 通用指定单体分支（此前硬编码银狼）
        target = u.extra.get('lc_last_skill_target')
        if target is None:
            target = next((x for x in state.units if x.is_alive and x is not u), None)
        if target is None:
            return
        # E1: 战技叠层→终结技目标好活+2×层数（最多3层, 消费后清零）
        e1_stacks = (u.relic_stacks or {}).get('tb_e1', 0)
        if u.eidolon_rank >= 1 and e1_stacks > 0:
            elation.grant_good_show(state, target.char.id, 2.0 * e1_stacks, source="tb_e1")
            u.relic_stacks['tb_e1'] = 0
            state.log.append(f'  开拓者E1: 终结技好活+{2.0 * e1_stacks:.0f}')
        if getattr(target.char, 'skills', {}).get('elation_skill'):
            elation.grant_good_show(state, target.char.id, 10, source="tb_ult")
            state.log.append(f'  主角终结技→{target.char.name}: +10好活当赏, 立即欢愉技(固定计入20笑点)')
            _use_skill(target, state, "elation_skill", laugh_n_override=20.0)
        else:
            from engine.core.combat_sim import (AV_PER_TURN, _effective_spd, _set_av,
                                                _guest_advance_blocked)
            navs = state.extra.get('navs', {})
            idx = next((i for i, x in enumerate(state.units) if x is target), None)
            if idx is not None and idx in navs \
                    and not _guest_advance_blocked(state, u, target):
                advanced_av = max(
                    state.current_av,
                    navs[idx] - (AV_PER_TURN / max(_effective_spd(target, state), 1.0)) * 0.5,
                )
                _set_av(state, navs, idx, advanced_av)
                state.log.append(f'  主角终结技→{target.char.name}: 行动提前50%')
    elif state.skill_points >= 2:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _hh_ai(u, state, *, elation, **__):
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
        for eu in state.units:
            if eu.is_alive and eu.char.id != "huohuo":
                eu.current_energy = min(eu.char.max_energy,
                                        eu.current_energy + eu.char.max_energy * 0.20)
        state.log.append('  藿藿终结技: 队友回能20%')
    elif state.skill_points >= 2 and any(
        x.current_hp / x.max_hp < 0.5 for x in state.units
        if x.is_alive and x.char.id != "huohuo"
    ):
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _eva_ai(u, state, *, elation, **__):
    """绯英 AI（v6.7）: 能量满→终结技; 好活≥240累计自动触发狐狸老师FUA(引擎hook);
    SP>0→战技(额外+10笑点); 否则普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


def _spx_ai(u, state, *, elation, **__):
    """火花 AI（v6.7）: 能量满→终结技; 直播连线激活→普攻(消耗连线触发强化普攻);
    SP>0→战技(开连线+陷阱); 否则普攻"""
    if u.current_energy >= u.char.max_energy:
        _use_skill(u, state, "ultimate")
    elif u.extra.get('sparxie_live'):
        _use_skill(u, state, "basic_attack")
    elif state.skill_points > 0:
        _use_skill(u, state, "skill")
    else:
        _use_skill(u, state, "basic_attack")


CHARACTER_AI = {
    "yinlang": _yl_ai,
    "yaoguang": _yg_ai,
    "trailblazer_elation": _tb_ai,
    "huohuo": _hh_ai,
    "evanescia": _eva_ai,   # v6.7 绯英
    "sparxie": _spx_ai,     # v6.7 火花
}
