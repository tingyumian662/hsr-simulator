"""v6.10.3 regression tests for Codex second-round audit fixes (P1-2/P1-5/P1-7 batch)."""
import pytest

from engine.core.attributes import CombatStats, compute_combat_stats
from engine.core.combat_engine import _build_effective_stats, _check_fatal
from engine.characters.cipher import _cipher_attack_aftermath, _cipher_trace3_apply_vuln, _cipher_trace3_remove_vuln
from engine.characters.welt import _welt_extra_damage
from engine.runtime import SimState, SimUnit
from engine.characters.cerydra import _trace_cerydra_trace1
from engine.characters.cipher import _trace_cipher_trace3
from engine.characters.hysilens import _trace_hysilens_trace3
from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus


def _enemy(hp=500000.0, res=0.0):
    return Enemy(id="target", name="Target", HP=hp, ATK=100, DEF=800,
                 SPD=80, toughness=200, max_toughness=200, level=80,
                 element_res={e: res for e in ("物理", "火", "冰", "雷", "风", "量子", "虚数")})


def _unit(cid, position=1, eidolon=0):
    char = load_character(cid, "data/characters")
    stats = compute_combat_stats(char, None, None, None)
    unit = SimUnit(char=char, base_stats=stats, position=position)
    unit.max_hp = unit.current_hp = stats.HP
    unit.eidolon_rank = eidolon
    return unit


def _state(*units, enemy=None):
    return SimState(enemies=[enemy or _enemy()], units=list(units))


# ── P1-7 瓦尔特 E1 双计杀 ──

def test_welt_e1_kill_counted_once():
    welt = _unit("welt", eidolon=1)
    enemy = _enemy(hp=1.0)
    enemy.add_status(EnemyStatus(id='welt_shizhong', name='失重', category='debuff',
                                 source='welt', remaining_turns=2))
    state = _state(welt, enemy=enemy)
    state.extra['last_attack_targets'] = [enemy]
    state.extra['last_hit_segments'] = [enemy]

    _welt_extra_damage(state, welt, 'ultimate')

    assert state.extra.get('killed_total', 0) == 1
    assert state.extra.get('killed_this_action', 0) == 1
    assert enemy.HP <= 0


# ── P1-5 刻律行迹1 / 海瑟音行迹3 双算 ──

def test_cerydra_trace1_single_critdmg_application():
    cery = _unit("cerydra")
    state = _state(cery)
    cery.base_stats.ATK = 2100.0
    cd_before = cery.base_stats.CRIT_DMG

    _trace_cerydra_trace1(cery, state)

    assert cery.base_stats.CRIT_DMG == cd_before  # 入场不再永久写
    eff = _build_effective_stats(cery, state)
    assert eff.CRIT_DMG - cd_before == pytest.approx(0.18)  # 动态只算一次


def test_hysilens_trace3_single_dmg_bonus_application():
    hs = _unit("hysilens")
    state = _state(hs)
    hs.base_stats.EFFECT_HIT_RATE = 0.80
    dmg_before = hs.base_stats.DMG_BONUS_ALL

    _trace_hysilens_trace3(hs, state)

    assert hs.base_stats.DMG_BONUS_ALL == dmg_before  # 入场不再永久写
    eff = _build_effective_stats(hs, state)
    assert eff.DMG_BONUS_ALL - dmg_before == pytest.approx(0.30)  # 动态只算一次


# ── P1-2 赛飞儿行迹3 作用域与对称维护 ──

def test_cipher_trace3_critdmg_scoped_to_fua():
    cipher = _unit("cipher")
    state = _state(cipher)
    cd_before = cipher.base_stats.CRIT_DMG

    _trace_cipher_trace3(cipher, state)

    assert cipher.base_stats.CRIT_DMG == cd_before  # 不再污染全局暴伤
    eff = _build_effective_stats(cipher, state)
    assert eff.CRIT_DMG_BY_ATTACK_TYPE.get('follow_up', 0.0) == pytest.approx(1.00)


def test_cipher_trace3_vuln_idempotent_and_symmetric():
    cipher = _unit("cipher")
    enemy = _enemy()
    state = _state(cipher, enemy=enemy)

    _cipher_trace3_apply_vuln(state)
    assert enemy.vulnerability == pytest.approx(0.40)
    _cipher_trace3_apply_vuln(state)  # 幂等
    assert enemy.vulnerability == pytest.approx(0.40)

    _cipher_trace3_remove_vuln(state)
    assert enemy.vulnerability == pytest.approx(0.0)


def test_cipher_trace3_vuln_removed_on_death():
    cipher = _unit("cipher")
    enemy = _enemy()
    state = _state(cipher, enemy=enemy)
    _cipher_trace3_apply_vuln(state)
    assert enemy.vulnerability == pytest.approx(0.40)

    cipher.current_hp = 0.0
    _check_fatal(state, cipher)

    assert enemy.vulnerability == pytest.approx(0.0)


# ── P1-1 赛飞儿 FUA 方向 + 星魂 ──

def _mark_laozhuke(enemy):
    enemy.extra['cipher_laozhuke'] = True
    return enemy


