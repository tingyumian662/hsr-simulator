"""v8.4.0: 两狼身份钉扎（甲·裁决16, 2026-10-01）。

项目主原话: "我叫欢愉队的银狼一直都是银狼Lv.999，或者是狼尊，虚无银狼就只是银狼。"
- 银狼Lv.999 = 狼尊 = yinlang（欢愉/虚数/隐藏分/银河城专属锥）
- 银狼 = silver_wolf（虚无/量子/常规能量110/植入缺陷/雨一直下）
判据按 id, 不按名字猜。本文件把该规约钉进数据/映射/文档三层, 防再次认错。
"""
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _enemy, _unit  # noqa: E402

from engine.core.combat_engine import SimState, simulate  # noqa: E402
from engine.models.character import load_character  # noqa: E402
from engine.models.equipment import load_lightcone  # noqa: E402
from engine.systems.elation import ElationSystem  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
DATA = ROOT / "data"


class TestYinlangIdentity:
    """银狼Lv.999（狼尊）五重身份 + 隐藏分归属。"""

    def test_identity(self):
        c = load_character('yinlang', 'data/characters')
        assert c.name == '银狼Lv.999'          # 数据侧名字自始如此（裁决16 甲级）
        assert c.path == '欢愉' and c.element == '虚数'
        assert c.max_energy == 0                # 无常规能量维度
        assert getattr(c, 'cast_number', None) == 999

    def test_hidden_score_lives_here(self):
        u = _unit('yinlang', lc_id='welcome_to_galaxy_city')
        st = SimState(enemies=[_enemy()], units=[u])
        es = ElationSystem()
        st.extra['_elation'] = es
        es.gain_hidden_score(st, u, 30.0)
        assert u.hidden_score == pytest.approx(30.0)

    def test_galaxy_city_is_her_sig(self):
        lc = load_lightcone('welcome_to_galaxy_city')
        c = load_character('yinlang', 'data/characters')
        assert lc.name == '欢迎来到银河城'
        assert lc.path == '欢愉' == c.path      # 命途相符=可佩戴


class TestSilverWolfIdentity:
    """银狼（虚无）五重身份 + 不参与隐藏分。"""

    def test_identity(self):
        c = load_character('silver_wolf', 'data/characters')
        assert c.name == '银狼'
        assert c.path == '虚无' and c.element == '量子'
        assert c.max_energy == 110

    def test_galaxy_city_wrong_path_for_her(self):
        lc = load_lightcone('welcome_to_galaxy_city')
        c = load_character('silver_wolf', 'data/characters')
        assert lc.path != c.path               # 欢愉锥她戴不上


class TestLightConeMapping:
    def test_galaxy_city_maps_to_yinlang(self):
        raw = json.loads((DATA / "light_cone_characters.json")
                         .read_text(encoding="utf-8"))
        # v8.4.0 修复: 原值"银狼"逐字撞虚无狼 name（专属徽标标错人）
        assert raw["欢迎来到银河城"] == "银狼Lv.999"
        assert raw["欢迎来到银河城"] != "银狼"


class TestBothWolvesCoexist:
    def test_same_team_only_yinlang_gains_hidden_score(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import smoke_baseline as sb
        recs = json.loads((DATA / "recommendations.json").read_text(encoding="utf-8"))
        relic_sets = sb._load_relic_sets()
        configs = [sb._build_member(cid, eid, i + 1, recs, relic_sets)
                   for i, (cid, eid) in enumerate(
                       [("yinlang", 0), ("silver_wolf", 0),
                        ("trailblazer_elation", 6), ("huohuo", 0)])]
        enemy = sb._build_enemy("battle", "木桩", 100000, 200, [], zero_res=True)
        random.seed(16)
        st = simulate(configs, enemy, max_av=200.0)
        yl = next(u for u in st.units if u.char.id == 'yinlang')
        sw = next(u for u in st.units if u.char.id == 'silver_wolf')
        assert not any("ERROR" in ln for ln in st.log)
        assert sw.hidden_score == 0.0          # 虚无狼永不参与隐藏分
        assert yl.hidden_score > 0             # 欢愉狼在欢愉队必攒分


class TestSourceAnchors:
    """原稿锚点: 两狼各有自己的 txt，互不混同。"""

    def test_txt_paths_exist(self):
        assert (ROOT / "角色技能介绍" / "欢愉" / "银狼Lv.999.txt").is_file()
        assert (ROOT / "角色技能介绍" / "虚无" / "银狼.txt").is_file()


class TestDocPins:
    """文档层钉扎: AGENTS 不变量与口径裁决16 在档。"""

    def test_agents_invariant(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        assert "两只银狼不是同一角色" in text
        assert "yinlang" in text and "silver_wolf" in text

    def test_elation_doc_ruling16(self):
        text = (REPO / "工作数据" / "项目文档" / "欢愉机制口径.md").read_text(encoding="utf-8")
        assert "零、术语：两只银狼" in text
        assert "或者是狼尊" in text
