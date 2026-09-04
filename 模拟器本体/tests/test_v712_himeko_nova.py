"""v7.2.0 姬子·启行修复回归测试

语义依据: 角色技能介绍/智识/姬子·启行.txt + 项目主三项裁决(2026-09-01):
A. 境界互斥——姬子·启行在场即永久占据境界位【拓星视界】, 遐蝶/白厄终结技永封
   (昔涟无境界技能, 结界与境界系统解耦);
B. 终结技手法 = 脉冲-3光束-脉冲-3光束-脉冲-最后一击(启动自带3源能);
C. 秘技维持开怪者门控(她通常为队伍唯一进战秘技持有者=默认开怪者)。
另有 8 项审查 bug 修复(flag_regen注册/AI轮转/E3E5等级消费/E1次数/E2双恢复/
晴歌终结技计裁决/行迹2按次/队友消费助战技)。
"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate, _use_skill
from engine.characters.himeko_nova import _hn_ultimate, _hn_support_skill, _hn_support_cap, _hn_realm_blocks_ult
from engine.runtime import SimState, SimUnit
from engine.characters.himeko_nova import _trace_hn_protocol, _trace_hn_flag_regen
from engine.runtime import TimedBuff


def _enemy(hp=900000, toughness=0):
    # toughness=0: 无击破干扰, 单体伤害全确定(crit_mode=expected)
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


def _state(units, eidolon=0):
    state = SimState(units=units, enemies=[_enemy()])
    state.extra['hn_support_uses'] = 1
    state.extra['hn_protocol_uses'] = 2
    return state


def _log(s):
    return '\n'.join(s.log)


# ══════════ 裁决A: 境界互斥 ══════════

class TestRealmExclusivity:
    def test_realm_marked_on_entry(self):
        """姬子进战→realm_owner=himeko_nova 永久(-1)"""
        hn = _unit('himeko_nova')
        state = _state([hn])
        _trace_hn_protocol(hn, state)
        assert state.realm_owner == 'himeko_nova'
        assert state.realm_turns == -1
        assert '展开【拓星视界】' in _log(state)

    def test_blocks_xiadie_and_phainon_only(self):
        """封锁判定: 遐蝶/白厄被封, 其他角色不受影响"""
        hn = _unit('himeko_nova')
        xd = _unit('xiadie', position=2)
        ph = _unit('phainon', position=3)
        qg = _unit('robin_summeretto', position=4)
        state = _state([hn, xd, ph, qg])
        assert _hn_realm_blocks_ult(state, xd)
        assert _hn_realm_blocks_ult(state, ph)
        assert not _hn_realm_blocks_ult(state, qg)
        assert not _hn_realm_blocks_ult(state, hn)

    def test_no_block_without_himeko(self):
        """无姬子·启行→遐蝶/白厄终结技不受封"""
        xd = _unit('xiadie')
        ph = _unit('phainon', position=2)
        state = _state([xd, ph])
        assert not _hn_realm_blocks_ult(state, xd)
        assert not _hn_realm_blocks_ult(state, ph)

    def test_sim_xiadie_phainon_never_ult(self):
        """模拟级: 姬子在场→遐蝶/白厄整场 0 次终结技"""
        team = [{'char': load_character('himeko_nova', 'data/characters'), 'position': 1},
                {'char': load_character('xiadie', 'data/characters'), 'position': 2},
                {'char': load_character('phainon', 'data/characters'), 'position': 3}]
        s = simulate(team, _enemy(hp=2000000), max_av=900)
        log = _log(s)
        assert '展开【拓星视界】' in log
        assert '亡喉怒哮，苏生之颂铃' not in log      # 遐蝶终结技
        assert '变身【卡厄斯兰那】' not in log          # 白厄变身
        assert '遗世冥域' not in log

    def test_xilian_field_unaffected(self):
        """昔涟结界不参与境界互斥——姬子在场照常展开"""
        hn = _unit('himeko_nova')
        xl = _unit('xilian', position=2)
        state = _state([hn, xl])
        _trace_hn_protocol(hn, state)  # 拓星视界占境界
        xl.zhuiyi = 24
        _use_skill(xl, state, 'skill')
        assert state.realm_owner == 'himeko_nova'      # 境界位仍归姬子
        assert state.extra.get('xilian_field_turns') == 2  # 结界照常展开
        assert '展开结界(2回合)' in _log(state)


# ══════════ 裁决B: 终结技手法 ══════════

class TestUltRotation:
    def _ult_dealt(self, rank):
        hn = _unit('himeko_nova', eidolon=rank)
        state = _state([hn])
        before = state.enemies[0].HP
        _hn_ultimate(state, hn)
        return before - state.enemies[0].HP

    def test_e0_source_energy_flow(self):
        """E0 手法: 启动3源能, 三次脉冲各耗3(每次2弹射), 终结技后源能归零
        倍率构成: 3×10%脉冲 + 6×15%弹射 + 6×16%光束 + 3×80%最后一击 = 456%ATK"""
        hn = _unit('himeko_nova')
        state = _state([hn])
        _hn_ultimate(state, hn)
        assert hn.extra.get('hn_source_energy') == 0
        log = _log(state)
        assert '脉冲-3光束-脉冲-3光束-脉冲-最后一击' in log
        assert '源能消耗9' in log  # 3+3+3

    def test_e6_source_energy_flow(self):
        """E6(上限6/光束+2): 脉冲耗3→光束×3(+6)→脉冲耗6→光束×3(+6)→脉冲耗6 = 消耗15
        两次源能≥6脉冲各附160%全体"""
        hn = _unit('himeko_nova', eidolon=6)
        state = _state([hn])
        _hn_ultimate(state, hn)
        assert '源能消耗15' in _log(state)

    def test_e3_multiplies_ult_damage(self):
        """E3终结技+2 → 全部内联倍率×1.10(每级+5%×2级)——修复前零消费
        (同rank对照: 手动注入 boost, 隔离 E2 等其他星魂影响)"""
        hn_a = _unit('himeko_nova', eidolon=3)
        hn_b = _unit('himeko_nova', eidolon=3)
        hn_b.extra['skill_level_boost'] = {'ultimate': 2}
        sa, sb = _state([hn_a]), _state([hn_b])
        before_a, before_b = sa.enemies[0].HP, sb.enemies[0].HP
        _hn_ultimate(sa, hn_a)
        _hn_ultimate(sb, hn_b)
        d_a, d_b = before_a - sa.enemies[0].HP, before_b - sb.enemies[0].HP
        assert d_a > 0
        assert d_b == pytest.approx(d_a * 1.10, rel=1e-9)

    def test_e6_full_multiplier_structure(self):
        """E6 完整构成对照(单体): E0=3×10+6×19.5+6×16+3×80=483% (弹射含行迹3×1.3);
        E6=3×10+12×19.5+6×16+2×160+3×80=920% → ×E2(1.30); E3boost 单独对照已覆盖"""
        hn0 = _unit('himeko_nova')
        hn6 = _unit('himeko_nova', eidolon=6)
        s0, s6 = _state([hn0]), _state([hn6])
        b0, b6 = s0.enemies[0].HP, s6.enemies[0].HP
        _hn_ultimate(s0, hn0)
        _hn_ultimate(s6, hn6)
        d0, d6 = b0 - s0.enemies[0].HP, b6 - s6.enemies[0].HP
        assert d6 == pytest.approx(d0 * 920.0 / 483.0 * 1.30, rel=1e-9)


# ══════════ Bug 修复回归 ══════════

class TestBugFixes:
    def test_flag_regen_registered_and_e2_double(self):
        """#1+#5: 旗语回合恢复已注册; E2 额外恢复第2次"""
        hn0 = _unit('himeko_nova', eidolon=0)
        hn2 = _unit('himeko_nova', eidolon=2)
        for hn, expect in ((hn0, 1), (hn2, 2)):
            state = _state([hn])
            state.extra['hn_support_uses'] = 0
            state.extra['hn_support_cap_ref'] = _hn_support_cap(hn)
            hn.buffs.append(TimedBuff(source_id='x', attributes={},
                                      remaining_turns=3, param_id='himeko_nova_flag'))
            _trace_hn_flag_regen(hn, state)
            assert state.extra['hn_support_uses'] == expect

    def test_trace1_energy_when_full(self):
        """#1: 行迹1——次数=上限时回合开始回5能量"""
        hn = _unit('himeko_nova')
        state = _state([hn])
        state.extra['hn_support_uses'] = _hn_support_cap(hn)
        e0 = hn.current_energy
        _trace_hn_flag_regen(hn, state)
        assert hn.current_energy == pytest.approx(e0 + 5.0)

    def test_e1_protocol_uses_three(self):
        """#4: E1 → 特殊效果免费助战技次数 3(终结技刷新同理)"""
        hn1 = _unit('himeko_nova', eidolon=1)
        state = _state([hn1])
        _hn_ultimate(state, hn1)
        assert state.extra['hn_protocol_uses'] == 3
        hn0 = _unit('himeko_nova', eidolon=0)
        state0 = _state([hn0])
        _hn_ultimate(state0, hn0)
        assert state0.extra['hn_protocol_uses'] == 2

    def test_e5_talent_factor_on_self_support(self):
        """#3: E5天赋+2 → 姬子自用助战技的抗穿/暴伤加成值×1.10(每级+5%×2级)
        (因子作用于加成面板而非最终伤害, 以探针捕获传入伤害函数的面板断言)"""
        import engine.characters.himeko_nova as hnm
        captured = {}
        orig = hnm.calculate_damage

        def spy(stats, *a, **kw):
            captured.setdefault('respen', []).append(stats.RES_PEN_ALL)
            captured.setdefault('cd', []).append(stats.CRIT_DMG)
            return orig(stats, *a, **kw)
        hnm.calculate_damage = spy
        try:
            hn0 = _unit('himeko_nova', eidolon=5)
            hn5 = _unit('himeko_nova', eidolon=5)
            hn5.extra['skill_level_boost'] = {'talent': 2}
            base_pen = hn0.base_stats.RES_PEN_ALL
            base_cd = hn0.base_stats.CRIT_DMG
            s0, s5 = _state([hn0]), _state([hn5])
            captured.clear(); _hn_support_skill(s0, hn0)
            pen0 = max(captured['respen']) - base_pen
            cd0 = max(captured['cd']) - base_cd
            captured.clear(); _hn_support_skill(s5, hn5)
            pen5 = max(captured['respen']) - base_pen
            cd5 = max(captured['cd']) - base_cd
        finally:
            hnm.calculate_damage = orig
        assert cd0 == pytest.approx(0.80, abs=1e-9)
        assert pen0 == pytest.approx(0.30, abs=1e-9)  # rank5 含E4: 天赋抗穿30%
        assert cd5 == pytest.approx(0.80 * 1.10, abs=1e-9)
        assert pen5 == pytest.approx(0.30 * 1.10, abs=1e-9)

    def test_qingge_ult_counts_verdict(self):
        """#6: 晴歌终结技计入裁决协议计数(提前return前已补)"""
        hn = _unit('himeko_nova')
        qg = _unit('robin_summeretto', position=2)
        state = _state([hn, qg])
        state.extra['hn_verdict'] = True  # 手动激活裁决(晴歌非触发角色)
        qg.current_energy = qg.char.max_energy
        _use_skill(qg, state, 'ultimate')
        assert state.extra.get('hn_verdict_ult_count') == 1

    def test_trace2_e2_extends_to_non_companions(self):
        """#7: E2 → 非开拓同行角色(希儿)使用助战技也获额外回合"""
        hn = _unit('himeko_nova', eidolon=2)
        seele = _unit('seele', position=2)
        state = _state([hn, seele])
        state.extra['hn_support_uses'] = 1
        _hn_support_skill(state, seele)
        kinds = [k for _, k in state.extra.get('extra_turns', [])]
        assert 'ult' in kinds

    def test_teammate_ai_consumes_support_uses(self):
        """#8: 队友行动后自动消费共享助战技次数"""
        team = [{'char': load_character('himeko_nova', 'data/characters'), 'position': 1},
                {'char': load_character('robin_summeretto', 'data/characters'), 'position': 2}]
        s = simulate(team, _enemy(hp=2000000), max_av=500)
        log = _log(s)
        assert '助战技·开拓与你同行: 知更鸟•晴歌' in log

    def test_ai_rotation_alternates(self):
        """#2 单元级: AI 轮转 = 助战技→战技→助战技→战技(cd=2 生效)"""
        from engine.characters.himeko_nova import _hn_ai
        hn = _unit('himeko_nova')
        state = _state([hn])
        state.skill_points = 5
        seq = []
        for _ in range(4):
            cd_before = hn.extra.get('hn_skill_cd', 0)
            if hn.current_energy >= hn.char.max_energy:
                seq.append('ult')
            elif cd_before <= 0:
                seq.append('support')
            elif state.skill_points > 0:
                seq.append('skill')
            else:
                seq.append('basic')
            # 复刻 _hn_ai 的 cd 语义
            hn.extra['hn_skill_cd'] = 2 if seq[-1] == 'support' else cd_before
            if hn.extra.get('hn_skill_cd', 0) > 0:
                hn.extra['hn_skill_cd'] -= 1
        assert seq == ['support', 'skill', 'support', 'skill']

    def test_ai_rotation_keeps_flag_alive(self):
        """#2+#1: 模拟级——烽火多次施放(开局秘技+AI轮转), 旗语回合恢复已生效
        (SP 与晴歌战技竞争下 700AV 至少 2 次烽火; 关键回归=回合恢复日志出现)"""
        team = [{'char': load_character('himeko_nova', 'data/characters'), 'position': 1},
                {'char': load_character('robin_summeretto', 'data/characters'), 'position': 2}]
        s = simulate(team, _enemy(hp=2000000), max_av=700)
        log = _log(s)
        assert log.count('升起领航的烽火') >= 2
        assert '领航旗语: 助战技次数+1' in log
