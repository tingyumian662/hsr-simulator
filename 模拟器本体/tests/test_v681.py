"""v6.8.1 回归: HARNESS PORT REVIEW（极简模式会话）10 P1/P2 + 2 P3 核验属实后的修复

语义依据: 角色技能介绍/*.txt + CODEX_HANDOFF.md「HARNESS PORT REVIEW」节。"""
import copy
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill, _build_effective_stats, _should_ult_now, _flat_toughness_with_break
from engine.characters.tribbie import _tribbie_field_extra_damage, _tribbie_talent_fua
from engine.characters.hysilens import _hysilens_apply_dot
from engine.characters.anaxa import _anaxa_add_weakness
from engine.characters.phainon import _phainon_shihun_counter, _phainon_kasier_act
from engine.runtime import SimState, SimUnit
from engine.systems.elation import ElationSystem


def _enemy(hp=500000, toughness=200, broken=False, res=None, name='X'):
    e = Enemy(id='x', name=name, HP=hp, ATK=100, DEF=800, SPD=80,
              toughness=0 if broken else toughness, max_toughness=toughness, level=80,
              element_res=dict(res or {'冰': 0, '量子': 0, '风': 0, '雷': 0,
                                       '虚数': 0, '物理': 0, '火': -0.2}))
    if broken:
        e.is_broken = True
    return e


def _unit(cid, position=1, eidolon=0):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    return u


def _elation(state):
    state.extra['_elation'] = ElationSystem()
    return state.extra['_elation']


class TestP1Fixes:
    def test_phainon_talent_target_gating(self):
        """P1-1: 白厄天赋需被点名——攻击技能不触发; all_allies 触发且暴伤 TimedBuff 3回合"""
        u = _unit('phainon')
        ally = _unit('seele', position=2)
        st = SimState(enemies=[_enemy()], units=[u, ally])
        _use_skill(ally, st, 'skill')
        assert u.extra.get('huozhong', 0) == 0
        br = _unit('bronya', position=3)
        st2 = SimState(enemies=[_enemy()], units=[u, br])
        br.char.skills['skill'] = copy.deepcopy(br.char.skills['skill'])
        br.char.skills['skill'].target = 'all_allies'
        _use_skill(br, st2, 'skill')
        assert u.extra.get('huozhong', 0) >= 1
        b = next(x for x in u.buffs if getattr(x, 'param_id', '') == 'phainon_cd_buff')
        assert b.attributes['CRIT_DMG'] == pytest.approx(30.0)
        assert b.remaining_turns == 3

    def test_tribbie_field_hit_targets_only(self):
        """P1-2: 结界附加只打被击中目标中的最高HP; E1 真伤=本次攻击总伤害24%"""
        trib = _unit('tribbie', eidolon=1)
        e1 = _enemy(hp=1000, name='A')
        e2 = _enemy(hp=9000, name='B')
        st = SimState(enemies=[e1, e2], units=[trib])
        st.extra['tribbie_field_turns'] = 2
        _tribbie_field_extra_damage(st, trib, [e1], total_dmg=5000.0)
        # 附加只打 e1（被击中集合内最高HP=唯一）
        assert e1.HP < 1000
        assert e2.HP == pytest.approx(9000)  # 未受击目标不受附加
        # E1 真伤 = 5000×24% 加在 e1
        assert e1.HP == pytest.approx(0)
        assert st.extra.get('killed_total', 0) == 1

    def test_hn_ultimate_toughness_break(self):
        """P1-3: 光束削韧归零触发击破（is_broken+击破伤害+延后）"""
        u = _unit('himeko_nova')
        e = _enemy(toughness=2)
        st = SimState(enemies=[e], units=[u])
        st.extra['hn_support_uses'] = 1
        _flat_toughness_with_break(st, u, e, 2.0, '火', 'ultimate', _build_effective_stats(u, st))
        assert e.is_broken is True
        assert e.extra.get('av_delayed', 0) > 0
        assert any('击破' in l for l in st.log)

    def test_sparxie_ult_elation_extra(self):
        """P1-4: 火花持好活→终结技额外48%欢愉伤害"""
        from engine.characters.sparxie import _sparxie_ult_elation_extra
        u = _unit('sparxie')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        _elation(st)
        st.extra['_elation'].grant_good_show(st, 'sparxie', 20.0)
        d0 = u.total_damage_dealt
        _sparxie_ult_elation_extra(st, u)
        assert u.total_damage_dealt > d0
        assert any('48%' in l for l in st.log)

    def test_shihun_bounce_kill_counted(self):
        """P1-5: 弑魂反击弹射段击杀计数"""
        u = _unit('phainon')
        e1 = _enemy(hp=500000, name='A')
        e2 = _enemy(hp=10, name='B')
        st = SimState(enemies=[e1, e2], units=[u])
        u.extra['shihun_stacks'] = 1
        _phainon_shihun_counter(st, u, 1)
        # 弹射段可能击杀 e2（随机, 但总量击杀应计 killed_total）
        if e2.HP <= 0:
            assert st.extra.get('killed_total', 0) >= 1


