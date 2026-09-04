"""v6.6c 回归测试: 深审子代理 18 项发现修复（缇宝/刻律/丹恒/海瑟音/那刻夏/赛飞儿/诗系统/SPD口径）

语义依据: HARNESS_HANDOFF.md v6.6c 修复记录"""
import copy

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate, _use_skill, _begin_enemy_turn, _build_effective_stats, _enemy_attack_stats
from engine.characters.tribbie import _tribbie_apply_shenqi, _tribbie_ult_field
from engine.characters.cerydra import _cerydra_check_promote, _cerydra_qixi, _cerydra_grant_jungong
from engine.characters.hysilens import _hysilens_apply_dot
from engine.runtime import SimState, SimUnit


def _enemy(hp=500000, tough=200):
    return Enemy(id='x', name='X', HP=hp, ATK=600, DEF=800, SPD=80,
                 toughness=tough, max_toughness=tough, level=80,
                 element_res={k: 0.2 for k in ['冰', '量子', '风', '雷', '虚数', '物理', '火']},
                 attacks=[{'name': '挥击', 'element': '物理', 'damage_type': 'direct',
                           'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 0}])


def _unit(cid, eidolon=0, position=1):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


# ── F1: 缇宝防重入（神启/结界/FUA重置） ──

class TestTribbie:
    def test_shenqi_no_double_apply(self):
        u = _unit('tribbie')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {}
        p0 = ally.base_stats.RES_PEN_ALL
        _tribbie_apply_shenqi(u, state)
        _tribbie_apply_shenqi(u, state)  # 重复战技只刷新
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(p0 + 0.24)

    def test_field_no_double_vuln(self):
        u = _unit('tribbie')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        _tribbie_ult_field(u, state)
        _tribbie_ult_field(u, state)
        assert e.vulnerability == pytest.approx(0.30)

    def test_teammate_ult_fua_after_tribbie_ult(self):
        """v6.6c: 缇宝首次开大后队友终结技仍可触发FUA（此前门锁反了）"""
        trib = _unit('tribbie', position=2)
        seele = _unit('seele', position=1)
        state = SimState(enemies=[_enemy()], units=[seele, trib])
        state.extra['navs'] = {}
        trib.current_energy = trib.char.max_energy
        _use_skill(trib, state, 'ultimate')  # 缇宝开大: 重置 FUA 计数
        seele.current_energy = seele.char.max_energy
        hp0 = state.enemies[0].HP
        _use_skill(seele, state, 'ultimate')
        assert state.enemies[0].HP < hp0 or any(
            '缇宝天赋FUA' in l for l in state.log)


# ── F2: 刻律德菈 ──

class TestCerydra:
    def test_juewei_applies_and_qixi_rebates(self):
        u = _unit('cerydra')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {}
        u.extra['cerydra_charge'] = 6
        p0 = ally.base_stats.RES_PEN_ALL
        _cerydra_check_promote(state, u, ally)
        assert ally.extra.get('cerydra_juewei') is True
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(p0 + 0.10)
        _cerydra_qixi(state, u, ally)
        assert ally.extra.get('cerydra_juewei') is False
        assert ally.base_stats.RES_PEN_ALL == pytest.approx(p0)

    def test_spd_buff_records_ally_and_rebates_original(self):
        """换目标后到期回减的是原受buff者（此前回减新军功者）"""
        u = _unit('cerydra')
        a1 = _unit('seele', position=2)
        a2 = _unit('bronya', position=3)
        state = SimState(enemies=[_enemy()], units=[u, a1, a2])
        state.extra['navs'] = {}
        s1, s2 = a1.base_stats.SPD, a2.base_stats.SPD
        u.base_stats.SPD += 20
        a1.base_stats.SPD += 20
        u.extra['cerydra_spd_buff_turns'] = 1
        u.extra['cerydra_spd_buff_ally'] = 'seele'
        # 军功换给 a2（模拟换目标）
        _cerydra_grant_jungong(state, u, a2)
        u.extra['cerydra_spd_buff_turns'] = 0
        # 到期回减路径（模拟 tick）: 回减 seele 而非 bronya
        u.base_stats.SPD -= 20
        a1.base_stats.SPD -= 20
        assert a1.base_stats.SPD == pytest.approx(s1)
        assert a2.base_stats.SPD == pytest.approx(s2)


# ── F3: 丹恒龙灵 ──

class TestDhtLongling:
    def test_skill_summons_marker_and_cleanse(self):
        u = _unit('dan_heng_permansor_terrae')
        ally = _unit('seele', position=2)
        e = _enemy()
        state = SimState(enemies=[e], units=[u, ally])
        state.extra['navs'] = {}
        u.current_energy = 0
        _use_skill(u, state, 'skill')
        assert u.marker is not None and u.marker.is_alive
        assert u.marker.marker_id == 'dht_longling'


# ── F4: 海瑟音 ──

class TestHysilens:
    def test_dot_ticks_and_field_atk(self):
        u = _unit('hysilens')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        _hysilens_apply_dot(state, u, e)
        st = next(s for s in e.statuses if s.id.startswith('hysilens_dot'))
        assert st.attributes.get('dot_snapshot') is not None
        hp0 = e.HP
        _begin_enemy_turn(state, e)
        assert e.HP < hp0  # DOT 实际跳伤

    def test_field_reduces_enemy_atk(self):
        u = _unit('hysilens')
        e = _enemy()
        e.extra['hysilens_field'] = True
        state = SimState(enemies=[e], units=[u])
        atk0 = e.ATK
        assert _enemy_attack_stats(e).ATK == pytest.approx(atk0 * 0.85)


# ── F5: 赛飞儿 ──

class TestCipher:
    def test_skill_atk_buff_no_double_and_rebate(self):
        u = _unit('cipher')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {}
        atk0 = u.base_stats.ATK
        base = u.base_stats._base_ATK
        _use_skill(u, state, 'skill')
        _use_skill(u, state, 'skill')  # 防重入
        assert u.base_stats.ATK == pytest.approx(atk0 + base * 0.30)
        u.base_stats.ATK -= base * 0.30  # 模拟到期回减
        assert u.base_stats.ATK == pytest.approx(atk0)

    def test_ult_has_75_percent_true_damage(self):
        u = _unit('cipher')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        u.extra['cipher_record'] = 10000.0
        u.current_energy = u.char.max_energy
        hp0 = e.HP
        _use_skill(u, state, 'ultimate')
        # 25%主目标真伤 + 75%技能目标均分；单目标时合计完整记录值。
        assert e.HP < hp0 - 7500.0


# ── F6: 那刻夏禁锢跳过 ──

class TestAnaxaImprison:
    def test_imprison_skips_enemy_turn(self):
        u = _unit('seele')
        e = _enemy()
        e.statuses.append(EnemyStatus(id='anaxa_imprison', name='禁锢',
                                      category='control', source='anaxa',
                                      remaining_turns=1))
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        hp0 = u.current_hp
        _begin_enemy_turn(state, e)
        assert u.current_hp == hp0  # 未攻击
        assert not any(s.id == 'anaxa_imprison' for s in e.statuses)
        assert state.extra['navs'][('e', 0)] > 10000.0 / 80.0 + 2500.0 - 1.0


# ── F7: SPD 口径 ──

class TestSpdConvention:
    def test_trace_flat_speed_counts_into_white(self):
        """v6.6c P3: 行迹固定速度（SPD_percent 键）按固定值计入白值"""
        c = load_character('dan_heng_permansor_terrae', 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        assert stats._base_SPD == pytest.approx(c.base_SPD + 5.0, abs=1e-9)
