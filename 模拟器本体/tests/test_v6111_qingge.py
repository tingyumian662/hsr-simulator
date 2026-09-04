"""知更鸟·晴歌（robin_summeretto）基础机制回归测试 (v6.11.1)

数据源: 角色技能介绍/记忆/知更鸟·晴歌.txt（用户原稿 v2）
核心循环: 战技召唤晴空乐手(贝茜档) → 攻击/治疗/护盾攒气氛 → 6/12点升档(啾米/派丁登台)
→ 全员登台(3档)进Fever(晴歌离场, 晴空乐手入行动条+140速倒计时扣气氛) → 气氛归零散场
v7.1.0 项目主澄清: 贝茜/啾米/派丁仅为状态档位, 实机按一只忆灵计算。
"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.models.equipment import load_lightcone
from engine.core.combat_engine import simulate
from engine.characters.robin_summeretto import _qingge_summon_variant


def _enemy(res=None):
    return Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                 toughness=30, max_toughness=30, level=80,
                 element_res=res or {'风': 0})


def _qingge():
    return load_character('robin_summeretto', 'data/characters')


def _sim(eidolon=0, with_fengjin=False, max_av=1500, **cfg):
    team = [{'char': _qingge(), 'position': 1, 'eidolon': eidolon, **cfg}]
    if with_fengjin:
        team.append({'char': load_character('fengjin', 'data/characters'),
                     'position': 2})
    return simulate(team, _enemy(), max_av=max_av)


def _log(s):
    return '\n'.join(s.log)


class TestSkillSummon:
    def test_skill_summons_bessie_with_inherit(self):
        """战技召唤唯一「晴空乐手」(贝茜档): HP=晴歌70%, SPD快照=晴歌180%
        v7.1.0 合一: 实体名=晴空乐手, 成员档位1=贝茜"""
        s = _sim(max_av=300)
        qg = s.units[0]
        assert '召唤「晴空乐手」贝茜' in _log(s)
        ms = qg.memsprite_unit
        assert ms is not None and ms.data.name == '晴空乐手'
        assert 1 <= ms.extra.get('qingge_members', 0) <= 3  # 成员档位状态在 1~3 之间演化
        # 全场只存在一个忆灵实体
        assert len([m for m in s.memsprites
                    if m.summoner_id == 'robin_summeretto']) == 1
        assert ms.max_hp == pytest.approx(qg.max_hp * 0.70, rel=1e-9)
        assert ms.base_stats.SPD == pytest.approx(qg.base_stats.SPD * 1.80, rel=1e-9)
        # 行迹1: 晴空乐手CR+50%
        assert ms.base_stats.CRIT_RATE == pytest.approx(
            qg.base_stats.CRIT_RATE + 0.50, rel=1e-9)
        # Fever前不在行动条
        assert ms.runtime_spd == 0

    def test_skill_heals_when_present(self):
        """晴空乐手已在场→战技回血100%+气氛+6"""
        from engine.core.combat_engine import _effective_spd
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        from engine.systems.remembrance import RemembranceSystem
        char = _qingge()
        stats = compute_combat_stats(char, None, None, None)
        u = SimUnit(char=char, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 100.0}
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        ms = _qingge_summon_variant(state, u, char.memsprite, '贝茜')
        ms.current_hp = ms.max_hp * 0.5
        before = u.extra.get('qingge_atmo', 0.0)
        _qingge_summon_variant(state, u, char.memsprite, '贝茜')
        assert ms.current_hp == pytest.approx(ms.max_hp, rel=1e-9)
        assert u.extra.get('qingge_atmo') == pytest.approx(before + 6)


class TestAtmoAndFever:
    def test_spawn_chain_and_fever(self):
        """气氛≥6→啾米登台(档位2), ≥12→派丁登台(档位3), 全员登台→Fever
        v7.1.0 合一: 升档=状态切换, 不产生新忆灵实体"""
        s = _sim(max_av=400, with_fengjin=True)
        log = _log(s)
        assert '召唤「晴空乐手」贝茜' in log
        assert '啾米登台 (成员2/3)' in log
        assert '派丁登台 (成员3/3)' in log
        assert '全员登台! 进入【Fever】' in log
        assert log.count('派丁登台 (成员3/3)') == 1  # 防重复升档回归
        # 合一不变量: 全场晴歌忆灵实体只有一个
        assert len([m for m in s.memsprites
                    if m.summoner_id == 'robin_summeretto']) <= 1
        assert 'Fever倒计时入场' in log

    def test_countdown_drains_and_exit(self):
        """倒计时扣50%气氛(至少12), 归零→退出Fever+晴空乐手消失+晴歌行动提前50%
        v7.1.0 合一: 单实体, 每轮Fever至多一次「晴空乐手消失」(防despawn双触发)"""
        s = _sim(max_av=900, with_fengjin=True)
        log = _log(s)
        assert 'Fever倒计时' in log
        assert '气氛归零: 退出【Fever】' in log
        assert '乘上夏夜晚风: 晴歌行动提前50%' in log
        assert '晴空乐手消失' in log
        # 防despawn双触发回归: 每次召唤(每轮Fever循环)至多消失一次
        assert 1 <= log.count('晴空乐手消失') <= log.count('召唤「晴空乐手」贝茜')

    def test_fever_removes_qingge_from_timeline(self):
        """Fever期间晴歌离开行动条(navs摘除), 退出后恢复"""
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        from engine.systems.remembrance import RemembranceSystem
        char = _qingge()
        stats = compute_combat_stats(char, None, None, None)
        u = SimUnit(char=char, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 100.0}
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        for name in ('贝茜', '啾米', '派丁'):
            _qingge_summon_variant(state, u, char.memsprite, name)
        assert u.extra.get('qingge_fever')
        assert 0 not in state.extra['navs']  # Fever期晴歌离场
        from engine.characters.robin_summeretto import _qingge_exit_fever
        _qingge_exit_fever(state, u)
        assert not u.extra.get('qingge_fever')
        assert 0 in state.extra['navs']  # 退出后恢复行动条

    def test_field_def_pen_and_vuln(self):
        """结界: 全队无视防御15%+气氛×0.5%; 成员数易伤8%/12%/16%"""
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        from engine.systems.remembrance import RemembranceSystem
        char = _qingge()
        stats = compute_combat_stats(char, None, None, None)
        u = SimUnit(char=char, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 100.0}
        rem = RemembranceSystem()
        state.extra['_rem_sys'] = rem
        base_def_pen = u.base_stats.DEF_PEN
        base_vuln = u.base_stats.VULNERABILITY_APPLIED
        # 1只: 易伤8%
        _qingge_summon_variant(state, u, char.memsprite, '贝茜')
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.08)
        # 2只: 易伤12%
        _qingge_summon_variant(state, u, char.memsprite, '啾米')
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.12)
        # 3只→Fever: 易伤16% + 结界DEF_PEN
        _qingge_summon_variant(state, u, char.memsprite, '派丁')
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln + 0.16)
        atmo = u.extra.get('qingge_atmo', 0.0)
        assert u.base_stats.DEF_PEN == pytest.approx(base_def_pen + 0.15 + atmo * 0.005)
        # 退出Fever→数值回退
        from engine.characters.robin_summeretto import _qingge_exit_fever
        _qingge_exit_fever(state, u)
        assert u.base_stats.DEF_PEN == pytest.approx(base_def_pen)
        assert u.base_stats.VULNERABILITY_APPLIED == pytest.approx(base_vuln)


class TestMemspriteSkill:
    def test_quartet_damage_and_energy(self):
        """忆灵技: 全敌150%HP风伤 + 晴歌+20能量"""
        s = _sim(max_av=400, with_fengjin=True)
        log = _log(s)
        assert '忆灵技·叽叽啾啾四重奏' in log
        assert '忆灵技: 晴歌+20能量' in log

    def test_e1_true_damage(self):
        """E1: 忆灵技对HP最高敌真伤=记录×(11%+气氛×0.1%), 后记录减半"""
        s = _sim(eidolon=6, max_av=400, with_fengjin=True)
        assert '晴歌E1: 真伤' in _log(s)


class TestUltimate:
    def test_ult_advance_energy_guest(self):
        """终结技: 目标行动提前100% + 回20%能量上限 + 特邀嘉宾2回合"""
        s = _sim(max_av=400, with_fengjin=True)
        log = _log(s)
        assert '晴歌终结技' in log
        assert '行动提前100%' in log
        assert '【特邀嘉宾】' in log
        fj = s.units[1]
        # v7.0.0 A6: 删除恒真短路, 断言特邀嘉宾落位日志(施放时真实挂上buff);
        # 400AV窗口内2回合buff可能已过期, remaining_turns精确断言见 test_v700 TestA6GuestBuff
        assert '【特邀嘉宾】→' in log

    def test_ult_target_prefers_himeko(self):
        """目标规则: 姬子·启行在队→拉姬子"""
        from engine.models.character import load_character
        team = [
            {'char': _qingge(), 'position': 1, 'eidolon': 0, 'initial_energy_pct': 100},
            {'char': load_character('himeko_nova', 'data/characters'), 'position': 2},
        ]
        s = simulate(team, _enemy(), max_av=300)
        log = _log(s)
        assert '晴歌终结技: 姬子' in log
        assert '行动提前100%' in log
        assert '特邀嘉宾' in log


class TestTraces:
    def test_trace1_cr(self):
        """行迹1: 晴歌+晴空乐手CR+50%"""
        s = _sim(max_av=300)
        qg = s.units[0]
        assert qg.base_stats.CRIT_RATE >= 0.50
        assert '重构谐乐: 晴歌CR+50%' in _log(s)

    def test_trace2_rhythm(self):
        """行迹2: 受队友治疗→律动12层; 首次获得气氛消耗1层→回3能量"""
        s = _sim(max_av=400, with_fengjin=True)
        log = _log(s)
        assert '律动12层' in log
        assert '消耗1层律动' in log and '回3能量' in log

    def test_trace3_chord_cd(self):
        """行迹3: 风堇ATK<晴歌→暴伤+40%+气氛×1.5%"""
        s = _sim(max_av=400, with_fengjin=True)
        assert '偏离和弦: 风堇 暴伤+' in _log(s)


class TestEidolons:
    def test_e2_respen_and_cap(self):
        """E2: 全队全抗穿+18% + 气氛上限70 + 回合首获气氛额外+2"""
        s = _sim(eidolon=2, max_av=400, with_fengjin=True)
        log = _log(s)
        assert '全队全属性抗性穿透+18%' in log
        assert '/70' in log
        assert 'E2额外' in log

    def test_e4_atmo_and_spd(self):
        """E4: 进Fever立即+12气氛 + 晴空乐手速度加成"""
        s = _sim(eidolon=4, max_av=400, with_fengjin=True)
        log = _log(s)
        assert '气氛+12' in log and '(E4)' in log
        assert '登台行动' in log

    def test_e6_double_energy(self):
        """E6: 首次进Fever回140 + 倒计时回140 + 能量上限×2(存2次终结技)"""
        s = _sim(eidolon=6, max_av=400, with_fengjin=True)
        log = _log(s)
        assert '首次进入Fever, 回140能量' in log
        assert 'Fever倒计时回合开始, 回140能量' in log
        assert '(280)' in log  # 能量储存2次

    def test_e6_multiplier(self):
        """E6: 忆灵技倍率×2——引擎级对照(v7.0.0 A5 重写)
        此前断言 dealt>0 + calculate_damage自证, 未验证引擎实际倍率;
        现跑引擎真实调用链: E6档(含E5忆灵技+1) = E0×1.05×2.0"""
        from engine.runtime import SimState, SimUnit
        from engine.core.attributes import compute_combat_stats
        from engine.systems.remembrance import RemembranceSystem

        def _dealt(rank, boost):
            char = _qingge()
            stats = compute_combat_stats(char, None, None, None)
            u = SimUnit(char=char, base_stats=stats, position=1)
            u.max_hp = u.current_hp = stats.HP
            u.eidolon_rank = rank
            u.extra['skill_level_boost'] = boost
            state = SimState(enemies=[_enemy()], units=[u])
            state.extra['navs'] = {0: 100.0}
            rem = RemembranceSystem()
            state.extra['_rem_sys'] = rem
            ms = _qingge_summon_variant(state, u, char.memsprite, '贝茜')
            before = state.enemies[0].HP
            rem._use_memsprite_skill(state, u, ms, 'memsprite_basic')
            return before - state.enemies[0].HP

        d0 = _dealt(0, {})
        d6 = _dealt(6, {'memsprite_skill': 1})
        assert d0 > 0
        assert d6 == pytest.approx(d0 * 2.10, rel=1e-9)


class TestTechniqueAndLightcone:
    def test_technique(self):
        """秘技: 开战行动提前20% + 6气氛 + 全队伤害+30% 2回合"""
        s = _sim(max_av=150)
        log = _log(s)
        assert '行动提前20% + 6气氛 + 全队伤害+30%' in log
        assert s.units[0].extra.get('qingge_atmo', 0.0) >= 6

    def test_lc_rise_and_sing_rank5_hp(self):
        """光锥[你将起身歌唱] S5: HP+60%(叠影档values)"""
        from engine.core.attributes import compute_combat_stats
        char = _qingge()
        lc = load_lightcone('rise_and_sing', 'data/light_cones')
        lc.rank = 5
        base = compute_combat_stats(char, None, None, None)
        with_lc = compute_combat_stats(char, lc, None, None)
        # HP_percent 与行迹18%加算: 白值(角色+光锥)×(1+0.18+0.60)
        white = char.base_HP + lc.base_HP
        assert with_lc.HP == pytest.approx(white * 1.78, rel=1e-9)
        assert base.HP == pytest.approx(char.base_HP * 1.18, rel=1e-9)

    def test_lc_rise_and_sing_entry(self):
        """光锥进战: 行动提前30%(S1) + 新声全队速度+20%"""
        lc = load_lightcone('rise_and_sing', 'data/light_cones')
        lc.rank = 1
        s = _sim(max_av=150, lightcone=lc)
        log = _log(s)
        assert '行动提前30% + 新声' in log
        assert '新声' in log

    def test_lc_rise_and_sing_ult_sp(self):
        """光锥: 终结技后回1战技点"""
        lc = load_lightcone('rise_and_sing', 'data/light_cones')
        s = _sim(max_av=400, lightcone=lc)
        assert '终结技回1战技点' in _log(s)
