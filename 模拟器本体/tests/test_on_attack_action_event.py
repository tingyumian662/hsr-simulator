"""M4 批2b: on_attack_action 事件契约测试（审核 P2-b3）。

晴歌经 INIT 注册 on_attack_action 处理器；非晴歌队广播必须为 no-op（返回 {}），
晴歌在队时代表性路径气氛+1，晴歌阵亡时处理器自守卫。
"""
import pytest

from helpers import _enemy, _unit
from engine.runtime import SimState


def test_no_handler_is_noop():
    """非晴歌队：trigger_all 返回空且无日志/气氛变化"""
    u = _unit('seele')
    state = SimState(enemies=[_enemy()], units=[u])
    before_log = len(state.log)
    result = state.hooks.trigger_all("on_attack_action", u=u, state=state, dealt=True)
    assert result == {}
    assert len(state.log) == before_log


def test_qingge_registered_and_atmo_gains():
    """晴歌在队：INIT 注册后广播→气氛+1（代表性主路径）"""
    from engine.characters import robin_summeretto
    qg = _unit('robin_summeretto')
    state = SimState(enemies=[_enemy()], units=[qg])
    robin_summeretto.INIT(state)
    atmo_before = qg.extra.get('qingge_atmo', 0)
    state.hooks.trigger_all("on_attack_action", u=qg, state=state, dealt=True)
    assert qg.extra.get('qingge_atmo', 0) > atmo_before


def test_qingge_dead_unit_noop():
    """晴歌处理器自守卫：dealt=False 时不产生变化"""
    from engine.characters import robin_summeretto
    qg = _unit('robin_summeretto')
    state = SimState(enemies=[_enemy()], units=[qg])
    robin_summeretto.INIT(state)
    atmo_before = qg.extra.get('qingge_atmo', 0)
    state.hooks.trigger_all("on_attack_action", u=qg, state=state, dealt=False)
    assert qg.extra.get('qingge_atmo', 0) == atmo_before
