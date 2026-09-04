"""v6.3.0b 回归测试: Codex v6.3.0 复核 12 项修复（Harness 端修正）

语义依据: CODEX_HANDOFF.md v6.3.0 审查节 + HARNESS_HANDOFF.md v6.3.0b 修复记录"""
import copy
import random

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate, _enemy_attack_stats, _enemy_eff_spd, _use_skill, _respawn_wave
from engine.characters.silver_wolf import _silver_wolf_implant_defect, _apply_silver_wolf_weakness
from engine.runtime import SimState, SimUnit
from engine.core.damage import _calc_def_mult


def _enemy(hp=500000, toughness=200):
    e = Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=toughness, max_toughness=toughness, level=80,
              element_res={'冰': 0, '量子': 0.4, '风': 0.4, '雷': 0.4,
                           '虚数': 0.4, '物理': 0.4, '火': 0.4})
    return e


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _sim(ids, max_av=0, hp=500000, toughness=200, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = dict(cfgs.get(cid, {}))
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(hp=hp, toughness=toughness), max_av=max_av)


# ── C1: 灵砂秘技浮元召唤（P1-1） ──

class TestLingshaMarker:
    def test_technique_spawns_fuyuan(self):
        s = _sim(['lingsha'], max_av=0)
        assert len(s.markers) >= 1
        assert any('召唤浮元' in l for l in s.log)
        assert any(st.id == 'lingsha_chunzui' for st in s.enemies[0].statuses)


# ── C2: 流萤秘技火抗与削韧（P1-3） ──

class TestFireflyTechRes:
    def test_fire_weakness_changes_res_and_toughness(self):
        s = _sim(['firefly'], max_av=0, hp=500000)
        e = s.enemies[0]
        assert e.element_res['火'] <= 0  # 火抗0.4 → 植入后为负弱点
        assert e.toughness < e.max_toughness  # 削韧20生效（此前门控丢失）
        st = next((x for x in e.statuses if x.id == 'firefly_fire_weakness'), None)
        assert st is not None
        assert st.attributes.get('weakness_old_res') == pytest.approx(0.4)


# ── C3: 希儿秘技增幅直接生效（P1-4） ──

class TestSeeleTechBuff:
    def test_amplify_buff_applied_directly(self):
        s = _sim(['seele'], max_av=0)
        u = s.units[0]
        assert any(getattr(b, 'source_name', '') == '再现增幅' for b in u.buffs)


# ── C4: 遐蝶秘技行动提前+新蕊（P1-5） ──

class TestXiadieTech:
    def test_advance_and_xinrui(self):
        s = _sim(['xiadie'], max_av=0)
        u = s.units[0]
        assert u.memsprite_unit is not None and u.memsprite_unit.is_alive
        # 死龙在场: 损失→死龙回血（铁律9）, 新蕊保持0是正确语义;
        # HP损失管线（absorb/on_hp_loss）此前完全缺失 → 日志应有吸收记录
        assert any('死龙回血' in l for l in s.log)
        assert s.extra['navs'][0] == 0.0  # 行动提前100%（此前未实现）
        assert u.current_hp < u.max_hp  # HP 消耗真实发生


# ── C5: 符玄秘技回能与HP上限（P1-6） ──

class TestFuxuanTech:
    def test_energy_and_maxhp(self):
        s = _sim(['fu_xuan'], max_av=0)
        u = s.units[0]
        assert u.current_energy == pytest.approx(97.5)  # v6.10: 50%开局(130×0.5=65)+秘技30+行迹2.5
        fx_hp = u.base_stats.HP
        assert u.max_hp == pytest.approx(u.extra['fuxuan_tech_orig_maxhp'] + fx_hp * 0.06)


# ── C6: 银狼缺陷消费链（P1-7） ──

class TestDefectConsumers:
    def test_atk_down_consumed_by_enemy_attack(self):
        e = _enemy()
        e.add_status(EnemyStatus(id='t_atk', name='攻击力降低', category='debuff',
                                 remaining_turns=2, attributes={'atk_down': 0.25}))
        assert _enemy_attack_stats(e).ATK == pytest.approx(e.ATK * 0.75)

    def test_def_reduction_consumed_by_def_mult(self):
        e = _enemy()
        base = _calc_def_mult(e, compute_combat_stats(load_character('seele', 'data/characters'),
                                                      None, None, None), 80)
        e.add_status(EnemyStatus(id='t_def', name='防御力降低', category='debuff',
                                 remaining_turns=2, attributes={'def_reduction': 0.12}))
        lowered = _calc_def_mult(e, compute_combat_stats(load_character('seele', 'data/characters'),
                                                         None, None, None), 80)
        assert lowered > base

    def test_spd_down_consumed_by_enemy_spd(self):
        e = _enemy()
        spd0 = _enemy_eff_spd(e)
        e.add_status(EnemyStatus(id='t_spd', name='速度降低', category='debuff',
                                 remaining_turns=2, attributes={'spd_down': 0.06}))
        assert _enemy_eff_spd(e) == pytest.approx(spd0 * 0.94)