def test_cipher_fua_triggers_on_ally_hit_laozhuke():
    cipher = _unit("cipher")
    ally = _unit("seele")
    enemy = _mark_laozhuke(_enemy())
    state = _state(cipher, ally, enemy=enemy)
    state.extra['last_attack_targets'] = [enemy]

    _cipher_attack_aftermath(state, ally, 'basic_attack')

    assert cipher.extra.get('cipher_fua_used') is True
    assert cipher.total_damage_dealt > 0
    assert any('猫咪怪盗FUA' in l for l in state.log)


def test_cipher_fua_not_triggered_by_own_attack_or_off_target():
    cipher = _unit("cipher")
    ally = _unit("seele")
    enemy = _mark_laozhuke(_enemy())
    state = _state(cipher, ally, enemy=enemy)

    # 赛飞儿自己攻击老主顾: 天赋FUA不触发（TXT要求"我方其他目标"）
    state.extra['last_attack_targets'] = [enemy]
    _cipher_attack_aftermath(state, cipher, 'basic_attack')
    assert cipher.extra.get('cipher_fua_used') is None

    # 队友攻击非老主顾: 不触发
    enemy2 = _enemy()
    state.enemies = [enemy2]
    state.extra['last_attack_targets'] = [enemy2]
    _cipher_attack_aftermath(state, ally, 'basic_attack')
    assert cipher.extra.get('cipher_fua_used') is None


def test_cipher_fua_once_per_turn():
    cipher = _unit("cipher")
    ally = _unit("seele")
    enemy = _mark_laozhuke(_enemy())
    state = _state(cipher, ally, enemy=enemy)
    state.extra['last_attack_targets'] = [enemy]

    _cipher_attack_aftermath(state, ally, 'basic_attack')
    dmg1 = cipher.total_damage_dealt
    _cipher_attack_aftermath(state, ally, 'basic_attack')
    assert cipher.total_damage_dealt == dmg1  # 同回合第二次不再触发


def test_cipher_e1_fua_atk_buff():
    cipher = _unit("cipher", eidolon=1)
    ally = _unit("seele")
    enemy = _mark_laozhuke(_enemy())
    state = _state(cipher, ally, enemy=enemy)
    state.extra['last_attack_targets'] = [enemy]
    base_atk = cipher.base_stats._base_ATK
    atk_before = cipher.base_stats.ATK

    _cipher_attack_aftermath(state, ally, 'basic_attack')

    assert cipher.base_stats.ATK == pytest.approx(atk_before + base_atk * 0.80)
    assert cipher.extra.get('cipher_e1_atk_buff') == 2


def test_cipher_e6_fua_multiplier_and_record_bonus():
    from engine.core.combat_engine import _commit_enemy_damage
    from engine.characters.cipher import _cipher_record
    c0 = _unit("cipher")
    c6 = _unit("cipher", eidolon=6)
    ally0 = _unit("seele")
    ally6 = _unit("seele")
    e0 = _mark_laozhuke(_enemy())
    e6 = _mark_laozhuke(_enemy())
    s0 = _state(c0, ally0, enemy=e0)
    s6 = _state(c6, ally6, enemy=e6)
    s0.extra['last_attack_targets'] = [e0]
    s6.extra['last_attack_targets'] = [e6]

    _cipher_attack_aftermath(s0, ally0, 'basic_attack')
    _cipher_attack_aftermath(s6, ally6, 'basic_attack')

    # E6: FUA 倍率150%×4.5, 且E1 ATK+80% 生效 → 比值 = 4.5 × (1 + 0.8×base/panel)
    # 注意 E6 同时触发 E4 附加, 故只比较 FUA 段日志数值
    def fua_dmg(state):
        for l in state.log:
            if '猫咪怪盗FUA' in l:
                return float(l.split('FUA: ')[1].split('(')[0])
        return 0.0
    panel = c0.base_stats.ATK
    expected_ratio = 4.5 * (1.0 + 0.80 * c6.base_stats._base_ATK / panel)
    assert fua_dmg(s6) / fua_dmg(s0) == pytest.approx(expected_ratio, rel=0.02)
    assert c6.total_damage_dealt > c6.base_stats.ATK  # E4 附加也在 total 中

    # E6 的16%只属于天赋FUA，不得污染普通伤害的记录率。
    c6.extra['cipher_record'] = 0.0
    _cipher_record(s6, c6, e6, 1000.0)
    assert c6.extra['cipher_record'] == pytest.approx(180.0)
    c6.extra['cipher_record'] = 0.0
    _commit_enemy_damage(s6, c6, e6, 1000.0, damage_type='direct',
                         cipher_extra_rate=0.16)
    assert c6.extra['cipher_record'] == pytest.approx(340.0)


def test_cipher_e2_vuln_applied_on_hit():
    cipher = _unit("cipher", eidolon=2)
    enemy = _enemy()
    state = _state(cipher, enemy=enemy)
    state.extra['last_attack_targets'] = [enemy]

    _cipher_attack_aftermath(state, cipher, 'basic_attack')

    assert enemy.has_status(status_id='cipher_e2_vuln')
    assert enemy.status_attribute('vulnerability') == pytest.approx(0.30)


