"""v5.2 Web API 边界与前端请求兼容回归。"""
import pytest
from fastapi.testclient import TestClient

from web.app import app
from web.api import EnemyConfig


client = TestClient(app)


def _request(member):
    return {
        "team": [member],
        "enemy": {"hp": 50000, "def": 800, "toughness": 20, "count": 1},
        "max_av": 1,
    }


def test_recommend_accepts_frontend_null_lightcone():
    """前端未选择光锥时传 null，推荐接口仍应返回一份分配。"""
    response = client.post(
        "/api/recommend",
        json=_request({
            "char_id": "seele", "lc_id": None, "total_rolls": 30,
            "relics": {}, "substats": {},
        }),
    )

    assert response.status_code == 200
    recommendations = response.json()["recommendations"]
    assert len(recommendations) == 1
    assert recommendations[0]["total"] == 30


def test_simulate_accepts_frontend_null_lightcone():
    """与页面 getConfig 一致的 null 光锥请求可直接运行模拟。"""
    response = client.post(
        "/api/simulate",
        json=_request({
            "char_id": "seele", "lc_id": None, "relics": {}, "substats": {},
        }),
    )

    assert response.status_code == 200
    assert "summary" in response.json()


def test_enemy_config_accepts_effect_resistance():
    """敌方效果抵抗必须能从 Web 请求模型进入模拟边界。"""
    enemy = EnemyConfig(effect_res=0.5)

    assert enemy.effect_res == 0.5


@pytest.mark.parametrize("endpoint", ["/api/preview", "/api/recommend", "/api/simulate"])
def test_all_endpoints_reject_non_whitelisted_lightcone(endpoint):
    """所有配置入口都必须拒绝路径型光锥 ID。"""
    response = client.post(
        endpoint,
        json=_request({
            "char_id": "seele", "lc_id": "../characters/bronya",
            "relics": {}, "substats": {},
        }),
    )

    assert response.status_code == 422


def test_recommend_reports_optimizer_failure(monkeypatch):
    """Optimizer exceptions must not be returned as an empty success result."""
    import engine.core.relic_optimizer as relic_optimizer

    def raise_error(*args, **kwargs):
        raise RuntimeError("recommend failed")

    monkeypatch.setattr(relic_optimizer, "recommend_substats_full", raise_error)
    response = client.post(
        "/api/recommend",
        json=_request({
            "char_id": "seele", "lc_id": None, "total_rolls": 30,
            "relics": {}, "substats": {},
        }),
    )

    assert response.status_code == 500
    assert "recommendation" in response.json()["detail"]


def test_recommended_break_character_speed_meets_its_target_in_preview():
    request = _request({
        "char_id": "lingsha", "lc_id": None, "total_rolls": 30,
        "relics": {}, "substats": {},
    })
    recommendation = client.post("/api/recommend", json=request)
    assert recommendation.status_code == 200

    request["team"][0]["substats"] = recommendation.json()["recommendations"][0]["rolls"]
    preview = client.post("/api/preview", json=request)

    assert preview.status_code == 200
    assert preview.json()["previews"][0]["SPD"] >= 134.0


def test_listed_lightcone_id_with_localized_filename_is_loadable():
    """列表返回的 JSON id 必须能加载，即使磁盘文件名是中文。"""
    listed = client.get("/api/list")
    assert listed.status_code == 200
    assert any(lc["id"] == "time_waits_for_no_one"
               for lc in listed.json()["light_cones"])

    response = client.post(
        "/api/preview",
        json=_request({
            "char_id": "lingsha", "lc_id": "time_waits_for_no_one",
            "relics": {}, "substats": {},
        }),
    )

    assert response.status_code == 200
    assert response.json()["previews"][0]["name"] == "灵砂"
