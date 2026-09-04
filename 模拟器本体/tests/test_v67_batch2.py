"""v6.7 批2 回归: 姬子·启行（助战技子系统）

语义依据: 角色技能介绍/智识/姬子·启行.txt + CLAUDE_HANDOFF v6.7 节
用户确认（2026-08-15）: 基础属性以 txt 手抄为准（ATK 756）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _gain_energy, _build_effective_stats
from engine.characters.himeko_nova import _hn_support_skill, _hn_support_cap, _hn_ultimate, _hn_count_ally_ult, _hn_count_hits, _hn_try_protocol_support
from engine.runtime import SimState, SimUnit
from engine.characters.himeko_nova import _tech_himeko_nova


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': -0.2})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _hn_state(*ids, eidolon=0):
    """构造含姬子·启行的战斗状态（含协议激活）"""
    units = [_unit('himeko_nova', position=1, eidolon=eidolon)]
    for i, cid in enumerate(ids):
        units.append(_unit(cid, position=i + 2))
    state = SimState(enemies=[_enemy()], units=units)
    state.extra['hn_support_uses'] = 1
    state.extra['hn_protocol_uses'] = 2
    return state


class TestHimekoNova:
    def test_support_uses(self):
        """助战技次数: 初始1(天赋); 姬子使用不消耗(行迹1); 非姬子使用减1"""
        state = _hn_state('seele')
        hn = state.units[0]
        ally = state.units[1]
        _hn_support_skill(state, hn)  # 姬子自己用
        assert state.extra['hn_support_uses'] == 1  # 不消耗
        _hn_support_skill(state, ally)
        assert state.extra['hn_support_uses'] == 0  # 队友消耗

    def test_support_uses_himeko_stats(self):
        """助战技用姬子面板: 提高姬子ATK→伤害同比提升"""
        state = _hn_state()
        hn = state.units[0]
        _hn_support_skill(state, hn)
        d0 = hn.total_damage_dealt
        assert d0 > 0
        hn.base_stats.ATK *= 1.5  # 提高面板
        state2 = _hn_state()
        hn2 = state2.units[0]
        hn2.base_stats.ATK *= 1.5
        _hn_support_skill(state2, hn2)
        assert hn2.total_damage_dealt > d0 * 1.3  # 同比提升(防御曲线下近似)

    def test_support_regen_and_trace2(self):
        """非姬子使用者回4能量; 开拓同行(开拓者·记忆)→行迹2额外回合
        v7.2.0 #7: 行迹2按次触发——防循环=额外回合内不重触(pending标记),
        额外回合执行后清除, 可再次触发"""
        state = _hn_state('trailblazer_remembrance')
        ally = state.units[1]
        e0 = ally.current_energy
        _hn_support_skill(state, ally)
        assert ally.current_energy - e0 == pytest.approx(4.0)
        kinds = [k for _, k in state.extra.get('extra_turns', [])]
        assert 'ult' in kinds
        assert ally.extra.get('hn_trace2_pending') is True
        # 防循环: 额外回合尚未执行(pending在身)→不重复入队
        state.extra['hn_support_uses'] = 5
        _hn_support_skill(state, ally)
        kinds2 = [k for _, k in state.extra.get('extra_turns', [])]
        assert kinds2.count('ult') == 1
        # 额外回合执行→pending清除→再次使用助战技可再触发
        # (_exec_extra_turn 开头会 pop 该标记, 此处直接模拟清除)
        state.extra['extra_turns'] = []
        ally.extra.pop('hn_trace2_pending', None)
        _hn_support_skill(state, ally)
        kinds3 = [k for _, k in state.extra.get('extra_turns', [])]
        assert kinds3.count('ult') == 1

    def test_verdict_protocol(self):
        """裁决协议（开拓者·记忆）: 激活+姬子伤害+100%; 队友终结技计数→免费助战技"""
        state = _hn_state('trailblazer_remembrance')
        hn = state.units[0]
        ally = state.units[1]
        from engine.characters.himeko_nova import _trace_hn_protocol
        _trace_hn_protocol(hn, state)
        assert state.extra.get('hn_verdict') is True
        # 姬子伤害×2: DMG_BONUS_ALL 100 生效
        s = _build_effective_stats(hn, state)
        assert s.DMG_BONUS_ALL >= 1.0
        # 队友2次终结技→免费助战技（hn_protocol_uses 递减）
        ally.current_energy = ally.char.max_energy
        uses0 = state.extra['hn_protocol_uses']
        _use_skill(ally, state, 'ultimate')
        _use_skill(ally, state, 'ultimate')
        assert state.extra['hn_protocol_uses'] == uses0 - 1
        assert '无消耗助战技' in '\n'.join(state.log)

    def test_charge_protocol(self):
        """歼破协议（长夜月）: 全队暴伤+100%; 每击中+1充能, 9点→免费助战技"""
        state = _hn_state('changyeyue')
        hn = state.units[0]
        from engine.characters.himeko_nova import _trace_hn_protocol
        _trace_hn_protocol(hn, state)
        assert state.extra.get('hn_charge') == 0
        assert any(getattr(b, 'attributes', {}).get('CRIT_DMG') == 100.0 for b in hn.buffs)
        ally = state.units[1]
        uses0 = state.extra['hn_protocol_uses']
        for _ in range(9):
            _hn_count_hits(state, ally)
        assert state.extra['hn_protocol_uses'] == uses0 - 1
        assert state.extra.get('hn_charge', 0) == 0  # 本次不获充能

    def test_ultimate_beams_and_pulse(self):
        """终结技: 光束6×16%每段+1源能(上限3) → 脉冲(10%+每源能15%弹射) → 最后一击3×80%"""
        u = _unit('himeko_nova')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        u.current_energy = u.char.max_energy
        _use_skill(u, state, 'ultimate')
        assert u.total_damage_dealt > 0
        assert u.extra.get('hn_source_energy', 0) == 0  # 源能已消耗
        assert any('终结技' in l for l in state.log)
        # 行迹3: 终结技+3源能（若终结技后未消耗? 直接调用验证）
        u2 = _unit('himeko_nova')
        state2 = SimState(enemies=[_enemy()], units=[u2])
        state2.extra['hn_support_uses'] = 1
        state2.extra['hn_protocol_uses'] = 2
        _hn_ultimate(state2, u2)
        assert u2.total_damage_dealt > 0
        assert state2.extra['hn_protocol_uses'] == 2  # 终结技后刷新

    def test_technique(self):
        """秘技: 秘技点上限+3 + 首波施放战技 + hn_tech_active"""
        state = _hn_state()
        hn = state.units[0]
        _tech_himeko_nova(state, hn, is_opener=True)
        assert state.max_sp == 8  # 默认5+3
        assert state.extra.get('hn_tech_active') is True
        assert any('拓星巡航' in l for l in state.log)