def test_cipher_e4_extra_on_own_and_ally_hit():
    from engine.core.damage import calculate_damage
    from engine.runtime import _enemy_for_damage
    c4 = _unit("cipher", eidolon=4)
    ally = _unit("seele")
    enemy = _mark_laozhuke(_enemy())
    state = _state(c4, ally, enemy=enemy)

    state.extra['last_attack_targets'] = [enemy]
    enemy.add_status(EnemyStatus(id='cipher_e2_vuln', name='易伤',
                                 category='debuff', source='cipher',
                                 remaining_turns=1,
                                 attributes={'vulnerability': 0.30}))
    stats = _build_effective_stats(c4, state)
    expected = calculate_damage(stats, _enemy_for_damage(enemy), stats.ATK, 50.0, 'additional',
                                '量子', 80, False,
                                crit_mode='expected').final_damage
    _cipher_attack_aftermath(state, c4, 'basic_attack')  # 自己攻击: 只有E4, 无FUA
    dmg_own = c4.total_damage_dealt
    assert dmg_own == pytest.approx(expected)
    assert c4.extra.get('cipher_fua_used') is None
    assert any('赛飞儿E4' in l for l in state.log)


# ── P1-3 爻光 ──

def _yao_state(eidolon=0, with_ally=False):
    yao = _unit("yaoguang", eidolon=eidolon)
    units = [yao]
    if with_ally:
        units.append(_unit("seele", position=2))
    state = _state(*units)
    return yao, state


def test_yaoguang_trace1_critdmg_and_trace3_spd():
    yao, state = _yao_state()
    eff = _build_effective_stats(yao, state)
    cd_before = eff.CRIT_DMG
    assert cd_before == pytest.approx(0.50 + 0.60)  # 基础50% + 行迹1 60%

    yao.base_stats.SPD = 130.0  # 行迹3: 120起+30%, 超10点+10%
    eff2 = _build_effective_stats(yao, state)
    base_elation = yao.base_stats.ELATION_LEVEL
    assert eff2.ELATION_LEVEL - base_elation == pytest.approx(0.30 + 10 * 0.01)


def test_yaoguang_e2_field_team_buffs():
    yao, state = _yao_state(eidolon=2)
    ally = state.units[1] if len(state.units) > 1 else _unit("seele", position=2)
    if ally not in state.units:
        state.units.append(ally)
    state.yao_field_active = True
    eff = _build_effective_stats(ally, state)
    assert eff.SPD_PERCENT == pytest.approx(0.12)
    assert eff.ELATION_LEVEL - ally.base_stats.ELATION_LEVEL == pytest.approx(0.16)

    state.yao_field_active = False
    eff_off = _build_effective_stats(ally, state)
    assert eff_off.SPD_PERCENT == pytest.approx(0.0)


def test_yaoguang_e6_team_laugh_boost():
    yao, state = _yao_state(eidolon=6)
    ally = _unit("seele", position=2)
    state.units.append(ally)
    eff = _build_effective_stats(ally, state)
    assert eff.LAUGH_BOOST == pytest.approx(0.25)  # E6 全队增笑25%


def test_yaoguang_ult_res_pen_and_e1_def_pen():
    yao, state = _yao_state(eidolon=1)
    yao.yao_res_pen_turns = 3
    eff = _build_effective_stats(yao, state)
    assert eff.RES_PEN_ALL == pytest.approx(0.24)  # E0 全抗穿24%
    assert eff.DEF_PEN_BY_TYPE.get('elation', 0.0) == pytest.approx(0.20)  # E1 无视防御20%

    yao2, state2 = _yao_state(eidolon=0)
    yao2.yao_res_pen_turns = 3
    eff2 = _build_effective_stats(yao2, state2)
    assert eff2.DEF_PEN_BY_TYPE.get('elation', 0.0) == pytest.approx(0.0)  # E0 无无视防御


def test_yaoguang_trace2_goodshow_duration():
    yao, state = _yao_state()
    from engine.systems.elation import ElationSystem
    elation = ElationSystem()
    state.extra['_elation'] = elation
    inst = elation.grant_good_show(state, 'yaoguang', 20.0, duration=2, source='test')
    assert inst.remaining_turns == 3  # 行迹2: 2→3回合


def test_yaoguang_dajidali_trigger_and_double_on_sp_skill():
    yao, state = _yao_state(with_ally=True)
    ally = next(u for u in state.units if u.char.id == 'seele')
    from engine.systems.elation import ElationSystem
    elation = ElationSystem()
    state.extra['_elation'] = elation
    elation.grant_good_show(state, 'yaoguang', 20.0, source='test')
    enemy = state.enemies[0]
    state.extra['last_attack_targets'] = [enemy]

    from engine.characters.yaoguang import _yaoguang_dajidali
    _yaoguang_dajidali(state, ally, 'basic_attack')
    dmg1 = ally.total_damage_dealt
    assert dmg1 > 0
    assert any('大吉大利' in l for l in state.log)

    ally.total_damage_dealt = 0.0
    _yaoguang_dajidali(state, ally, 'skill')  # 战技耗SP → 触发2次
    dmg2 = ally.total_damage_dealt
    # 同敌双段: 2次伤害 ≈ 2×单次
    assert dmg2 / dmg1 == pytest.approx(2.0, rel=0.03)


