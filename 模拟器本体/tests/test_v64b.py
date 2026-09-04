"""v6.4b 回归测试: Codex v6.4 审查 4 项修复（Harness 端修正）

语义依据: CODEX_HANDOFF.md v6.4 审查记录节 + HARNESS_HANDOFF.md v6.4b 修复记录"""
import pytest
from unittest import mock

from engine.models.character import load_character
from engine.models.enemy import Enemy, EnemyStatus
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _enemy_attack, _enemy_attack_stats, _enemy_eff_spd, _begin_enemy_turn
from engine.runtime import SimState, SimUnit


def _enemy(**kw):
    base = dict(id='x', name='X', HP=500000, ATK=600, DEF=800, SPD=80,
                toughness=200, max_toughness=200, level=80,
                element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                             '虚数': 0, '物理': 0, '火': 0})
    base.update(kw)
    return Enemy(**base)


def _unit(cid='seele', position=1):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    return u


# ── D1: 全部受限技能 no-op（Codex P1-2） ──

class TestAllRestrictedNoOp:
    def test_no_skill_released_and_av_still_advances(self):
        u = _unit()
        e = _enemy()
        e.attacks = [
            {'name': '强化A', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 2,
             'requires_buff': 'state_a'},
            {'name': '强化B', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 1,
             'requires_buff': 'state_b'},
        ]
        state = SimState(enemies=[e], units=[u])
        state.extra['navs'] = {}
        hp0 = u.current_hp
        _begin_enemy_turn(state, e)
        log = '\n'.join(state.log)
        assert '无可用技能' in log
        assert u.current_hp == hp0  # 未造成伤害
        assert not any(s.category == 'buff' for s in e.statuses)  # 未施加自增益
        # AV 仍在 _begin_enemy_turn ⑤ 推进（无行动条死循环）
        assert ('e', 0) in state.extra['navs']


# ── D2: SPD 别名兼容（Codex P1-3） ──

