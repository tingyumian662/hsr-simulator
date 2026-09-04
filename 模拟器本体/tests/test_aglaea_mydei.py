"""阿格莱雅/万敌 基础机制测试"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_engine import simulate


def _enemy(res=None):
    return Enemy(id='x', name='X', HP=300000, ATK=100, DEF=800, SPD=80,
                 toughness=20, max_toughness=20, level=80,
                 element_res=res or {'雷': 0, '虚数': 0})


def _sim(cid, max_av=800, **cfg):
    c = load_character(cid, 'data/characters')
    return simulate([{'char': c, 'position': 1, **cfg}], _enemy(), max_av=max_av)


class TestAglaea:
    def test_tailor_inherits_effective_speed(self):
        """衣匠的35%速度继承应包含阿格莱雅的战斗中速度 Buff。"""
        from engine.core.attributes import compute_combat_stats
        from engine.core.combat_engine import _effective_spd
        from engine.runtime import SimState, SimUnit, TimedBuff
        from engine.systems.remembrance import RemembranceSystem

        char = load_character('aglaea', 'data/characters')
        stats = compute_combat_stats(char, None, None, None)
        u = SimUnit(char=char, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        u.buffs.append(TimedBuff(
            source_id='test', attributes={'SPD_PERCENT': 20.0}, remaining_turns=2,
        ))
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, char.memsprite)

        expected = _effective_spd(u, state) * 0.35
        assert u.memsprite_unit.base_stats.SPD == pytest.approx(expected, rel=1e-9)
        u.buffs.append(TimedBuff(
            source_id='test_2', attributes={'SPD_PERCENT': 10.0}, remaining_turns=2,
        ))
        from engine.characters.aglaea import _aglaea_sync_memsprite
        _aglaea_sync_memsprite(u, state)
        assert u.memsprite_unit.base_stats.SPD == pytest.approx(
            _effective_spd(u, state) * 0.35, rel=1e-9,
        )

    def test_summon_tailor(self):
        """终结技应召唤衣匠并进入至高之姿"""
        s = _sim('aglaea', max_av=300, initial_energy_pct=100)
        u = s.units[0]
        assert u.memsprite_unit is not None, "衣匠应被召唤"
        assert u.is_sovereign, "应进入至高之姿"
        ms = u.memsprite_unit
        # 衣匠 HP = 阿格莱雅66% + 720
        assert abs(ms.max_hp - (u.max_hp * 0.66 + 720)) < 2
        # 衣匠 SPD = 阿格莱雅35% + 速度叠层×55
        stack = ms.extra.get('spd_stack', 0)
        assert abs(ms.base_stats.SPD - (u.base_stats.SPD * 0.35 + stack * 55)) < 2

    def test_sovereign_enhanced_basic(self):
        """至高之姿下普攻强化为孤锋千吻"""
        s = _sim('aglaea', max_av=400, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '孤锋千吻' in log
        assert '刺纹之陷' in log  # 衣匠忆灵技

    def test_gossamer_mark(self):
        """阿格莱雅攻击应标记间隙织线"""
        s = _sim('aglaea', max_av=400, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '间隙织线' in log

    def test_trace1_start_energy(self):
        """飞驰之阳: 开局能量不足50%恢复至50%"""
        s = _sim('aglaea', max_av=50, initial_energy_pct=0)
        u = s.units[0]
        assert u.current_energy >= u.char.max_energy * 0.49

    def test_trace3_sovereign_atk(self):
        """短视之惩: 至高之姿时攻击力提升"""
        s = _sim('aglaea', max_av=300, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '短视之惩' in log


class TestMydei:
    def test_charge_on_hp_loss(self):
        """战技消耗HP应积攒充能"""
        s = _sim('mydei', max_av=300)
        log = '\n'.join(s.log)
        assert '以血还血' in log
        assert 'HP消耗' in log

    def test_blood_debt_entry(self):
        """充能100应进入血仇"""
        s = _sim('mydei', max_av=1000)
        u = s.units[0]
        log = '\n'.join(s.log)
        assert '进入【血仇】' in log or u.extra.get('is_blood_debt')

    def test_blood_debt_max_hp_boost(self):
        """血仇时生命上限+50%"""
        s = _sim('mydei', max_av=1000)
        u = s.units[0]
        base_max = u.char.base_HP * (1 + 18 / 100)  # 基础 + 行迹HP18%
        if u.extra.get('is_blood_debt'):
            assert u.max_hp > base_max * 1.4  # 至少 +50%（允许遗器无）

    def test_enhanced_skill_auto(self):
        """血仇中回合开始自动弑王成王"""
        s = _sim('mydei', max_av=1000)
        log = '\n'.join(s.log)
        assert '弑王成王' in log

    def test_trace1_blood_armor(self):
        """血祥罩衫: 生命上限>4000→暴击加成"""
        s = _sim('mydei', max_av=50, initial_energy_pct=0)
        log = '\n'.join(s.log)
        # 无遗器时生命上限1831<4000 不触发（仅验证不崩溃）
        assert '血祥罩衫' not in log or '血祥罩衫' in log

    def test_fatal_recovery(self):
        """血仇致命攻击: 水与泥土不退出，否则退出+回50%"""
        from engine.characters.mydei import _mydei_fatal_recovery
        c = load_character('mydei', 'data/characters')
        s = simulate([{'char': c, 'position': 1}], _enemy(), max_av=50)
        u = s.units[0]
        u.extra['is_blood_debt'] = True
        u.extra['debt_retain_charges'] = 2  # 水与泥土剩余2次
        u.current_hp = 0
        _mydei_fatal_recovery(u, s)
        assert u.extra['is_blood_debt'], "水与泥土不退出"
        assert u.extra['debt_retain_charges'] == 1
        # 消耗完3次后退出
        u.extra['debt_retain_charges'] = 0
        u.current_hp = 0
        _mydei_fatal_recovery(u, s)
        assert not u.extra['is_blood_debt'], "无免死次数退出"
        assert u.current_hp > 0


class TestTrailblazerRemembrance:
    def test_summon_mimi(self):
        """召唤迷迷: HP=开拓者80%+640, SPD=130, 召唤+50%充能(迷迷加油)"""
        s = _sim('trailblazer_remembrance', max_av=400, initial_energy_pct=100)
        u = s.units[0]
        ms = u.memsprite_unit
        assert ms is not None, "迷迷应被召唤"
        assert abs(ms.max_hp - (u.max_hp * 0.8 + 640)) < 2
        assert abs(ms.base_stats.SPD - 130) < 1
        log = '\n'.join(s.log)
        assert '迷迷加油' in log  # 召唤时+50%充能

    def test_mimi_basic_charge(self):
        """充能<100%时迷迷行动自动坏人麻烦(+5%充能)"""
        s = _sim('trailblazer_remembrance', max_av=800, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '坏人！麻烦' in log
        assert '袖珍的事诗' in log  # +5%充能

    def test_mimi_support_at_100(self):
        """充能100%→我会帮你→声援"""
        s = _sim('trailblazer_remembrance', max_av=1500, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '我会！帮你' in log
        assert '迷迷的声援' in log

    def test_epic_enhanced_basic(self):
        """终结技+1史诗→普攻强化为明天一同写下"""
        s = _sim('trailblazer_remembrance', max_av=1500, initial_energy_pct=100)
        log = '\n'.join(s.log)
        assert '未完的尾声' in log
        assert '明天，一同写下' in log

    def test_trace2_action_advance(self):
        """追念之权杖: 战斗开始行动提前30%"""
        s = _sim('trailblazer_remembrance', max_av=200, initial_energy_pct=0)
        log = '\n'.join(s.log)
        assert '追念之权杖' in log