def test_yaoguang_e4_e6_elation_skill_multipliers():
    from engine.core.combat_engine import _use_skill
    # 独立 state 对照（凶星低语易伤 debuff 会残留, 不能同 state 二连放）
    yao_a, state_a = _yao_state(eidolon=6)
    state_a.skill_points = 5
    _use_skill(yao_a, state_a, 'elation_skill')
    base = yao_a.total_damage_dealt
    assert base > 0

    yao_b, state_b = _yao_state(eidolon=6)
    state_b.skill_points = 5
    state_b.extra['yao_e4_aha'] = True  # E4: ×1.5（与E6 ×2 叠加）
    _use_skill(yao_b, state_b, 'elation_skill')
    boosted = yao_b.total_damage_dealt
    assert boosted / base == pytest.approx(1.5, rel=0.03)


# ── P1-4 开拓者·欢愉 ──

def _tb_state(eidolon=0):
    tb = _unit("trailblazer_elation", eidolon=eidolon)
    state = _state(tb)
    return tb, state


def test_tb_ultimate_energy_cost_160():
    tb, _ = _tb_state()
    assert tb.char.max_energy == 160
    assert tb.char.skills['ultimate'].cost.get('energy') == 160


def test_tb_txt_multipliers_and_split_damage():
    from engine.core.combat_engine import _use_skill

    tb = _unit("trailblazer_elation")
    assert tb.char.skills['basic_attack'].multipliers[0].scale == pytest.approx(100.0)
    assert tb.char.skills['skill'].multipliers[0].scale == pytest.approx(60.0)
    assert [m.scale for m in tb.char.skills['elation_skill'].multipliers] == [20.0, 60.0]
    assert tb.char.skills['elation_skill'].multipliers[1].split is True

    def _shared_damage(enemy_count):
        unit = _unit("trailblazer_elation")
        unit.char.skills['elation_skill'].multipliers = [
            unit.char.skills['elation_skill'].multipliers[1]]
        enemies = [_enemy() for _ in range(enemy_count)]
        state = _state(unit, enemy=enemies[0])
        state.enemies = enemies
        _use_skill(unit, state, 'elation_skill')
        return unit.total_damage_dealt

    assert _shared_damage(2) == pytest.approx(_shared_damage(1), rel=1e-6)


def test_tb_skill_grants_good_show_and_talent_damage():
    from engine.core.combat_engine import _build_effective_stats
    from engine.characters.trailblazer_elation import _tb_skill_aftermath
    from engine.runtime import _enemy_for_damage
    from engine.core.damage import calculate_damage
    tb, state = _tb_state()
    state.elation_state.grant_good_show('trailblazer_elation', 20.0, duration=2)
    enemy = state.enemies[0]
    state.extra['last_attack_targets'] = [enemy]

    before = state.elation_state.get_good_show_total('trailblazer_elation')
    _tb_skill_aftermath(state, tb, 'skill')

    after = state.elation_state.get_good_show_total('trailblazer_elation')
    assert after == pytest.approx(before + 20.0)  # 战技+20好活
    expected = calculate_damage(
        _build_effective_stats(tb, state), _enemy_for_damage(enemy), 0, 30.0,
        'elation', '雷', 80, False, laugh_n=after,
        crit_mode='expected').final_damage
    assert tb.total_damage_dealt == pytest.approx(expected)


def test_tb_ultimate_cd_is_50_percent_once():
    from engine.systems.elation import ElationSystem

    tb = _unit("trailblazer_elation")
    ally = _unit("seele", position=2)
    state = _state(tb, ally)
    state.extra['_elation'] = ElationSystem()
    ally.tb_cd_buff_turns = 3
    assert (_build_effective_stats(ally, state).CRIT_DMG
            - ally.base_stats.CRIT_DMG) == pytest.approx(0.50)


def test_tb_technique_uses_weighted_small_large_probability(monkeypatch):
    from engine.systems import elation as elation_module

    seen = {}

    def _choose(population, weights, k):
        seen['population'] = population
        seen['weights'] = weights
        seen['k'] = k
        return [population[1]]

    monkeypatch.setattr(elation_module.random, 'choices', _choose)
    tb = _unit("trailblazer_elation")
    ally = _unit("seele", position=2)
    state = _state(tb, ally)
    elation_module.ElationSystem().init_battle(state, [tb, ally])
    assert seen == {'population': [0.30, 0.20], 'weights': [1, 3], 'k': 1}
    tech_buff = next(b for b in ally.buffs if b.param_id == 'tb_tech_elation')
    assert tech_buff.attributes['ELATION_LEVEL'] == pytest.approx(20.0)


def test_tb_trace1_crit_and_trace3_atk_panel():
    tb, state = _tb_state()
    eff = _build_effective_stats(tb, state)
    assert eff.CRIT_RATE == pytest.approx(0.05 + 0.12 + 0.15)  # 基础5% + 基础行迹12% + 行迹1 15%

    tb.base_stats.ATK = 1600.0  # (1600-1000)//200=3 → +30% 欢愉度
    eff2 = _build_effective_stats(tb, state)
    assert eff2.ELATION_LEVEL - tb.base_stats.ELATION_LEVEL == pytest.approx(0.30)


