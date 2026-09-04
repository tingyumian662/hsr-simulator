"""trailblazer_harmony（M4 收官批·最小模块：战斗逻辑数据驱动, 仅推荐层击破配置）"""

CHAR_ID = "trailblazer_harmony"
BREAK_CONFIG = {'spd_target': 134.0}


# ---- M5a: 技能 effect 处理器（原引擎 _apply_skill_effects 内联, verbatim 迁入）----

def _tbh_band_dance_duration(u, state, attrs, skill):
    """EFFECT_MUTATORS['tbh_band_dance']: 伴舞持续3回合。"""
    return attrs, 3


EFFECT_MUTATORS = {'tbh_band_dance': _tbh_band_dance_duration}
