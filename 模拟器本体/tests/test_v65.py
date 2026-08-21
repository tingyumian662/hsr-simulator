"""v6.5.0 回归: 异构敌人列表（前端逐只配置 精英/弱点/HP/韧性）"""
import copy
from pathlib import Path

import pytest

from engine.models.character import load_character
from engine.models.enemy import Enemy
from engine.core.combat_sim import simulate, _respawn_wave, SimState


def _enemy(eid='x', name='X', hp=500000, tough=200, elite=False, attacks=None):
    res = {k: 0.2 for k in ['冰', '量子', '风', '雷', '虚数', '物理', '火']}
    return Enemy(id=eid, name=name, HP=hp, ATK=600, DEF=800, SPD=80,
                 toughness=tough, max_toughness=tough, level=80,
                 element_res=res, actions_per_turn=2 if elite else 1,
                 attacks=attacks)


CHARGE = [
    {'name': '蓄力', 'element': '物理', 'damage_type': 'direct',
     'multiplier': 40.0, 'target_type': 'single_enemy', 'priority': 4,
     'self_buffs': [{'id': 'rage', 'name': '狂暴',
                     'attributes': {'ATK_PERCENT': 0.5}, 'duration': 1}]},
    {'name': '狂暴挥击', 'element': '物理', 'damage_type': 'direct',
     'multiplier': 150.0, 'target_type': 'single_enemy', 'priority': 5,
     'requires_buff': 'rage'},
]


class TestHeterogeneousEnemies:
    def test_mixed_normal_and_elite(self):
        """异构列表: 普通怪 + 精英怪（双动蓄力循环）同场, 个体 HP 独立"""
        normal = _enemy(eid='n', name='杂兵', hp=8000, tough=40)
        elite = _enemy(eid='e', name='凶兽', hp=50000, tough=200, elite=True,
                       attacks=CHARGE)
        configs = [{'char': load_character('seele'), 'lightcone': None,
                    'relics': [], 'relic_sets': {}, 'position': 1, 'eidolon': 0}]
        state = simulate(configs, normal, max_av=1200.0,
                         enemy_templates=[normal, elite])
        assert len(state.enemies) == 2
        assert state.enemies[0].actions_per_turn == 1
        assert state.enemies[1].actions_per_turn == 2
        # 个体 HP 独立（战斗中会掉血, 验证两者数值不同即可）
        assert state.enemies[0].max_hp != state.enemies[1].max_hp \
            if hasattr(state.enemies[0], 'max_hp') else True
        assert state.enemies[0].HP != state.enemies[1].HP
        lines = [l for l in state.log if '蓄力' in l or '狂暴挥击' in l]
        assert len(lines) >= 2  # 精英双动循环正常
        assert not any('[ERROR]' in l for l in state.log)

    def test_respawn_recreates_heterogeneous_wave(self):
        """波次重生按模板列表逐只重建（个体 HP/精英属性保留）"""
        normal = _enemy(eid='n', name='杂兵', hp=8000, tough=40)
        elite = _enemy(eid='e', name='凶兽', hp=50000, tough=200, elite=True)
        state = SimState(enemies=[normal, elite])
        state.extra['enemy_blueprint'] = copy.deepcopy(normal)
        state.extra['enemy_blueprints'] = [copy.deepcopy(normal), copy.deepcopy(elite)]
        state.extra['num_enemies'] = 2
        state.extra['wave'] = 1
        state.extra['navs'] = {}
        _respawn_wave(state)
        assert len(state.enemies) == 2
        assert state.enemies[0].HP == 8000
        assert state.enemies[1].HP == 50000
        assert state.enemies[1].actions_per_turn == 2
        assert state.extra['wave'] == 2


class TestWebApiEnemies:
    def test_simulate_with_enemies_list(self):
        """API: enemies 列表（普通+精英）→ enemy_status 两敌人 + 精英双动日志"""
        from fastapi.testclient import TestClient
        from web.app import app
        client = TestClient(app)
        body = {
            "team": [{"char_id": "seele", "position": 1}],
            "enemies": [
                {"name": "杂兵", "hp": 8000, "def": 600, "toughness": 40,
                 "weakness": ["量子"]},
                {"name": "凶兽", "elite": True, "hp": 50000, "def": 800,
                 "toughness": 200, "weakness": ["量子", "虚数"]},
            ],
            "max_av": 300,
        }
        r = client.post('/api/simulate', json=body)
        assert r.status_code == 200
        data = r.json()
        assert len(data['enemy_status']) == 2
        assert data['_debug']['num_enemies'] == 2
        # 精英双动: 同一 AV 连续两次攻击日志
        elite_lines = [l for l in data['log'] if '凶兽' in l and '挥击' in l]
        assert len(elite_lines) >= 2


class TestMemspriteHitHooks:
    def test_memsprite_hit_does_not_crash_hp_loss_handlers(self):
        """v6.5.1: 敌方选中忆灵受击 → on_hp_loss 广播 u=忆灵, 流萤/风堇处理器必须过滤
        （此前 MemSpriteUnit.char 无 id → AttributeError 随机崩溃）"""
        import random
        from engine.models.enemy import Enemy
        random.seed(0)  # 修复前 seed 0 必崩（长夜被选中→流萤减伤处理器裸 .char.id）
        enemy = Enemy(id='x', name='X', HP=500000, ATK=600, DEF=600, SPD=80,
                      toughness=40, max_toughness=40, level=80,
                      element_res={k: 0.0 if k == '量子' else 0.2
                                   for k in ['冰', '量子', '风', '雷', '虚数', '物理', '火']},
                      actions_per_turn=2,
                      attacks=[{'name': '挥击', 'element': '物理', 'damage_type': 'direct',
                                'multiplier': 100.0, 'target_type': 'single_enemy', 'priority': 0}])
        configs = [{'char': load_character(c), 'lightcone': None, 'relics': [],
                    'relic_sets': {}, 'position': i + 1, 'eidolon': 0}
                   for i, c in enumerate(['changyeyue', 'firefly'])]
        state = simulate(configs, enemy, max_av=200.0,
                         enemy_templates=[enemy, copy.deepcopy(enemy)])
        assert not any('[ERROR]' in l for l in state.log)
        assert state.turn_count > 0


class TestEnemyConfigLayout:
    def test_enemy_type_selector_has_a_bounded_grid_column(self):
        """The enemy type selector must not expand beyond its configuration card."""
        css = Path('web/static/style.css').read_text(encoding='utf-8')

        assert '.enemy-slot-head { display: grid;' in css
        assert 'grid-template-columns: minmax(0,1fr) minmax(96px,118px) 26px;' in css
        assert '.enemy-slot-head select { width: 100%; min-width: 0; }' in css