class TestSpdAlias:
    def test_spd_percent_lowercase(self):
        e = _enemy()
        e.statuses.append(EnemyStatus(id='b', name='加速', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_percent': 0.25}))
        assert _enemy_eff_spd(e) == pytest.approx(80.0 * 1.25)

    def test_spd_percent_uppercase(self):
        e = _enemy()
        e.statuses.append(EnemyStatus(id='b', name='加速', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_PERCENT': 0.25}))
        assert _enemy_eff_spd(e) == pytest.approx(80.0 * 1.25)

    def test_mixed_sources_accumulate(self):
        e = _enemy()
        e.statuses.append(EnemyStatus(id='b1', name='加速', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_percent': 0.25}))
        e.statuses.append(EnemyStatus(id='b2', name='加速2', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_PERCENT': 0.25}))
        assert _enemy_eff_spd(e) == pytest.approx(80.0 * 1.50)

    def test_single_status_dual_keys_counted_once(self):
        e = _enemy()
        e.statuses.append(EnemyStatus(id='b', name='加速', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'SPD_PERCENT': 0.25,
                                                  'SPD_percent': 0.25}))
        assert _enemy_eff_spd(e) == pytest.approx(80.0 * 1.25)  # 防双键重复叠加


# ── D3: debuff main 契约=初选目标（Codex P2） ──

class TestDebuffMainTarget:
    def test_main_applies_to_primary_even_if_bounce_misses_it(self):
        a = _unit('seele', position=1)
        b = _unit('fu_xuan', position=2)
        e = _enemy()
        e.attacks = [{'name': '弹射', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 50.0, 'target_type': 'bounce', 'hits': 2,
                      'priority': 1,
                      'debuffs': [{'id': 'main_only', 'name': '只打主目标',
                                   'category': 'control', 'duration': 1,
                                   'base_chance': 1.0, 'target': 'main'}]}]
        state = SimState(enemies=[e], units=[a, b])
        # 初选主目标=a; 弹射每跳都命中 b（a 全程未被弹射命中）
        with mock.patch('engine.core.combat_engine._select_enemy_target',
                        side_effect=[a, b, b]):
            _enemy_attack(state, e)
        # main 契约: 施加给初选目标 a, 而非弹射实际命中的 b
        assert any(s.id == 'main_only' for s in a.statuses)
        assert not any(s.id == 'main_only' for s in b.statuses)


# ── D4: 眩晕+回能覆盖保留（原 test_brute 断言迁移） ──

class TestSyntheticStunEnergy:
    def test_stun_and_energy_gain(self):
        u = _unit()
        e = _enemy()
        e.attacks = [{'name': '咆哮', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 50.0, 'target_type': 'single_enemy',
                      'priority': 1,
                      'debuffs': [{'id': 'stun', 'name': '眩晕',
                                   'category': 'control', 'duration': 1,
                                   'base_chance': 1.0}],
                      'energy_gain': 5}]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        assert any(s.name == '眩晕' for s in u.statuses)
        assert u.current_energy == pytest.approx(5.0, abs=1e-6)


# ── D5: 子代理独立深审补漏（命名空间隔离/防御性编码） ──

class TestSelfBuffNamespace:
    def test_requires_buff_ignores_same_id_player_debuff(self):
        """同名玩家 debuff（非 buff 类）不得让 requires_buff 误判满足"""
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='rage', name='缺陷', category='debuff',
                                      source='silver_wolf', remaining_turns=3,
                                      attributes={'def_reduction': 0.12}))
        e.attacks = [
            {'name': '普通', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1},
            {'name': '狂暴挥击', 'element': '物理', 'damage_type': 'direct',
             'multiplier': 150.0, 'target_type': 'single_enemy', 'priority': 5,
             'requires_buff': 'rage'},
        ]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        assert '狂暴挥击' not in '\n'.join(state.log)
        assert '普通' in '\n'.join(state.log)

    def test_self_buff_does_not_clobber_player_debuff(self):
        """self_buff id 与玩家负面状态撞名 → 跳过施加, 玩家 debuff 保留"""
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='rage', name='缺陷', category='debuff',
                                      source='silver_wolf', remaining_turns=3,
                                      attributes={'def_reduction': 0.12}))
        e.attacks = [{'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1,
                      'self_buffs': [{'id': 'rage', 'name': '狂暴',
                                      'attributes': {'ATK_PERCENT': 0.5},
                                      'duration': 2}]}]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        st = next(s for s in e.statuses if s.id == 'rage')
        assert st.category == 'debuff'  # 未被自增益覆盖
        assert 'ATK_PERCENT' not in st.attributes
        assert any('跳过施加' in l for l in state.log)

    def test_self_buff_refresh_uses_max(self):
        """同 id 刷新: 剩余回合取 max（与 _apply_player_status 语义一致）"""
        u = _unit()
        e = _enemy()
        e.statuses.append(EnemyStatus(id='rage', name='狂暴', category='buff',
                                      source='x', remaining_turns=5,
                                      attributes={'ATK_PERCENT': 0.5}))
        e.attacks = [{'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1,
                      'self_buffs': [{'id': 'rage', 'name': '狂暴',
                                      'attributes': {'ATK_PERCENT': 0.5},
                                      'duration': 2}]}]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        st = next(s for s in e.statuses if s.id == 'rage')
        assert st.remaining_turns == 5  # 不被 2 缩短

    def test_self_buff_missing_id_does_not_crash(self):
        """self_buffs 缺 id 兜底不崩"""
        u = _unit()
        e = _enemy()
        e.attacks = [{'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
                      'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 1,
                      'self_buffs': [{'name': '无名增益',
                                      'attributes': {'ATK_PERCENT': 0.5},
                                      'duration': 2}]}]
        state = SimState(enemies=[e], units=[u])
        _enemy_attack(state, e)
        assert any(s.id == 'self_buff:无名增益' for s in e.statuses)

    def test_atk_percent_lowercase_alias(self):
        """ATK_percent 小写别名与 ATK_PERCENT 等效"""
        e = _enemy()
        e.statuses.append(EnemyStatus(id='b', name='狂暴', category='buff',
                                      source='x', remaining_turns=2,
                                      attributes={'ATK_percent': 0.5}))
        assert _enemy_attack_stats(e).ATK == pytest.approx(600.0 * 1.5)
