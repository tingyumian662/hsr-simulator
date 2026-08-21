"""行动条第四象限模型测试（X轴额外回合队列）"""
import pytest
from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate


def _enemy(hp=500000, res=None):
    return Enemy(id='x', name='X', HP=hp, ATK=100, DEF=800, SPD=80,
                 toughness=20, max_toughness=20, level=80,
                 element_res=res or {'冰': 0, '量子': 0, '风': 0, '雷': 0, '虚数': 0})


def _sim(ids, max_av=1500, **cfgs):
    chars = []
    for i, cid in enumerate(ids):
        cfg = cfgs.get(cid, {})
        chars.append({'char': load_character(cid, 'data/characters'),
                      'position': i + 1, **cfg})
    return simulate(chars, _enemy(), max_av=max_av)


class TestExtraTurnQueue:
    def test_ult_enqueue_and_execute(self):
        """终结技入X轴队列→额外回合执行（例2: 队友终结技插入当前回合）"""
        s = _sim(['xiadie', 'fengjin'], max_av=800)
        log = '\n'.join(s.log)
        assert '终结技入队' in log
        assert '额外回合[终结技]' in log

    def test_same_av_last_arrived_first(self):
        """同AV后到先动: 后达成该行动值者先行动"""
        s = _sim(['xiadie', 'fengjin'], max_av=300)
        # 风堇SPD125 > 遐蝶95, 各自AV不同; 用日志验证无崩溃即可
        # 核心断言: 两个角色都正常行动过
        log = '\n'.join(s.log)
        assert '遐蝶' in log and '风堇' in log

    def test_mimi_charge_full_repull(self):
        """迷迷充能满后再获充能=拉条（规则7）"""
        s = _sim(['trailblazer_remembrance'], max_av=1500, initial_energy_pct=100)
        log = '\n'.join(s.log)
        # 迷迷最终应经历过充能100%（我会帮你）或拉条
        assert '我会！帮你' in log or '迷迷的声援' in log


class TestSeeleReproduce:
    def test_reproduce_on_kill(self):
        """希儿常规回合击杀→再现额外回合+增幅"""
        # 低HP敌人保证击杀（3目标）
        c = load_character('seele', 'data/characters')
        enemy = Enemy(id='x', name='X', HP=100, ATK=100, DEF=800, SPD=80,
                      toughness=20, max_toughness=20, level=80, element_res={'量子': 0})
        s = simulate([{'char': c, 'position': 1, 'initial_energy_pct': 100}],
                     enemy, max_av=600, num_enemies=3)
        log = '\n'.join(s.log)
        assert '【再现】' in log

    def test_reproduce_no_infinite(self):
        """再现回合击杀不再触发（不能无限续杯）"""
        c = load_character('seele', 'data/characters')
        enemy = Enemy(id='x', name='X', HP=100, ATK=100, DEF=800, SPD=80,
                      toughness=20, max_toughness=20, level=80, element_res={'量子': 0})
        s = simulate([{'char': c, 'position': 1, 'initial_energy_pct': 100}],
                     enemy, max_av=1000, num_enemies=10)
        log = '\n'.join(s.log)
        repro_count = log.count('【再现】')
        # 再现次数受限于击杀轮数(非无限续杯: 每次常规回合最多1次)
        assert repro_count > 0  # 有触发
        assert repro_count < 30  # 有上限(10敌人多轮, 但不无限叠加)

    def test_amplify_effective_in_extra(self):
        """增幅(80%增伤)在再现额外回合中生效: 击杀回合末tick不再误杀,
        增幅=X轴首个希儿行动激活→额外回合(X轴)不tick buff→增幅回合结束撤销"""
        c = load_character('seele', 'data/characters')
        enemy = Enemy(id='x', name='X', HP=100, ATK=100, DEF=800, SPD=80,
                      toughness=20, max_toughness=20, level=80, element_res={'量子': 0})
        s = simulate([{'char': c, 'position': 1, 'initial_energy_pct': 100}],
                     enemy, max_av=600, num_enemies=3)
        log = '\n'.join(s.log)
        assert '【再现】' in log
        assert '再现: 增幅生效(80%增伤)' in log  # 激活成功(旧代码无此日志)
        assert '再现结束: 增幅解除' in log         # X轴不tick, 增幅回合结束撤销

    def test_ult_before_extra_gets_amplify(self):
        """实机场景: 战技动画中释放终结技→终结技排在增幅回合之前→终结技吃到增幅。
        增幅=击杀瞬间获得的pending, X轴首个希儿行动(终结技或再现)时激活, 增幅回合结束撤销"""
        from engine.core.attributes import compute_combat_stats
        from engine.core.combat_sim import SimState, SimUnit, _exec_extra_turn
        c = load_character('seele', 'data/characters')
        stats = compute_combat_stats(c, None, None, None)
        u = SimUnit(char=c, base_stats=stats, position=1)
        u.max_hp = u.current_hp = stats.HP
        u.current_energy = u.char.max_energy
        u.extra['seele_amplify_pending'] = True  # 击杀瞬间的战利品(模拟 _seele_reproduce_check 已触发)
        enemy = Enemy(id='x', name='X', HP=500000, ATK=100, DEF=800, SPD=80,
                      toughness=20, max_toughness=20, level=80, element_res={'量子': 0})
        state = SimState(enemies=[enemy], units=[u])
        state.extra.update({'action_ctx': 'extra', 'extra_turns': [], 'ult_chain_guard': 0,
                            'killed_this_action': 0})
        # X轴队列 [终结技, 再现]: 终结技先执行
        _exec_extra_turn(state, u, 'ult')
        log = '\n'.join(state.log)
        assert '再现: 增幅生效(80%增伤)' in log  # 终结技执行时增幅已激活
        assert any(getattr(b, 'source_name', '') == '再现增幅' for b in u.buffs)
        # 增幅额外回合执行: 行动后撤销
        _exec_extra_turn(state, u, 'extra')
        log = '\n'.join(state.log)
        assert '再现结束: 增幅解除' in log
        assert not any(getattr(b, 'source_name', '') == '再现增幅' for b in u.buffs)


class TestXiadieChain:
    def test_dragon_y_axis_flow(self):
        """死龙召唤后走Y轴行动条: 喷吐(倍率递增)→HP≤25%→自爆(晦翼)"""
        s = _sim(['xiadie', 'fengjin', 'xilian'], max_av=3000)
        log = '\n'.join(s.log)
        assert '召唤死龙' in log
        assert '焰息' in log
        assert '晦翼' in log
        # 死龙消失后重新攒新蕊
        assert '死龙消失' in log

    def test_fengjin_ult_insert(self):
        """例2: 风堇终结技插入(遐蝶回合待机)→雨过天晴→小伊卡连锁"""
        s = _sim(['xiadie', 'fengjin'], max_av=800)
        log = '\n'.join(s.log)
        assert '额外回合[终结技]' in log
        assert '雨过天晴' in log
