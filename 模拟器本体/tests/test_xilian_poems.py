"""昔涟黄金裔献予诗系统测试（检测修复 + 分发 + 6 首已录入角色诗）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate
from engine.core.character_utils import GOLD_OFFSPRING_IDS, is_gold_offspring


def _enemy(hp=500000, toughness=20):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=toughness, max_toughness=toughness, level=80,
                 element_res={'冰': 0, '量子': 0, '风': 0, '雷': 0,
                              '虚数': 0, '物理': 0, '火': 0})


def _sim(ids, max_av=800, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(), max_av=max_av)


def _unit(cid, **extra):
    """直接构造 SimUnit 战斗单元（单元测试用）"""
    from engine.core.combat_sim import SimUnit
    from engine.core.attributes import compute_combat_stats
    c = load_character(cid, 'data/characters')
    stats = compute_combat_stats(c, None, None, None)
    u = SimUnit(char=c, base_stats=stats, position=1)
    u.max_hp = u.current_hp = stats.HP
    u.extra.update(extra)
    return u


class TestGoldDetection:
    def test_set_ids_fixed(self):
        """黄金裔兜底ID修正: 真实ID在, 错ID不在"""
        assert {"cerydra", "hysilens", "anaxa", "phainon",
                "xilian", "dan_heng_permansor_terrae"} <= GOLD_OFFSPRING_IDS
        assert not (GOLD_OFFSPRING_IDS & {"kezhuladela", "haiserin", "nakexia",
                                          "baiu", "danheng_tenghuang"})

    def test_shell_gold_detection(self):
        """空壳角色与新标记的昔涟均检出黄金裔"""
        for cid in ("cerydra", "hysilens", "anaxa", "phainon",
                    "dan_heng_permansor_terrae", "xilian"):
            assert is_gold_offspring(load_character(cid, 'data/characters')) is True, cid


class TestPoemDispatch:
    def _team(self, *ids):
        from engine.core.combat_sim import SimState
        from engine.systems.remembrance import RemembranceSystem
        units = [_unit(cid, position=i + 1) for i, cid in enumerate(ids)]
        state = SimState(enemies=[_enemy()], units=units)
        rem = RemembranceSystem()
        xilian = next(u for u in units if u.char.id == 'xilian')
        return state, rem, xilian

    def test_gold_poem_preferred(self):
        """黄金裔优先获诗, 不再吃+40%"""
        state, rem, xilian = self._team('seele', 'xiadie', 'xilian')
        rem._xilian_support_skill(state, xilian, None)
        log = '\n'.join(state.log)
        assert '献予「生死」之诗' in log
        assert '+40%伤害' not in log
        xiadie = next(u for u in state.units if u.char.id == 'xiadie')
        assert xiadie.extra.get('poem_shengsi') is True

    def test_non_gold_fallback(self):
        """无黄金裔→非黄金裔+40%分支(红线格式保留)"""
        state, rem, xilian = self._team('seele', 'xilian')
        rem._xilian_support_skill(state, xilian, None)
        log = '\n'.join(state.log)
        assert '此诗献予一切生命: 希儿+40%伤害(2回合)' in log

    def test_all_poems_activated(self):
        """v6.6: 13 首献予诗全部激活——无占位残留（POEM_EFFECTS 无 None）"""
        from engine.systems.remembrance import POEM_EFFECTS
        assert all(fn is not None for fn in POEM_EFFECTS.values())
        assert len(POEM_EFFECTS) == 13

    def test_round_robin(self):
        """多黄金裔轮流: 未获诗优先(遐蝶→风堇)"""
        state, rem, xilian = self._team('xiadie', 'fengjin', 'xilian')
        rem._xilian_support_skill(state, xilian, None)
        rem._xilian_support_skill(state, xilian, None)
        log = '\n'.join(state.log)
        assert '献予「生死」之诗' in log and '献予「天空」之诗' in log


class TestPoemEffects:
    @pytest.mark.parametrize('team,log_frag', [
        (['xiadie', 'xilian'], '献予「生死」之诗'),
        (['changyeyue', 'xilian'], '献予「岁月」之诗'),
        (['fengjin', 'xilian'], '献予「天空」之诗'),
        (['aglaea', 'xilian'], '献予「浪漫」之诗'),
        (['mydei', 'xilian'], '献予「纷争」之诗'),
        (['trailblazer_remembrance', 'xilian'], '献予「创世」之诗'),
    ])
    def test_poem_dispatched(self, team, log_frag):
        """6 首已录入角色诗: 德谬歌献予→日志出现"""
        s = _sim(team, max_av=3000)
        assert log_frag in '\n'.join(s.log)


class TestPoemNumerics:
    def test_shengsi_cap(self):
        """生死: 新蕊 cap 200%, 吸收不截断在34000"""
        from engine.core.combat_sim import SimState, _xiadie_absorb_hp_loss, xiadie_xinrui_cap
        u = _unit('xiadie', poem_shengsi=True)
        u.xinrui = 50000
        state = SimState(enemies=[_enemy()], units=[u])
        _xiadie_absorb_hp_loss(state, 1000, '测试')
        assert u.xinrui == 51000
        assert xiadie_xinrui_cap(u) == 68000

    def test_shengsi_overflow_to_huiyi(self):
        """生死: 召唤死龙消费溢出→晦翼倍率+0.48/1%(≤2敌)"""
        from engine.core.combat_sim import SimState
        from engine.systems.remembrance import RemembranceSystem
        u = _unit('xiadie', poem_shengsi=True, shengsi_overflow=34000.0)
        state = SimState(enemies=[_enemy()], units=[u])
        rem = RemembranceSystem()
        rem.summon_memsprite(state, u, u.char.memsprite)
        dragon = u.memsprite_unit
        assert dragon.extra.get('huiyi_mult_bonus', 0) == pytest.approx(48.0, abs=1e-6)

    def test_suiyue_yizhi(self):
        """岁月: 战技后忆质+4(行迹3+1 + 战技+2 + 岁月+1; v5.6.1 战技忆质接线)"""
        from engine.core.effect_resolver import _changyeyue_trace3
        from engine.core.combat_sim import SimState
        u = _unit('changyeyue', poem_suiyue=True)
        state = SimState(enemies=[_enemy()], units=[u])
        _changyeyue_trace3(u, state, 'skill')
        assert u.yizhi == 4  # 行迹+1 + 战技+2 + 岁月+1

    def test_suiyue_cd_dynamic(self):
        """岁月: 战技CD buff 动态(24+暴伤×12)"""
        from engine.core.combat_sim import SimState, _use_skill
        u = _unit('changyeyue', poem_suiyue=True)
        state = SimState(enemies=[_enemy()], units=[u])
        _use_skill(u, state, 'skill')
        cd_buff = [b for b in u.buffs if getattr(b, 'param_id', '') == 'changyeyue_skill_cd']
        assert cd_buff
        expect = 24.0 + u.base_stats.CRIT_DMG * 12.0
        assert cd_buff[0].attributes['CRIT_DMG'] == pytest.approx(expect, abs=1e-6)

    def test_tiankong_energy(self):
        """天空: 施放回24能量"""
        from engine.core.combat_sim import SimState
        from engine.systems.remembrance import _poem_tiankong
        u = _unit('fengjin')
        state = SimState(enemies=[_enemy()], units=[u])
        e0 = u.current_energy
        _poem_tiankong(state, None, None, u)
        assert u.current_energy - e0 == pytest.approx(24.0)

    def test_fenzheng_advance(self):
        """纷争: 非血仇万敌→行动提前100%"""
        from engine.core.combat_sim import SimState
        from engine.systems.remembrance import _poem_fenzheng
        u = _unit('mydei')
        state = SimState(enemies=[_enemy()], units=[u])
        state.extra['navs'] = {0: 500.0}
        state.current_av = 100.0
        _poem_fenzheng(state, None, None, u)
        assert state.extra['navs'][0] == 100.0

    def test_chuangshi_applied(self):
        """创世: 黑盒模拟后开拓者获诗标记"""
        s = _sim(['trailblazer_remembrance', 'xilian'], max_av=3000)
        tbr = next(u for u in s.units if u.char.id == 'trailblazer_remembrance')
        assert tbr.extra.get('poem_chuangshi_applied') is True