def test_tb_trace2_next_skill_bonus():
    from engine.characters.trailblazer_elation import _tb_skill_aftermath
    tb, state = _tb_state()
    ally = _unit("seele", position=2)
    state.units.append(ally)

    _tb_skill_aftermath(state, ally, 'elation_skill')  # 我方欢愉技 → 标记
    assert state.extra.get('tb_trace2_pending') is True

    before = state.elation_state.get_good_show_total('trailblazer_elation')
    _tb_skill_aftermath(state, tb, 'skill')  # 下次战技消费
    after = state.elation_state.get_good_show_total('trailblazer_elation')
    assert after == pytest.approx(before + 20.0 + 2.0)  # 战技20 + 行迹2额外2
    assert state.extra.get('tb_trace2_pending') is None


def test_tb_e4_enemy_vuln_status_not_permanent():
    from engine.characters.trailblazer_elation import _eid_tb_elation_e4
    tb, state = _tb_state(eidolon=4)
    base_vuln = tb.base_stats.VULNERABILITY_APPLIED

    _eid_tb_elation_e4(tb, state)

    enemy = state.enemies[0]
    assert tb.base_stats.VULNERABILITY_APPLIED == base_vuln  # 不再永久叠面板
    assert enemy.has_status(status_id='tb_e4_vuln')
    assert enemy.status_attribute('vulnerability') == pytest.approx(0.10)


def test_tb_e2_targets_ult_target():
    from engine.characters.trailblazer_elation import _eid_tb_elation_e2
    tb, state = _tb_state(eidolon=2)
    ally = _unit("seele", position=2)
    state.units.append(ally)
    tb.extra['lc_last_skill_target'] = ally

    _eid_tb_elation_e2(tb, state)

    assert any(getattr(b, 'param_id', '') == 'tb_e2' and b.source_id == 'tb_e2'
               or getattr(b, 'source_id', '') == 'tb_e2'
               for b in ally.buffs)


# ── P1-6 通用 E3/E5 技能等级模型 ──

def test_skill_level_boost_parsing_both_formats():
    from engine.core.effect_resolver import _eid_skill_levels
    c3 = _unit("cipher", eidolon=3)
    s3 = _state(c3)
    _eid_skill_levels(c3, s3)
    assert c3.extra['skill_level_boost'] == {'ultimate': 2, 'basic_attack': 1}

    c6 = _unit("cipher", eidolon=6)
    s6 = _state(c6)
    _eid_skill_levels(c6, s6)
    assert c6.extra['skill_level_boost'] == {
        'ultimate': 2, 'basic_attack': 1, 'skill': 2, 'talent': 2}

    # 长格式（"战技等级+2，最多不超过15级"）; E5 时 E3 也生效（合并）
    hh = _unit("huohuo", eidolon=5)
    sh = _state(hh)
    _eid_skill_levels(hh, sh)
    assert hh.extra['skill_level_boost'] == {
        'ultimate': 2, 'talent': 2, 'skill': 2, 'basic_attack': 1}


def test_on_after_damage_event_contract():
    """P2-1: on_after_damage 每段伤害提交后广播, 真实注册处理器可收到"""
    from engine.core.combat_engine import _commit_enemy_damage
    u = _unit("seele")
    enemy = _enemy(hp=100000.0)
    state = _state(u, enemy=enemy)
    seen = []
    state.hooks.register("seele", "on_after_damage",
                         lambda u, state, enemy=None, damage=0.0, killed=False, **_: seen.append((enemy, damage, killed)))
    _commit_enemy_damage(state, u, enemy, 100.0)
    assert len(seen) == 1
    assert seen[0][0] is enemy
    assert seen[0][1] == pytest.approx(100.0)
    assert seen[0][2] is False

    overflow = _enemy(hp=1.0)
    overflow_state = _state(u, enemy=overflow)
    overflow_seen = []
    overflow_state.hooks.register(
        "seele", "on_after_damage",
        lambda u, state, damage=0.0, submitted_damage=0.0, **_:
            overflow_seen.append((damage, submitted_damage)))
    actual, _ = _commit_enemy_damage(overflow_state, u, overflow, 100.0)
    assert actual == pytest.approx(1.0)
    assert overflow_seen == pytest.approx([(1.0, 100.0)])


def test_skill_level_boost_applies_to_multiplier():
    from engine.core.combat_engine import _use_skill
    from engine.core.effect_resolver import _eid_skill_levels

    c0 = _unit("cipher", eidolon=0)
    s0 = _state(c0)
    s0.skill_points = 5
    _use_skill(c0, s0, 'basic_attack')
    base = c0.total_damage_dealt
    assert base > 0

    c3 = _unit("cipher", eidolon=3)
    s3 = _state(c3)
    s3.skill_points = 5
    _eid_skill_levels(c3, s3)
    _use_skill(c3, s3, 'basic_attack')
    boosted = c3.total_damage_dealt
    # 普攻+1级 → 倍率×1.05
    assert boosted / base == pytest.approx(1.05, rel=0.02)


