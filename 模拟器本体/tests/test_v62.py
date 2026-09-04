"""v6.2 回归测试: 忆灵技能后持续效果-1 / 昔涟E2结界真伤累计 / 大公按段叠层 / 织锦忆灵段

语义依据: CLAUDE_HANDOFF.md v6.2 节（用户提供实机文档: 昔涟.txt/风堇.txt/光锥技能补充）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import _use_skill, _tick_buffs
from engine.characters.lingsha import _lingsha_fuyuan_action
from engine.runtime import SimState, SimUnit, TimedBuff
from engine.core.attributes import compute_combat_stats
from engine.systems.remembrance import RemembranceSystem, _ms_effective_stats
from engine.characters.xilian import _xilian_support_skill


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, lc_id=None, **extra):
    from engine.models.equipment import load_lightcone
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    if lc_id:
        u.lightcone = load_lightcone(lc_id, 'data/light_cones')
    u.extra.update(extra)
    return u


class TestMemspriteSkillDurationReduce:
    def test_fengjin_memsprite_buff_ticks_after_skill(self):
        """v6.2: 小伊卡施放技能后自身持续效果-1（2回合→1回合）"""
        u = _unit('fengjin')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        # 给忆灵挂2回合 buff（疗愈世间的晨曦层）
        ms.buffs.append(TimedBuff(source_id='fengjin', attributes={'DMG_BONUS_ALL': 80.0},
                                  remaining_turns=2, source_name='疗愈世间的晨曦'))
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        kept = [b for b in ms.buffs if getattr(b, 'remaining_turns', 0) > 0]
        assert len(kept) == 1 and kept[0].remaining_turns == 1

    def test_xilian_memsprite_buff_ticks_after_skill(self):
        """v6.2: 德谬歌施放技能后自身持续效果-1"""
        u = _unit('xilian')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.buffs.append(TimedBuff(source_id='xilian', attributes={'DMG_BONUS_ALL': 24.0},
                                  remaining_turns=3, source_name='测试持续效果'))
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        kept = [b for b in ms.buffs if getattr(b, 'remaining_turns', 0) > 0]
        assert len(kept) == 1 and kept[0].remaining_turns == 2

    def test_xilian_support_memsprite_buff_ticks_after_skill(self):
        """德谬歌实际使用无倍率辅助技时也应递减自身持续效果。"""
        u = _unit('xilian')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.buffs.append(TimedBuff(source_id='xilian', attributes={'DMG_BONUS_ALL': 24.0},
                                  remaining_turns=2, source_name='测试持续效果'))

        rem._use_memsprite_skill(state, u, ms, 'memsprite_support')

        assert [b.remaining_turns for b in ms.buffs] == [1]

    def test_memsprite_skill_preserves_permanent_buff(self):
        """忆灵技能后的持续效果递减不得移除 remaining_turns=-1 的永久 Buff。"""
        u = _unit('xilian')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        permanent = TimedBuff(source_id='xilian', attributes={'DMG_BONUS_ALL': 24.0},
                              remaining_turns=-1, source_name='永久测试效果')
        ms.buffs.append(permanent)

        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')

        assert ms.buffs == [permanent]
        assert permanent.remaining_turns == -1

    def test_regular_buff_tick_preserves_permanent_buff(self):
        """常规回合的通用 Buff 倒计时也必须保留永久 Buff。"""
        u = _unit('seele')
        permanent = TimedBuff(source_id='test', attributes={'ATK': 10.0},
                              remaining_turns=-1, source_name='永久测试效果')
        u.buffs.append(permanent)

        expired = _tick_buffs(u)

        assert expired == []
        assert u.buffs == [permanent]
        assert permanent.remaining_turns == -1

    def test_non_xilian_fengjin_memsprite_not_affected(self):
        """v6.2: 其他忆灵（长夜）施放技能不触发此效果"""
        u = _unit('changyeyue')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        ms.buffs.append(TimedBuff(source_id='changyeyue', attributes={'CRIT_DMG': 15.0},
                                  remaining_turns=2, source_name='测试buff'))
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        kept = [b for b in ms.buffs if getattr(b, 'remaining_turns', 0) > 0]
        assert len(kept) == 1 and kept[0].remaining_turns == 2  # 不减


class TestXilianE2RealmTrueDmg:
    def test_e2_accumulates_per_distinct_target(self):
        """v6.2: 昔涟E2——每名不同角色获增益→结界真伤+6%（累计）"""
        u = _unit('xilian', eidolon=2)
        # 两名黄金裔（献予目标选择优先未获诗黄金裔按位置: 先阿格莱雅后长夜月）
        allies = [_unit('aglaea', position=2), _unit('changyeyue', position=3)]
        state = SimState(enemies=[_enemy()], units=[u] + allies)
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        # 展开结界（无增益→0.24）
        _use_skill(u, state, 'skill')
        assert state.realm_true_dmg == pytest.approx(0.24, abs=1e-9)
        # 此诗献予 阿格莱雅（黄金裔）→ +6%
        _xilian_support_skill(state, u, ms)
        assert state.realm_true_dmg == pytest.approx(0.30, abs=1e-9)
        # 再献予 长夜月（另一黄金裔）→ +6%
        _xilian_support_skill(state, u, ms)
        assert state.realm_true_dmg == pytest.approx(0.36, abs=1e-9)

    def test_same_target_counts_once(self):
        """v6.2: 同一角色重复获增益不重复计数"""
        u = _unit('xilian', eidolon=2)
        allies = [_unit('aglaea', position=2)]
        state = SimState(enemies=[_enemy()], units=[u] + allies)
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        _use_skill(u, state, 'skill')
        _xilian_support_skill(state, u, ms)
        _xilian_support_skill(state, u, ms)  # 再献予同一目标
        assert state.realm_true_dmg == pytest.approx(0.30, abs=1e-9)

    def test_e0_stays_base(self):
        """v6.2: 无E2时恒为0.24"""
        u = _unit('xilian')
        allies = [_unit('aglaea', position=2)]
        state = SimState(enemies=[_enemy()], units=[u] + allies)
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        _use_skill(u, state, 'skill')
        _xilian_support_skill(state, u, ms)
        assert state.realm_true_dmg == pytest.approx(0.24, abs=1e-9)

    def test_e2_gifted_persists_across_realm(self):
        """v6.2: 增益计数跨结界（先获增益后展开结界也计入）"""
        u = _unit('xilian', eidolon=2)
        allies = [_unit('aglaea', position=2), _unit('changyeyue', position=3)]
        state = SimState(enemies=[_enemy()], units=[u] + allies)
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        # 先献予2名（结界未展开, 计数先行）
        _xilian_support_skill(state, u, ms)
        _xilian_support_skill(state, u, ms)
        assert len(state.extra.get('xilian_e2_gifted', set())) == 2
        # 后展开结界→真伤按累计计数
        _use_skill(u, state, 'skill')
        assert state.realm_true_dmg == pytest.approx(0.36, abs=1e-9)

    def test_e2_counts_non_gold_target_after_real_buff_is_applied(self):
        """非黄金裔获得+40%可消费 Buff 时，E2 才计入该角色。"""
        u = _unit('xilian', eidolon=2)
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        _use_skill(u, state, 'skill')

        _xilian_support_skill(state, u, ms)

        assert state.extra['xilian_e2_gifted'] == {'seele'}
        assert state.realm_true_dmg == pytest.approx(0.30, abs=1e-9)

    def test_e2_counts_activated_gold_poem(self):
        """v6.6: 13 首献予诗全部激活——已实现黄金裔献予计入 E2 增益"""
        u = _unit('xilian', eidolon=2)
        shell = _unit('phainon', position=2)
        state = SimState(enemies=[_enemy()], units=[u, shell])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        _use_skill(u, state, 'skill')

        _xilian_support_skill(state, u, ms)

        assert state.extra.get('xilian_e2_gifted', set()) == {'phainon'}
        assert state.realm_true_dmg == pytest.approx(0.30, abs=1e-9)


class TestDachangPerHitStack:
    def test_fuyuan_broadcasts_followup_once_per_action(self):
        """通用 on_followup 是动作级事件，单敌浮元整次追击只广播一次。"""
        from engine.hooks.base import HookRegistry
        u = _unit('lingsha')
        counts = {'n': 0}
        def _counting(u, state, **kw):
            counts['n'] += 1
        hook = HookRegistry()
        hook.register('lingsha', 'on_followup', _counting, source_name='计数')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.hooks = hook
        import types
        _lingsha_fuyuan_action(state, types.SimpleNamespace())
        assert counts['n'] == 1

    def test_fuyuan_e6_broadcasts_six_followup_hit_events(self):
        """大公专用逐段事件：浮元E6单敌=6段（全体+单体+4次）。"""
        from engine.hooks.base import HookRegistry
        u = _unit('lingsha', eidolon=6)
        counts = {'n': 0}
        def _counting(u, state, **kw):
            counts['n'] += 1
        hook = HookRegistry()
        hook.register('lingsha', 'on_followup_hit', _counting, source_name='计数')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.hooks = hook
        import types
        _lingsha_fuyuan_action(state, types.SimpleNamespace())
        assert counts['n'] == 6

    def test_dachang_stacks_per_hit(self):
        """v6.2: 大公叠层按段——单敌浮元2段→2层（实机'每次造成伤害'叠层）"""
        from engine.core.relic_conditions import _stack_atk_on_fua_hit
        from engine.hooks.base import HookRegistry
        u = _unit('lingsha')
        u.relic_stacks = {}
        hook = HookRegistry()
        hook.register('lingsha', 'on_followup_hit', _stack_atk_on_fua_hit, source_name='大公')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.hooks = hook
        import types
        _lingsha_fuyuan_action(state, types.SimpleNamespace())
        assert u.relic_stacks.get('大公', 0) == 2

    @pytest.mark.parametrize(('enemy_count', 'eidolon'), [(1, 0), (3, 0), (1, 6)])
    def test_duran_merit_stacks_once_per_fuyuan_action(self, enemy_count, eidolon):
        """都蓝王朝始终按一次浮元追击动作叠层，不受敌人数或E6追加段数影响。"""
        from engine.core.relic_conditions import register_dynamic_relic_effects
        u = _unit('lingsha', eidolon=eidolon)
        state = SimState(enemies=[_enemy() for _ in range(enemy_count)], units=[u])
        register_dynamic_relic_effects(state.hooks, 'lingsha', 'stack_merit_on_fua')
        import types

        _lingsha_fuyuan_action(state, types.SimpleNamespace())

        assert u.relic_stacks.get('Merit', 0) == 1

    def test_bronya_e4_fua_stacks_big_duke_once_per_damage_hit(self):
        """布洛妮娅E4的一段追加伤害也应触发大公的逐段事件。"""
        from engine.characters.bronya import _eid_bronya_e4
        from engine.core.relic_conditions import register_dynamic_relic_effects
        bronya = _unit('bronya', position=1)
        attacker = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[bronya, attacker])
        register_dynamic_relic_effects(state.hooks, 'bronya', 'stack_atk_on_fua')

        _eid_bronya_e4(attacker, state, target=state.enemies[0], skill_key='basic_attack')

        assert bronya.relic_stacks.get('大公', 0) == 1


class TestZhijinMemspriteCrit:
    def test_zhijin_bonus_applies_to_memsprite(self):
        """v6.2: 织锦层数→忆灵暴伤+9%/层（满6层+54%）"""
        u = _unit('aglaea', lc_id='time_woven_into_gold')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        # 6层织锦
        u.lc_stacks['time_woven_into_gold::zhijin'] = 6
        stats = _ms_effective_stats(ms, state)
        assert stats.CRIT_DMG == pytest.approx(ms.base_stats.CRIT_DMG + 0.54, abs=1e-9)

    def test_zhijin_zero_stack_no_bonus(self):
        """v6.2: 无织锦层数→忆灵暴伤不变"""
        u = _unit('aglaea', lc_id='time_woven_into_gold')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        stats = _ms_effective_stats(ms, state)
        assert stats.CRIT_DMG == pytest.approx(ms.base_stats.CRIT_DMG, abs=1e-9)

    def test_no_zhijin_lc_no_bonus(self):
        """v6.2: 无织锦光锥→忆灵暴伤不变"""
        u = _unit('aglaea')
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        ms = u.memsprite_unit
        stats = _ms_effective_stats(ms, state)
        assert stats.CRIT_DMG == pytest.approx(ms.base_stats.CRIT_DMG, abs=1e-9)
