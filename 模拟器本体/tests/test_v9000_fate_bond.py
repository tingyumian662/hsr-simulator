"""v9.0.0 批1: 奇缘星魂体系基建 + 黄泉奇缘星魂1·虚隙一顾…枯骨眠霜。

裁定①(2026-11-14): 奇缘星魂 = 独立开关, 勾选即生效, 无队伍条件
                   （文本没写的条件不臆加）。
裁定②: 黄泉 txt 新段即奇缘星魂1, 原稿仅补标签前缀（正文一字不动）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.runtime import SimState  # noqa: E402
from engine.models.character import Character, Eidolon, load_character  # noqa: E402
from engine.models.enemy import EnemyStatus  # noqa: E402
from engine.characters.acheron import (  # noqa: E402
    _acheron_bond_on_ally_attack, _acheron_tick,
)
from engine.core.effect_resolver import (  # noqa: E402
    EIDOLON_REGISTRY, resolve_character_effects,
)
from engine.core.combat_engine import simulate  # noqa: E402


def _bond_state(*units, enemies=None, refs=None, fate_bond=1):
    """黄泉奇缘测试场: 手工摆 units/enemies + 攻击目标引用。"""
    us = list(units)
    if not any(getattr(getattr(u, 'char', None), 'id', '') == 'acheron' for u in us):
        us.insert(0, _unit('acheron', fate_bond=fate_bond))
    st = SimState(enemies=list(enemies or [_enemy()]), units=us)
    st.extra['lc_attack_target_refs'] = list(refs if refs is not None else st.enemies)
    return st


class TestFateBondInfra:
    """fate_bonds 数据面/解析闸/SimUnit 通路。"""

    def test_acheron_json_shape(self):
        c = load_character('acheron', 'data/characters')
        assert len(c.fate_bonds) == 1
        b = c.fate_bonds[0]
        assert (b.rank, b.hook_name) == (1, 'acheron_bond1')
        assert '虚隙一顾' in b.name

    def test_missing_fate_bonds_defaults_empty(self):
        assert load_character('seele', 'data/characters').fate_bonds == []

    def test_registry_entry(self):
        tmpl = EIDOLON_REGISTRY['acheron_bond1']
        assert tmpl['trigger'] == 'on_enter_battle'  # 模块内联, action=None 同 acheron_e1/e6 先例

    def test_resolver_rank_gate(self):
        """fate_bond_rank 档位闸: rank>档位不入效果列表（借带 action 的模板验证过滤）。"""
        probe = Character(id='probe', name='探针', element='雷', path='虚无',
                          fate_bonds=[Eidolon(rank=1, name='pb', description='',
                                              hook_name='acheron_e5')])
        effs0 = resolve_character_effects(probe, fate_bond_rank=0)
        effs1 = resolve_character_effects(probe, fate_bond_rank=1)
        assert not [e for e in effs0 if e.source == 'fate_bond']
        assert len([e for e in effs1 if e.source == 'fate_bond']) == 1

    def test_unit_flag_default_zero(self):
        assert _unit('seele').fate_bond_rank == 0
        assert _unit('acheron', fate_bond=1).fate_bond_rank == 1


class TestAcheronBondFUA:
    """奇缘星魂1·虚隙一顾…枯骨眠霜: 队友攻击集真赤目标→追加攻击(视为终结技)。"""

    def test_fires_on_ally_attack_jizhen_target(self):
        mate = _unit('seele')
        e = _enemy(hp=500000)
        e.extra['acheron_jizhen'] = 3
        st = _bond_state(mate, enemies=[e])
        hp0 = e.HP
        _acheron_bond_on_ally_attack(st, mate)
        assert '虚隙一顾' in '\n'.join(st.log)
        assert e.HP < hp0
        acheron = next(u for u in st.units if u.char.id == 'acheron')
        assert acheron.extra['acheron_dream'] == 1          # +1残梦
        assert e.extra['acheron_jizhen'] == 4               # 集真赤最多者+1层
        assert acheron.extra['acheron_bond_used'] is True   # 每回合1次标记
        assert acheron.damage_log[-1][0] == '虚隙一顾…枯骨眠霜'

    def test_gate_fate_bond_zero(self):
        mate = _unit('seele')
        e = _enemy()
        e.extra['acheron_jizhen'] = 3
        st = _bond_state(mate, enemies=[e], fate_bond=0)
        hp0 = e.HP
        _acheron_bond_on_ally_attack(st, mate)
        assert e.HP == hp0 and '虚隙一顾' not in '\n'.join(st.log)

    def test_gate_acheron_self_not_teammate(self):
        e = _enemy()
        e.extra['acheron_jizhen'] = 3
        st = _bond_state(enemies=[e])
        acheron = next(u for u in st.units if u.char.id == 'acheron')
        hp0 = e.HP
        _acheron_bond_on_ally_attack(st, acheron)  # txt: 队友攻击触发, 不含自身
        assert e.HP == hp0

    def test_gate_once_per_cycle_reset_on_acheron_turn(self):
        mate = _unit('seele')
        e = _enemy(hp=500000)
        e.extra['acheron_jizhen'] = 3
        st = _bond_state(mate, enemies=[e])
        _acheron_bond_on_ally_attack(st, mate)
        hp1 = e.HP
        _acheron_bond_on_ally_attack(st, mate)   # 同回合第二次: 不触发
        assert e.HP == hp1
        acheron = next(u for u in st.units if u.char.id == 'acheron')
        _acheron_tick(st, acheron)               # 黄泉回合开始→重置
        assert 'acheron_bond_used' not in acheron.extra
        _acheron_bond_on_ally_attack(st, mate)   # 重置后可再触发
        assert e.HP < hp1

    def test_gate_target_without_jizhen(self):
        mate = _unit('seele')
        e = _enemy()  # 无集真赤
        st = _bond_state(mate, enemies=[e])
        hp0 = e.HP
        _acheron_bond_on_ally_attack(st, mate)
        assert e.HP == hp0

    def test_naluo_multiplier_applies(self):
        """txt: 行迹【奈落】对本次追加攻击生效——虚无队友1名→伤害×1.15。"""
        def _run(mate_id):
            mate = _unit(mate_id)
            e = _enemy(hp=900000)
            e.extra['acheron_jizhen'] = 3
            st = _bond_state(mate, enemies=[e])
            _acheron_bond_on_ally_attack(st, mate)
            acheron = next(u for u in st.units if u.char.id == 'acheron')
            return acheron.damage_log[-1][1]
        base = _run('seele')        # 巡猎队友 → 奈落 1.0
        buffed = _run('silver_wolf')  # 虚无队友1名 → 奈落 1.15
        assert buffed / base == pytest.approx(1.15, rel=1e-6)

    def test_e4_ultimate_vulnerability_applies(self):
        """视为终结技伤害: E4 终结技易伤 8% 生效。"""
        e = _enemy(hp=900000)
        e.add_status(EnemyStatus(
            id='acheron_e4_ultimate_vulnerability', name='终结技易伤',
            category='debuff', source='acheron', remaining_turns=-1,
            attributes={'vulnerability_ultimate': 0.08},
        ))
        e.extra['acheron_jizhen'] = 3
        st = _bond_state(_unit('seele'), enemies=[e])
        _acheron_bond_on_ally_attack(st, next(u for u in st.units if u.char.id == 'seele'))
        with_e4 = next(u for u in st.units if u.char.id == 'acheron').damage_log[-1][1]

        e2 = _enemy(hp=900000)
        e2.extra['acheron_jizhen'] = 3
        st2 = _bond_state(_unit('seele'), enemies=[e2])
        _acheron_bond_on_ally_attack(st2, next(u for u in st2.units if u.char.id == 'seele'))
        without = next(u for u in st2.units if u.char.id == 'acheron').damage_log[-1][1]
        assert with_e4 / without == pytest.approx(1.08, rel=1e-6)


class TestApiFlow:
    """全链路: simulate configs 的 fate_bond 键 → u.fate_bond_rank → 战斗内触发。"""

    def test_full_sim_flag_and_trigger(self):
        team = [
            {'char': load_character('acheron', 'data/characters'),
             'eidolon': 0, 'fate_bond': 1, 'position': 1},
            {'char': load_character('seele', 'data/characters'),
             'eidolon': 0, 'fate_bond': 0, 'position': 2},
        ]
        st = simulate(team, _enemy(), max_av=500)
        acheron = next(u for u in st.units if u.char.id == 'acheron')
        seele = next(u for u in st.units if u.char.id == 'seele')
        assert acheron.fate_bond_rank == 1
        assert seele.fate_bond_rank == 0
        # 行迹1开局为随机敌附5层集真赤 → 希儿攻击该目标后追击应出现在日志
        assert '虚隙一顾' in '\n'.join(st.log)
