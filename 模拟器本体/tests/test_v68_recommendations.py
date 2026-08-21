"""v6.8 推荐装备映射回归: data/recommendations.json 结构校验 + /api/list 附带 + 模板可解析

数据语义: 用户拍板（2026-08-15）——映射表+用户提供数据/选角色自动套用/无兜底/纳入主词条/
映射维护并入 character-pipeline 录入规范"""
import json
import glob
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from web.app import app


client = TestClient(app)
DATA = Path('data')

# v6.8.3: 按槽位拆分合法键（此前全局 MAIN_KEYS 混用, body=SPD_PERCENT 也能通过测试）
MAIN_KEYS_BY_SLOT = {
    'body': {'CRIT_RATE', 'CRIT_DMG', 'ATK_percent', 'HP_percent', 'DEF_percent',
             'HEAL_BONUS', 'EFFECT_HIT_RATE'},
    'feet': {'SPD_PERCENT', 'ATK_percent', 'HP_percent', 'DEF_percent'},
    'sphere': {'DMG_BONUS_QUANTUM', 'DMG_BONUS_PHYSICAL', 'DMG_BONUS_FIRE',
               'DMG_BONUS_ICE', 'DMG_BONUS_LIGHTNING', 'DMG_BONUS_WIND',
               'DMG_BONUS_IMAGINARY', 'ATK_percent', 'HP_percent', 'DEF_percent'},
    'rope': {'ATK_percent', 'ENERGY_REGEN', 'HP_percent', 'DEF_percent', 'BREAK_EFFECT'},
}


def _relic_names():
    names = set()
    for f in glob.glob(str(DATA / 'relics' / '*.json')):
        d = json.load(open(f, encoding='utf-8'))
        names.add(d['name'])
    return names


def _character_ids():
    ids = set()
    for f in glob.glob(str(DATA / 'characters' / '*.json')):
        if f.endswith('_template.json') or f.endswith('.bak.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        ids.add(d.get('id', Path(f).stem))
    return ids


def _lightcone_ids():
    ids = set()
    for f in glob.glob(str(DATA / 'light_cones' / '*.json')):
        if f.endswith('_template.json'):
            continue
        d = json.load(open(f, encoding='utf-8'))
        ids.add(d.get('id', Path(f).stem))
    return ids


class TestRecommendationsData:
    def test_json_loads(self):
        """映射表可解析"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        assert isinstance(rec, dict)
        assert rec  # 非空（至少 3 个已知专属）

    def test_keys_are_valid_characters(self):
        """映射键=存在的角色 id"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        assert set(rec) <= _character_ids()

    def test_light_cone_exists(self):
        """light_cone 存在于 data/light_cones"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        for cid, cfg in rec.items():
            if cfg.get('light_cone'):
                assert cfg['light_cone'] in _lightcone_ids(), cid

    def test_light_cone_path_matches_character(self):
        """推荐光锥命途必须与角色命途一致（前端按命途过滤, 不匹配则静默失效）"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        char_paths = {}
        for f in glob.glob(str(DATA / 'characters' / '*.json')):
            if f.endswith('_template.json') or f.endswith('.bak.json'):
                continue
            d = json.load(open(f, encoding='utf-8'))
            char_paths[d.get('id', Path(f).stem)] = d.get('path')
        lc_paths = {}
        for f in glob.glob(str(DATA / 'light_cones' / '*.json')):
            if f.endswith('_template.json'):
                continue
            d = json.load(open(f, encoding='utf-8'))
            lc_paths[d.get('id', Path(f).stem)] = d.get('path')
        for cid, cfg in rec.items():
            lc_id = cfg.get('light_cone')
            if not lc_id:
                continue
            assert lc_paths.get(lc_id) == char_paths.get(cid), \
                f"{cid} 光锥 {lc_id} 命途({lc_paths.get(lc_id)}) != 角色命途({char_paths.get(cid)})"

    def test_relic_sets_exist(self):
        """set4/set2 套装名（数组或字符串）存在于 data/relics"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        names = _relic_names()
        for cid, cfg in rec.items():
            for key in ('set4', 'set2'):
                val = cfg.get(key)
                if val:
                    vals = val if isinstance(val, list) else [val]
                    for v in vals:
                        assert v in names, f"{cid}.{key}.{v}"

    def test_main_stats_valid(self):
        """主词条键按槽位合法（数组或字符串）"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        for cid, cfg in rec.items():
            for key, allowed in MAIN_KEYS_BY_SLOT.items():
                val = cfg.get(key)
                if val:
                    vals = val if isinstance(val, list) else [val]
                    for v in vals:
                        assert v in allowed, f"{cid}.{key}.{v}"

    def test_first_is_default(self):
        """数组首个为默认选择（用户 2026-08-15 确认斜杠多选取第一个）"""
        with open(DATA / 'recommendations.json', encoding='utf-8') as f:
            rec = json.load(f)
        # 那刻夏外圈默认=翔鹰; 万敌躯干默认=生命
        assert rec['anaxa']['set4'][0] == '晨昏交界的翔鹰'
        assert rec['mydei']['body'][0] == 'HP_percent'
        # 银狼Lv.999 对调: 外圈=魔法少女(外), 内圈=朋克洛德(内)
        assert rec['yinlang']['set4'][0] == '闪耀功勋的魔法少女'
        assert rec['yinlang']['set2'][0] == '零号关卡朋克洛德'
        # 开拓者·欢愉光锥=欢愉满溢祝福（括号内解析）
        assert rec['trailblazer_elation']['light_cone'] == 'elation_overflow_blessing'

    def test_template_parses(self):
        """录入模板可解析"""
        with open(DATA / 'recommendations_template.json', encoding='utf-8') as f:
            tpl = json.load(f)
        assert '_field_notes' in tpl


class TestRecommendationsApi:
    def test_list_includes_recommendations(self):
        """/api/list 响应含 recommendations 且火花映射正确"""
        r = client.get('/api/list')
        assert r.status_code == 200
        body = r.json()
        assert 'recommendations' in body
        rec = body['recommendations']
        assert rec.get('sparxie', {}).get('light_cone') == 'dazzling_world'
        assert rec.get('evanescia', {}).get('light_cone') == 'encounter_next_bloom'
        assert rec.get('yaoguang', {}).get('light_cone') == 'when_she_decided_to_see'

    def test_list_still_core_fields(self):
        """原字段不受影响"""
        r = client.get('/api/list')
        body = r.json()
        assert 'characters' in body and 'light_cones' in body
        assert 'outer_relics' in body and 'inner_relics' in body
