"""v7.1.0 知更鸟·晴歌修正回归测试

覆盖:
1. 忆灵合一(项目主澄清①): 贝茜/啾米/派丁=状态档位, 全场单实体;
   易伤档位按成员状态取值; 每轮Fever一次登台; 升档不触发召唤类事件
2. P1 气氛触发补全: FUA/助战技/内联终结技等绕过 _use_skill 通用循环的攻击路径
3. P2 E1 真伤目标: 取AoE结算后存活敌; 全灭时不触发且记录不减半
4. P3 特邀嘉宾封锁(项目主澄清②): 持有者不拉别人(防永动机), 自拉条放行(翔鹰4pc)
5. 翔鹰2件套误带4件套条件数据修复
"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import RelicPiece, RelicSet
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import simulate, _get_relic_conditions, _lc_team_advance, _use_skill
from engine.characters.robin_summeretto import _qingge_gain_atmo
from engine.runtime import SimState, SimUnit, TimedBuff
from engine.systems.remembrance import RemembranceSystem
from engine.characters.robin_summeretto import _qingge_summon_variant


def _enemy(hp=500000):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80, element_res={'风': 0})


def _unit(cid, pos=1):
    char = load_character(cid, 'data/characters')
    stats = compute_combat_stats(char, None, None, None)
    u = SimUnit(char=char, base_stats=stats, position=pos)
    u.max_hp = u.current_hp = stats.HP
    return u


def _state(units, enemies=None):
    st = SimState(units=units, enemies=enemies or [_enemy()])
    st.extra['navs'] = {i: 100.0 + 100 * i for i in range(len(units))}
    rem = RemembranceSystem()
    st.extra['_rem_sys'] = rem
    # M4批2b: 旁路攻击单测需自注册晴歌监听（on_attack_action 事件契约）
    if any(getattr(u.char, 'id', '') == 'robin_summeretto' for u in units):
        from engine.characters import robin_summeretto
        robin_summeretto.INIT(st)
    return st


def _log(s):
    return '\n'.join(s.log)


# ══════════ 1. 忆灵合一（项目主澄清①）══════════

class TestSingleEntityMemsprite:
    def test_tier_upgrade_single_entity(self):
        """气氛6/12→档位2/3, 全场始终只有一个忆灵实体; 升档不触发召唤事件(+20能量仅首次)"""
        u = _unit('robin_summeretto')
        state = _state([u])
        rem = state.extra['_rem_sys']
        ms = _qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        assert ms.extra.get('qingge_members') == 1
        assert u.current_energy == pytest.approx(20.0)  # 贴近海的心跳仅首次召唤
        _qingge_gain_atmo(state, 6.0)  # 6 → 档位2
        assert ms.extra.get('qingge_members') == 2
        assert u.current_energy == pytest.approx(20.0)  # 升档不回能
        _qingge_gain_atmo(state, 6.0)  # 12 → 档位3 → 全员登台
        assert ms.extra.get('qingge_members') == 3
        assert u.extra.get('qingge_fever')
        assert len([m for m in state.memsprites
                    if m.summoner_id == 'robin_summeretto']) == 1

    def test_vuln_tiers_follow_member_state(self):
        """易伤档位按成员档位取值: 1→8% / 2→12% / 3→16%（非实体计数）"""
        u = _unit('robin_summeretto')
        state = _state([u])
        rem = state.extra['_rem_sys']
        base_vuln = u.base_stats.VULNERABILITY_APPLIED
        _qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.08)
        _qingge_gain_atmo(state, 6.0)
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.12)
        _qingge_gain_atmo(state, 6.0)
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.16)

    def test_one_stage_entry_per_fever(self):
        """每轮Fever仅一条「晴空乐手」登台日志（单实体上行动条）"""
        team = [{'char': load_character('robin_summeretto', 'data/characters'),
                 'position': 1},
                {'char': load_character('fengjin', 'data/characters'), 'position': 2}]
        s = simulate(team, _enemy(), max_av=600)
        log = _log(s)
        entries = log.count('全员登台! 进入【Fever】')
        assert entries >= 1
        assert log.count('「晴空乐手」登台行动') == entries
        assert len([m for m in s.memsprites
                    if m.summoner_id == 'robin_summeretto']) <= 1


# ══════════ 2. P1 气氛触发补全 ══════════

class TestAtmoFromBypassAttackPaths:
    def test_feixiao_fua_grants_atmo(self):
        """飞霄天赋FUA(直接结算路径)→晴歌气氛+1"""
        qg = _unit('robin_summeretto')
        fx = _unit('feixiao', pos=2)
        state = _state([qg, fx])
        from engine.characters.feixiao import _feixiao_fua
        _feixiao_fua(state, fx, state.enemies[0])
        assert qg.extra.get('qingge_atmo', 0.0) == pytest.approx(1.0)

    def test_hn_support_skill_grants_atmo(self):
        """姬子·启行助战技(不调_use_skill防递归)→晴歌气氛+1"""
        qg = _unit('robin_summeretto')
        hn = _unit('himeko_nova', pos=2)
        state = _state([qg, hn])
        from engine.characters.himeko_nova import _hn_support_skill
        _hn_support_skill(state, hn)
        assert qg.extra.get('qingge_atmo', 0.0) == pytest.approx(1.0)

    def test_hn_ultimate_grants_atmo(self):
        """姬子·启行内联终结技(提前return分支)→晴歌气氛+1"""
        qg = _unit('robin_summeretto')
        hn = _unit('himeko_nova', pos=2)
        state = _state([qg, hn])
        from engine.characters.himeko_nova import _hn_ultimate
        _hn_ultimate(state, hn)
        assert qg.extra.get('qingge_atmo', 0.0) == pytest.approx(1.0)

    def test_guest_holder_fua_grants_extra_atmo(self):
        """特邀嘉宾持有者的FUA→+1(攻击)+2(特邀嘉宾)=+3"""
        qg = _unit('robin_summeretto')
        fx = _unit('feixiao', pos=2)
        state = _state([qg, fx])
        fx.buffs.append(TimedBuff(source_id='robin_summeretto', attributes={},
                                  remaining_turns=2, param_id='qingge_guest',
                                  source_name='特邀嘉宾'))
        from engine.characters.feixiao import _feixiao_fua
        _feixiao_fua(state, fx, state.enemies[0])
        assert qg.extra.get('qingge_atmo', 0.0) == pytest.approx(3.0)


# ══════════ 3. P2 E1 真伤目标 ══════════

class TestE1TrueDamageTarget:
    def _setup(self, enemy_hp):
        u = _unit('robin_summeretto')
        u.eidolon_rank = 1
        state = _state([u], enemies=[_enemy(hp=enemy_hp)])
        rem = state.extra['_rem_sys']
        ms = _qingge_summon_variant(state, u, u.char.memsprite, '贝茜')
        u.extra['qingge_record'] = 1000.0
        return state, rem, u, ms

    def test_e1_hits_alive_target_and_halves_record(self):
        """AoE后仍有存活敌→对HP最高敌真伤, 记录减半"""
        state, rem, u, ms = self._setup(enemy_hp=500000)
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert '晴歌E1: 真伤' in _log(state)
        assert u.extra['qingge_record'] == pytest.approx(500.0)

    def test_e1_skips_when_aoe_kills_all(self):
        """AoE团灭→无存活目标: 不触发真伤, 记录不减半(v7.1.0 P2)"""
        state, rem, u, ms = self._setup(enemy_hp=100)
        rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
        assert state.enemies[0].HP <= 0
        assert '晴歌E1: 真伤' not in _log(state)
        assert u.extra['qingge_record'] == pytest.approx(1000.0)


# ══════════ 4. P3 特邀嘉宾封锁（项目主澄清②）══════════

class TestGuestAdvanceBlock:
    def test_lc_team_advance_blocked_for_others(self):
        """持有者开大带舞舞舞全队拉条: 他人被封锁, 自己照常"""
        qg = _unit('robin_summeretto')
        fj = _unit('fengjin', pos=2)
        state = _state([qg, fj])
        fj.buffs.append(TimedBuff(source_id='robin_summeretto', attributes={},
                                  remaining_turns=2, param_id='qingge_guest',
                                  source_name='特邀嘉宾'))
        navs = state.extra['navs']
        qg_before, fj_before = navs[0], navs[1]
        _lc_team_advance(state, 0.24, actor=fj)
        assert navs[0] == qg_before            # 他人封锁
        assert navs[1] < fj_before             # 自拉条放行
        assert '无法使其他友方获得行动提前' in _log(state)

    def test_self_advance_allowed_with_guest(self):
        """持有特邀嘉宾时自拉条放行: 翔鹰4pc终结技后自拉照常挂起"""
        qg = _unit('robin_summeretto')
        seele = _unit('seele', pos=2)
        state = _state([qg, seele])
        seele.buffs.append(TimedBuff(source_id='robin_summeretto', attributes={},
                                     remaining_turns=2, param_id='qingge_guest',
                                     source_name='特邀嘉宾'))
        seele._active_relic_conditions = {'ult_action_advance_25'}
        seele.current_energy = seele.char.max_energy
        _use_skill(seele, state, 'ultimate')
        assert seele._pending_action_advance > 0
        assert '翔鹰拉条' in _log(state)


# ══════════ 5. 翔鹰2件套数据修复 ══════════

class TestEagleRelicData:
    def test_2pc_has_no_ult_advance_condition(self):
        """2件套只应有风伤+10%, 不再误带4件套的终结技自拉条条件"""
        rs = RelicSet.from_json('data/relics/110_晨昏交界的翔鹰.json')
        pieces = [RelicPiece(slot=s, set_name=rs.name) for s in
                  ('head', 'hands', 'body', 'feet', 'link_rope', 'planar_sphere')]
        conds_2pc = _get_relic_conditions(pieces[:2], {rs.name: rs})
        assert 'ult_action_advance_25' not in conds_2pc
        conds_4pc = _get_relic_conditions(pieces[:4], {rs.name: rs})
        assert 'ult_action_advance_25' in conds_4pc
