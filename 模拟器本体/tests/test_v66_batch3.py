"""v6.6 批3 回归: 白厄（变身状态机）+ 遐蝶校验

语义依据: 角色技能介绍/毁灭/白厄.txt、记忆/遐蝶.txt + CLAUDE_HANDOFF v6.6 节"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.attributes import compute_combat_stats
from engine.core.combat_engine import _use_skill
from engine.characters.phainon import _phainon_gain_huozhong, _phainon_transform
from engine.runtime import SimState, SimUnit
from engine.systems.remembrance import RemembranceSystem
from engine.characters.xilian import _xilian_support_skill


def _enemy(hp=500000, toughness=200):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _unit(cid, position=1, eidolon=0, **extra):
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=position)
    u.max_hp = u.current_hp = stats.HP
    u.eidolon_rank = eidolon
    u.extra.update(extra)
    return u


class TestPhainon:
    def test_skill_gains_huozhong(self):
        """战技: 火种+2"""
        u = _unit('phainon')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        assert u.extra.get('huozhong', 0) == 2

    def test_trace1_start_huozhong(self):
        """行迹1: 开局+1火种"""
        from engine.characters.phainon import _trace_phainon_trace1
        u = _unit('phainon')
        state = SimState(enemies=[_enemy()], units=[u])
        _trace_phainon_trace1(u, state)
        assert u.extra.get('huozhong', 0) >= 1

    def test_transform_with_12_huozhong(self):
        """终结技: 火种12→变身（8额外回合+敌物理弱点）"""
        u = _unit('phainon')
        e = _enemy()
        e.element_res['物理'] = 0.2
        state = SimState(enemies=[e], units=[u])
        _phainon_gain_huozhong(state, u, 12)
        _use_skill(u, state, 'ultimate')
        assert u.extra.get('kasier') is True
        assert u.extra.get('kasier_turns') == 8
        assert e.element_res['物理'] == pytest.approx(-0.2)

    def test_ult_insufficient_huozhong(self):
        """火种不足12: 不变身"""
        u = _unit('phainon')
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'ultimate')
        assert not u.extra.get('kasier')

    def test_talent_targeted_gains_huozhong(self):
        """天赋: 白厄成为技能目标→+1火种+暴伤30%(3回合); 攻击技能(目标为敌)不触发
        v6.8.1: 补目标判定——此前任意队友普攻/战技都触发且暴伤永久叠加"""
        from copy import deepcopy
        u = _unit('phainon')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        _use_skill(ally, state, 'skill')  # 希儿战技=攻击技能, 目标为敌 → 不触发
        assert u.extra.get('huozhong', 0) == 0
        assert not any(getattr(b, 'param_id', '') == 'phainon_cd_buff' for b in u.buffs)
        # 友方群辅（all_allies 命中白厄）→ 触发
        br = _unit('bronya', position=3)
        state2 = SimState(enemies=[_enemy()], units=[u, br])
        br.char.skills['skill'] = deepcopy(br.char.skills['skill'])
        br.char.skills['skill'].target = 'all_allies'
        _use_skill(br, state2, 'skill')
        assert u.extra.get('huozhong', 0) >= 1
        assert any(getattr(b, 'param_id', '') == 'phainon_cd_buff'
                   and getattr(b, 'attributes', {}).get('CRIT_DMG') == 30.0 for b in u.buffs)


class TestPoemFushi:
    def test_fushi_poem_grants_huozhong_and_buffs(self):
        """献予「负世」: 火种+6+毁伤+4+暴伤72%/CR16%"""
        u = _unit('xilian')
        ph = _unit('phainon', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ph])
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        rem.summon_memsprite(state, u, u.char.memsprite)
        _xilian_support_skill(state, u, u.memsprite_unit)
        assert ph.extra.get('poem_fushi') is True
        assert ph.extra.get('huozhong', 0) >= 6
        assert ph.extra.get('huishang', 0) >= 4
        assert ph.base_stats.CRIT_DMG >= 0.72
        assert ph.base_stats.CRIT_RATE >= 0.16


class TestXiadieVerify:
    def test_json_matches_txt(self):
        """遐蝶 JSON 对照 txt: 战技50/30(满级) + 30%群耗; 死龙双段30/50 + 40%群耗"""
        d = load_character('xiadie', 'data/characters')
        sd = d.skills
        skill = sd['skill']
        assert [m.scale for m in skill.multipliers] == [50.0, 30.0]
        assert skill.cost.get('hp_percent_allies') == 30.0
        dragon = sd['skill_dragon']
        assert [m.scale for m in dragon.multipliers] == [30.0, 50.0]
        assert dragon.cost.get('hp_percent_allies') == 40.0


class TestPhainonPrecise:
    """v6.6 精准化（用户 2026-08-14 纠正语义）"""

    def test_kasier_interval_evenly_divided(self):
        """8 额外回合均分: 间隔 = AV_PER_TURN/(基础速度×0.6)/8"""
        u = _unit('phainon')
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.characters.phainon import _phainon_transform
        base = u.base_stats._base_SPD
        _phainon_transform(state, u)
        interval = 10000.0 / (base * 0.60) / 8.0
        assert u.extra['kasier_interval'] == pytest.approx(interval, rel=1e-9)
        assert u.extra['kasier_turns'] == 8
        assert u.extra['kasier_next_av'] == state.current_av  # 第1回合立即

    def test_teammates_leave_bar_not_dead(self):
        """队友离场=从进度条离开(非无法战斗): navs 移除, 忆灵保留, 退出后恢复"""
        u = _unit('phainon')
        ally = _unit('seele', position=2)
        state = SimState(enemies=[_enemy()], units=[u, ally])
        state.extra['navs'] = {0: 100.0, 1: 200.0}
        from engine.characters.phainon import _phainon_transform, _phainon_kasier_end
        _phainon_transform(state, u)
        assert 0 not in state.extra['navs']  # 白厄自身脱离
        assert 1 not in state.extra['navs']  # 队友离场
        assert ally.is_alive is True  # 非无法战斗
        _phainon_kasier_end(state, u)
        assert state.extra['navs'].get(0) == 100.0
        assert state.extra['navs'].get(1) == 200.0

    def test_huozhong_overflow_refund_immediate(self):
        """火种返还无延迟: 变身结束溢出+行迹1的3直接计入"""
        u = _unit('phainon')
        state = SimState(enemies=[_enemy()], units=[u])
        from engine.characters.phainon import _phainon_gain_huozhong, _phainon_transform, _phainon_kasier_end
        _phainon_gain_huozhong(state, u, 14)  # 12消耗+2溢出
        _phainon_transform(state, u)
        assert u.extra.get('huozhong_overflow') == 2
        _phainon_kasier_end(state, u)
        assert u.extra.get('huozhong') == 5  # 溢出2 + 行迹1的3

    def test_shihun_counter_after_enemy_action(self):
        """弑魂时序: 施放挂层+敌立即行动 → 敌攻击后叠层 → 敌行动完毕反击"""
        u = _unit('phainon')
        e = _enemy()
        state = SimState(enemies=[e], units=[u])
        from engine.core.combat_engine import _enemy_exec_action, _enemy_turn_end
        from engine.characters.phainon import _phainon_gain_huishang
        _phainon_gain_huishang(state, u, 2)
        u.extra['kasier'] = True
        _use_skill(u, state, 'skill_enhanced')
        assert u.extra.get('shihun_stacks') == 1
        hp0 = e.HP
        _enemy_exec_action(state, e)  # 敌攻击→叠层
        assert u.extra.get('shihun_stacks') == 2
        _enemy_turn_end(state, e)  # 敌行动完毕→反击+清除
        assert u.extra.get('shihun_stacks', 0) == 0
        assert e.HP < hp0
        assert any('弑魂反击' in l for l in state.log)