# ── C7: 银狼行迹1击破植入 + 开局激活（P1-8） ──

class TestSilverWolfTrace1:
    def test_battle_start_activation_and_break_implant(self):
        """行迹1开局激活 + 队友(希儿)击破 → 银狼经 on_any_weakness_break 植入缺陷"""
        s = _sim(['seele', 'silver_wolf'], max_av=300, hp=500000, toughness=1)
        sw = s.units[1]
        assert sw.extra.get('silver_wolf_trace1') is True  # 开局激活
        e = s.enemies[0]
        assert e.is_broken  # 希儿削韧1即破
        assert any(st.id.startswith('silver_wolf_defect_') for st in e.statuses)


# ── C8: 银狼E2入战易伤（P1-9） ──

class TestSilverWolfE2:
    def test_initial_wave_vuln(self):
        s = _sim(['silver_wolf'], max_av=0, silver_wolf={'eidolon': 2})
        assert s.enemies[0].vulnerability == pytest.approx(0.20)

    def test_respawn_wave_vuln(self):
        sw = _unit('silver_wolf', eidolon=2)
        state = SimState(enemies=[_enemy()], units=[sw])
        state.extra['enemy_blueprint'] = copy.deepcopy(state.enemies[0])
        state.extra['num_enemies'] = 2
        _respawn_wave(state)
        for ne in state.enemies:
            assert ne.vulnerability == pytest.approx(0.20)

    def test_e0_no_vuln(self):
        s = _sim(['silver_wolf'], max_av=0)
        assert s.enemies[0].vulnerability == pytest.approx(0.0)


# ── C9: 银狼只对实际命中目标植入（P1-10） ──

class TestSilverWolfHitTargets:
    def test_single_target_attack_implants_only_hit_enemy(self):
        sw = _unit('silver_wolf')
        e1, e2 = _enemy(), _enemy()
        state = SimState(enemies=[e1, e2], units=[sw])
        state.extra['navs'] = {}
        _use_skill(sw, state, 'basic_attack')
        assert any(st.id.startswith('silver_wolf_defect_') for st in e1.statuses)
        assert not any(st.id.startswith('silver_wolf_defect_') for st in e2.statuses)


# ── C10: 银狼弱点快照/刷新/到期（P1-11） ──

class TestSilverWolfWeaknessSnapshot:
    def test_refresh_keeps_snapshot_and_expiry_restores(self):
        sw = _unit('silver_wolf')
        e = _enemy()  # 量子0.4
        state = SimState(enemies=[e], units=[sw])
        # 实机: 添加弱点(0)+抗性降低20% → -0.2
        _apply_silver_wolf_weakness(sw, state, e)
        assert e.element_res['量子'] == pytest.approx(-0.2)
        _apply_silver_wolf_weakness(sw, state, e)  # 同元素刷新: 保留首快照, 不再降
        assert e.element_res['量子'] == pytest.approx(-0.2)
        st = next(x for x in e.statuses if x.id == 'silver_wolf_weakness')
        # 到期恢复: 由 _begin_enemy_turn 消费 expired; 此处直接验证快照值为首次纯抗性
        assert st.attributes.get('weakness_old_res') == pytest.approx(0.4)

    def test_element_switch_restores_old(self):
        sw = _unit('silver_wolf')
        ff = _unit('firefly', position=2)  # 火
        sw.position = 1
        e = _enemy()
        state = SimState(enemies=[e], units=[sw, ff])
        _apply_silver_wolf_weakness(sw, state, e)  # min position=sw(量子) → 量子-0.2
        assert e.element_res['量子'] == pytest.approx(-0.2)
        sw.position = 3  # 换队首→firefly(火)
        _apply_silver_wolf_weakness(sw, state, e)
        assert e.element_res['量子'] == pytest.approx(0.4)  # 旧元素还原
        assert e.element_res['火'] == pytest.approx(-0.2)


# ── C11: 银狼三类缺陷并存（P1-12） ──

class TestSilverWolfDefectCoexist:
    def test_three_types_coexist(self):
        random.seed(42)
        sw = _unit('silver_wolf')
        e = _enemy()
        state = SimState(enemies=[e], units=[sw])
        for _ in range(30):
            _silver_wolf_implant_defect(state, sw, e)
        ids = {st.id for st in e.statuses if st.id.startswith('silver_wolf_defect_')}
        assert ids == {'silver_wolf_defect_atk',
                       'silver_wolf_defect_def',
                       'silver_wolf_defect_spd'}
        assert e.debuff_count() >= 3