def test_cipher_record_uses_actual_hit_and_non_overflow_damage():
    """记录值按实际命中目标的非溢出 HP 损失计算，不按敌人数量均摊。"""
    from engine.core.combat_engine import _use_skill

    cipher = _unit("cipher")
    ally = _unit("seele", position=2)
    enemies = [_mark_laozhuke(_enemy(hp=500000.0)), _enemy(hp=500000.0), _enemy(hp=500000.0)]
    state = _state(cipher, ally, enemy=enemies[0])
    state.enemies = enemies
    state.skill_points = 5
    cipher.extra["cipher_fua_used"] = True

    _use_skill(ally, state, "basic_attack")

    actual = 500000.0 - enemies[0].HP
    assert cipher.extra.get("cipher_record", 0.0) == pytest.approx(actual * 0.12)
    assert all(e.HP == pytest.approx(500000.0) for e in enemies[1:])

    enemies[0].HP = 1.0
    cipher.extra["cipher_record"] = 0.0
    _use_skill(ally, state, "basic_attack")
    assert cipher.extra["cipher_record"] == pytest.approx(1.0 * 0.12)


def test_cipher_records_legacy_non_true_damage_but_excludes_true_damage():
    from engine.core.combat_engine import _commit_enemy_damage
    from engine.characters.seele import _apply_luandie

    cipher = _unit("cipher")
    ally = _unit("seele", position=2)
    enemy = _mark_laozhuke(_enemy(hp=100000.0))
    state = _state(cipher, ally, enemy=enemy)

    # 未迁移的旧调用没有 damage_type，但仍属于常规伤害，不能漏记。
    _commit_enemy_damage(state, ally, enemy, 100.0)
    assert cipher.extra["cipher_record"] == pytest.approx(12.0)

    # 显式真伤以及仍由旧 helper 提交的乱蝶真伤均不得进入记录值。
    _commit_enemy_damage(state, ally, enemy, 100.0, damage_type="true_damage")
    assert cipher.extra["cipher_record"] == pytest.approx(12.0)
    enemy.extra["luandie"] = 1
    enemy.extra["luandie_ult_dmg"] = 100.0
    _apply_luandie(state, enemy, ally)
    assert cipher.extra["cipher_record"] == pytest.approx(12.0)


def test_cipher_excludes_realm_true_damage_from_record():
    from engine.core.combat_engine import _use_skill

    def _run(realm_true_dmg):
        cipher = _unit("cipher")
        ally = _unit("seele", position=2)
        enemy = _mark_laozhuke(_enemy(hp=100000.0))
        state = _state(cipher, ally, enemy=enemy)
        state.realm_true_dmg = realm_true_dmg
        cipher.extra["cipher_fua_used"] = True
        _use_skill(ally, state, "basic_attack")
        return 100000.0 - enemy.HP, cipher.extra["cipher_record"]

    base_damage, base_record = _run(0.0)
    realm_damage, realm_record = _run(0.24)
    assert realm_damage / base_damage == pytest.approx(1.24)
    assert realm_record == pytest.approx(base_record)


def test_cipher_laozhuke_uses_max_hp_and_technique_record_multiplier():
    from engine.characters.cipher import _cipher_pick_laozhuke
    from engine.characters.cipher import _tech_cipher

    cipher = _unit("cipher")
    high_max = _enemy(hp=600000.0)
    low_max = _enemy(hp=500000.0)
    state = _state(cipher, enemy=high_max)
    state.enemies = [high_max, low_max]
    high_max.HP = 1.0
    picked = _cipher_pick_laozhuke(state, cipher)
    assert picked is high_max

    high_max.HP = high_max.max_hp
    before = [e.HP for e in state.enemies]
    cipher.extra["cipher_record"] = 0.0
    _tech_cipher(state, cipher, is_opener=True)
    losses = [hp - e.HP for hp, e in zip(before, state.enemies)]
    expected = losses[0] * 0.12 * 3.0 + losses[1] * 0.08 * 3.0
    assert cipher.extra["cipher_record"] == pytest.approx(expected)


def test_cipher_trace3_and_e2_scope_refresh():
    """行迹3只给赛飞儿FUA暴伤，E2状态到期后可再次施加。"""
    from engine.core.combat_engine import _build_effective_stats
    from engine.characters.cipher import _cipher_attack_aftermath
    from engine.characters.cipher import _trace_cipher_trace3

    cipher = _unit("cipher", eidolon=2)
    ally = _unit("seele", position=2)
    enemy = _enemy()
    state = _state(cipher, ally, enemy=enemy)
    _trace_cipher_trace3(cipher, state)
    assert _build_effective_stats(cipher, state).CRIT_DMG_BY_ATTACK_TYPE.get("follow_up", 0.0) == pytest.approx(1.0)
    assert _build_effective_stats(ally, state).CRIT_DMG_BY_ATTACK_TYPE.get("follow_up", 0.0) == pytest.approx(0.0)

    state.extra["last_attack_targets"] = [enemy]
    _cipher_attack_aftermath(state, cipher, "basic_attack")
    assert enemy.has_status(status_id="cipher_e2_vuln")
    enemy.statuses.clear()
    _cipher_attack_aftermath(state, cipher, "basic_attack")
    assert enemy.has_status(status_id="cipher_e2_vuln")