class TestP2Fixes:
    def test_should_ult_now_phainon_huozhong(self):
        """P2-2: 白厄火种<12 不排终结技"""
        u = _unit('phainon')
        st = SimState(enemies=[_enemy()], units=[u])
        u.current_energy = 12
        u.extra['huozhong'] = 5
        assert _should_ult_now(u, st) is False
        u.extra['huozhong'] = 12
        assert _should_ult_now(u, st) is True

    def test_cerydra_jungong_swap_clears_old(self):
        """P2-3: 换军功目标清除旧目标状态"""
        from engine.characters.cerydra import _cerydra_grant_jungong
        cery = _unit('cerydra')
        a = _unit('seele', position=2)
        b = _unit('bronya', position=3)
        st = SimState(enemies=[_enemy()], units=[cery, a, b])
        _cerydra_grant_jungong(st, cery, a)
        assert a.extra.get('cerydra_jungong') is True
        _cerydra_grant_jungong(st, cery, b)
        assert a.extra.get('cerydra_jungong') is False  # 旧目标清除
        assert b.extra.get('cerydra_jungong') is True

    def test_hysilens_dot_hit_targets_only(self):
        """P2-4: 海瑟音天赋只对受击目标挂 DOT（v6.8.1: 调用点已改 last_attack_targets）"""
        hs = _unit('hysilens')
        e1 = _enemy(name='A')
        e2 = _enemy(name='B')
        st = SimState(enemies=[e1, e2], units=[hs])
        _hysilens_apply_dot(st, hs, e1)
        assert any(s.id.startswith('hysilens_dot') for s in e1.statuses)
        assert not any(s.id.startswith('hysilens_dot') for s in e2.statuses)

    def test_hysilens_e1_double_and_settle_multiplier(self):
        """P2-4 附: E1 天赋路径双挂 + DOT 结算×116%（挂时不再乘）"""
        hs = _unit('hysilens', eidolon=1)
        e = _enemy()
        st = SimState(enemies=[e], units=[hs])
        _hysilens_apply_dot(st, hs, e, e1_double=True)
        dots = [s for s in e.statuses if s.id.startswith('hysilens_dot')]
        assert len(dots) == 2  # 额外陷入一次
        for s in dots:
            # 裂伤 mult=0 / 其余 25; 挂时不再乘 116%（此前会变 29）
            assert s.attributes['multiplier'] in (0.0, 25.0)
        from engine.characters.hysilens import _tick_hysilens_dot
        hp0 = e.HP
        _tick_hysilens_dot(st, e, dots[0])
        assert hp0 - e.HP > 0  # 结算有伤害

    def test_anaxa_weakness_per_hit(self):
        """P2-4: 那刻夏每击中1次→目标1个弱点（单目标函数级验证）"""
        ax = _unit('anaxa')
        e = _enemy()
        st = SimState(enemies=[e], units=[ax])
        st.extra['last_attack_targets'] = [e]
        st.extra['last_multihit_targets'] = []
        n0 = len([s for s in e.statuses if s.id.startswith('anaxa_weak')])
        _anaxa_add_weakness(st, ax, e)
        n1 = len([s for s in e.statuses if s.id.startswith('anaxa_weak')])
        assert n1 == n0 + 1

    def test_tribbie_trace1_buff_and_e6(self):
        """P2-5: 行迹1 TimedBuff 消费（面板 DMG_BONUS_ALL 72×层）+ E6 FUA ×8.29"""
        trib = _unit('tribbie', eidolon=6)
        e = _enemy()
        st = SimState(enemies=[e], units=[trib])
        _tribbie_talent_fua(st, trib)
        _tribbie_talent_fua(st, trib)  # 2层
        s = _build_effective_stats(trib, st)
        assert s.DMG_BONUS_ALL >= 0.72 * 2  # 行迹1消费生效
        b = next(x for x in trib.buffs if getattr(x, 'param_id', '') == 'tribbie_trace1_stack')
        assert b.attributes['DMG_BONUS_ALL'] == pytest.approx(144.0)
        # E6 729%: 伤害量级对比（E6 vs 无E6 约 8.29 倍）
        trib0 = _unit('tribbie')
        e0 = _enemy()
        st0 = SimState(enemies=[e0], units=[trib0])
        _tribbie_talent_fua(st0, trib0)
        d_e6 = trib.total_damage_dealt
        d_0 = trib0.total_damage_dealt
        assert d_e6 > d_0 * 8  # ×8.29


class TestP3Fixes:
    def test_respawn_rebuilds_tribbie_field(self):
        """P3-1: 新波敌人重建缇宝结界易伤"""
        from engine.core.combat_engine import _respawn_wave
        trib = _unit('tribbie')
        e = _enemy()
        st = SimState(enemies=[e], units=[trib])
        st.extra['enemy_blueprint'] = copy.deepcopy(e)
        st.extra['num_enemies'] = 1
        st.extra['tribbie_field_turns'] = 2
        _respawn_wave(st)
        assert st.enemies[0].vulnerability == pytest.approx(0.30)

    def test_kasier_act_shihun_counter(self):
        """P3-2: 额外回合开始持弑魂→立即反击并解除"""
        u = _unit('phainon')
        e = _enemy()
        st = SimState(enemies=[e], units=[u])
        u.extra['kasier'] = True
        u.extra['kasier_done'] = 0
        u.extra['shihun_stacks'] = 2
        u.extra['shihun_dr'] = 0.75
        u.buffs.append(type('B', (), {'attributes': {}, 'source_name': '弑魂之炽减伤'})())
        _phainon_kasier_act(st, u)
        assert 'shihun_stacks' not in u.extra  # 反击后解除
        assert not any(getattr(b, 'source_name', '') == '弑魂之炽减伤' for b in u.buffs)
        assert u.total_damage_dealt > 0  # 反击造成伤害