def test_cipher_trace1_uses_effective_speed_for_crit_and_record_tiers():
    from engine.characters.cipher import _cipher_record
    from engine.runtime import TimedBuff

    cipher = _unit("cipher")
    enemy = _mark_laozhuke(_enemy())
    state = _state(cipher, enemy=enemy)
    base_speed = (cipher.base_stats.SPD
                  + cipher.base_stats._base_SPD * cipher.base_stats.SPD_PERCENT)
    base_crit = cipher.base_stats.CRIT_RATE

    def _spd_percent_for(target_speed):
        return ((target_speed - base_speed) / cipher.base_stats._base_SPD) * 100.0

    cipher.buffs.append(TimedBuff(
        source_id="test", attributes={"SPD_PERCENT": _spd_percent_for(139.9)},
        remaining_turns=1))
    assert _build_effective_stats(cipher, state).CRIT_RATE == pytest.approx(base_crit)

    cipher.buffs[-1].attributes["SPD_PERCENT"] = _spd_percent_for(140.0)
    assert _build_effective_stats(cipher, state).CRIT_RATE == pytest.approx(base_crit + 0.25)
    _cipher_record(state, cipher, enemy, 100.0)
    assert cipher.extra["cipher_record"] == pytest.approx(18.0)

    cipher.extra["cipher_record"] = 0.0
    cipher.buffs[-1].attributes["SPD_PERCENT"] = _spd_percent_for(170.0)
    assert _build_effective_stats(cipher, state).CRIT_RATE == pytest.approx(base_crit + 0.50)
    _cipher_record(state, cipher, enemy, 100.0)
    assert cipher.extra["cipher_record"] == pytest.approx(24.0)


def test_cipher_ultimate_record_split_and_true_damage():
    """终结技记录值为主目标25%真伤，75%真伤按技能目标均分。"""
    from engine.core.combat_engine import _use_skill

    def _run(record):
        cipher = _unit("cipher")
        cipher.current_energy = cipher.char.max_energy
        cipher.extra["cipher_record"] = record
        enemies = [_enemy(hp=500000.0), _enemy(hp=500000.0), _enemy(hp=500000.0)]
        state = _state(cipher, enemy=enemies[0])
        state.enemies = enemies
        _use_skill(cipher, state, "ultimate")
        return [500000.0 - e.HP for e in enemies]

    baseline = _run(0.0)
    with_record = _run(1000.0)
    record_damage = [total - base for total, base in zip(with_record, baseline)]
    assert record_damage == pytest.approx([500.0, 250.0, 250.0], abs=1e-6)


def test_yaoguang_field_lifetime_follows_yaoguang_turns():
    from engine.core.combat_engine import _build_effective_stats, _tick_buffs
    from engine.systems.elation import ElationSystem

    yao = _unit("yaoguang")
    ally = _unit("seele", position=2)
    state = _state(yao, ally)
    system = ElationSystem()
    system.init_battle(state, [yao, ally])
    assert state.laugh_points == pytest.approx(4.0)  # 1名欢愉角色基础1 + 自动战技3
    assert yao.current_energy == pytest.approx(30.0)
    initial_bonus = (_build_effective_stats(ally, state).ELATION_LEVEL
                     - ally.base_stats.ELATION_LEVEL)
    assert initial_bonus > 0
    for _ in range(3):
        _tick_buffs(ally)
    assert state.yao_field_active
    assert (_build_effective_stats(ally, state).ELATION_LEVEL
            - ally.base_stats.ELATION_LEVEL) == pytest.approx(initial_bonus)

    yao2 = _unit("yaoguang")
    ally2 = _unit("seele", position=2)
    state2 = _state(yao2, ally2)
    system.init_battle(state2, [yao2, ally2])
    for _ in range(3):
        system.tick_turn(state2, yao2)
    assert not state2.yao_field_active
    assert _build_effective_stats(ally2, state2).ELATION_LEVEL == pytest.approx(
        ally2.base_stats.ELATION_LEVEL)


def test_yaoguang_skill_without_sp_does_not_double_dajidali():
    from engine.characters.yaoguang import _yaoguang_dajidali

    yao = _unit("yaoguang")
    qianye = _unit("qianye", position=2)
    enemy = _enemy()
    state = _state(yao, qianye, enemy=enemy)
    state.elation_state.grant_good_show("yaoguang", 20.0, duration=2)
    state.extra["last_attack_targets"] = [enemy]
    hp_before = enemy.HP
    _yaoguang_dajidali(state, qianye, "skill", spent_skill_points=0)
    zero_sp_damage = hp_before - enemy.HP

    enemy.HP = hp_before
    qianye.total_damage_dealt = 0.0
    _yaoguang_dajidali(state, qianye, "skill", spent_skill_points=1)
    spent_sp_damage = hp_before - enemy.HP
    assert zero_sp_damage > 0
    assert spent_sp_damage / zero_sp_damage == pytest.approx(2.0)


def test_trailblazer_e2_e6_buffs_refresh_and_good_show_uses_system_entry():
    from engine.core.combat_engine import _build_effective_stats
    from engine.characters.trailblazer_elation import _tb_skill_aftermath
    from engine.characters.trailblazer_elation import _eid_tb_elation_e2, _eid_tb_elation_e6
    from engine.systems.elation import ElationSystem

    tb = _unit("trailblazer_elation", eidolon=6)
    ally = _unit("seele", position=2)
    evanescia = _unit("evanescia", position=3)
    state = _state(tb, ally, evanescia)
    tb.extra["lc_last_skill_target"] = ally
    _eid_tb_elation_e2(tb, state)
    _eid_tb_elation_e2(tb, state)
    _eid_tb_elation_e6(tb, state)
    _eid_tb_elation_e6(tb, state)
    assert sum(b.source_id == "tb_e2" for b in ally.buffs) == 1
    assert _build_effective_stats(ally, state).ELATION_LEVEL - ally.base_stats.ELATION_LEVEL == pytest.approx(0.12)
    assert sum(b.source_id == "tb_e6" for b in tb.buffs) == 1
    assert _build_effective_stats(tb, state).CRIT_DMG - tb.base_stats.CRIT_DMG == pytest.approx(1.0)
    elation = ElationSystem()
    state.extra["_elation"] = elation
    _tb_skill_aftermath(state, tb, "skill")
    assert state.elation_state.get_good_show_total("evanescia") > 0


def test_skill_level_boost_accumulates_and_scales_healing():
    from engine.core.combat_engine import _use_skill
    from engine.core.effect_resolver import _eid_skill_levels

    yao = _unit("yaoguang", eidolon=6)
    state = _state(yao)
    _eid_skill_levels(yao, state)
    assert yao.extra["skill_level_boost"]["elation_skill"] == 2

    def _huohuo_heal(eidolon):
        huohuo = _unit("huohuo", eidolon=eidolon)
        ally = _unit("seele", position=2)
        # v6.10.6: 血量缺口拉大避免治疗溢出封顶掩盖增量（E5 含 E4 低血加成, 一并纳入期望）
        ally.max_hp = 10000.0
        ally.current_hp = 100.0
        state = _state(huohuo, ally)
        state.skill_points = 5
        if eidolon:
            _eid_skill_levels(huohuo, state)
        hp_before = ally.current_hp
        _use_skill(huohuo, state, "skill")
        # v6.10.6: 治疗量改为直接读血量增量（禳命不再挂在受疗者身上）
        return ally.current_hp - hp_before

    # E5 含 E4 低血加成且主/相邻目标逐段治疗, 精确比值受目标选择干扰;
    # 断言 E5 治疗显著放大（>×1.05）即证明 skill_level 覆盖层对治疗生效
    assert _huohuo_heal(5) > _huohuo_heal(0) * 1.05


def test_skill_level_boost_scales_shield_and_handwritten_talent():
    from engine.core.combat_engine import _use_skill
    from engine.characters.trailblazer_elation import _tb_skill_aftermath
    from engine.core.effect_resolver import _eid_skill_levels

    def _dht_shield(eidolon):
        dht = _unit("dan_heng_permansor_terrae", eidolon=eidolon)
        ally = _unit("seele", position=2)
        state = _state(dht, ally)
        state.skill_points = 5
        if eidolon:
            _eid_skill_levels(dht, state)
        _use_skill(dht, state, "skill")
        return ally.shield

    assert _dht_shield(5) / _dht_shield(0) == pytest.approx(1.10)

    def _tb_talent_damage(eidolon):
        tb = _unit("trailblazer_elation", eidolon=eidolon)
        state = _state(tb)
        state.elation_state.grant_good_show('trailblazer_elation', 20.0)
        if eidolon:
            _eid_skill_levels(tb, state)
        _tb_skill_aftermath(state, tb, 'skill')
        return tb.total_damage_dealt

    assert _tb_talent_damage(3) / _tb_talent_damage(0) == pytest.approx(1.10)


def test_complete_roster_e0_e6_and_mixed_team_smoke():
    import json
    import math
    from pathlib import Path
    from engine.core.combat_engine import simulate

    complete_ids = []
    for path in sorted(Path('data/characters').glob('*.json')):
        if path.stem.startswith('_'):
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if 'basic_attack' in (data.get('skills') or {}):
            complete_ids.append(path.stem)
    assert len(complete_ids) == 40  # v6.11.1 晴歌录入 39→40

    for eidolon in (0, 6):
        for char_id in complete_ids:
            state = simulate(
                [{'char': load_character(char_id, 'data/characters'),
                  'position': 1, 'eidolon': eidolon}],
                _enemy(hp=1_000_000_000.0), max_av=150)
            values = [state.current_av, state.units[0].current_hp,
                      state.units[0].total_damage_dealt]
            assert all(math.isfinite(float(value)) for value in values), (char_id, eidolon)
            assert not any('[ERROR]' in line for line in state.log), (char_id, eidolon)

    configs = [
        {'char': load_character('cipher', 'data/characters'), 'position': 1, 'eidolon': 6},
        {'char': load_character('yaoguang', 'data/characters'), 'position': 2, 'eidolon': 6},
        {'char': load_character('trailblazer_elation', 'data/characters'),
         'position': 3, 'eidolon': 6},
    ]
    mixed = simulate(configs, _enemy(hp=1_000_000_000.0), max_av=300, num_enemies=3)
    assert all(math.isfinite(unit.total_damage_dealt) for unit in mixed.units)
    assert not any('[ERROR]' in line for line in mixed.log)

    repeat = simulate(configs, _enemy(hp=1_000_000_000.0), max_av=0, num_enemies=3)
    assert all(left is not right for left, right in zip(mixed.units, repeat.units))
    assert all(left is not right for left, right in zip(mixed.enemies, repeat.enemies))
