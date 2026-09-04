# -*- coding: utf-8 -*-
"""v7.14.0 (M6): 依赖方向锁定测试。

方向约定（与 CLAUDE_HANDOFF M5~M8 v2 计划一致）：
- characters(L2) → core 引擎/runtime/systems（顶层可导入 combat_engine）
- combat_engine 顶层禁 import characters / effect_resolver / remembrance / elation
  （角色层触达只经 activate()/build_phase_tables 延迟导入或函数级延迟导入）
- systems 顶层禁 import characters（防环；角色触达保持函数级延迟）
"""
import ast
import glob
import io
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(REPO, 'engine')


def _roster_ids():
    ids = set()
    for f in glob.glob(os.path.join(REPO, 'data', 'characters', '*.json')):
        if f.endswith('_template.json') or f.endswith('.bak.json'):
            continue
        ids.add(json.load(open(f, encoding='utf-8'))['id'])
    return ids

COMBAT_SIM_FORBIDDEN_TOP = {
    'engine.characters', 'engine.core.effect_resolver',
    'engine.systems.remembrance', 'engine.systems.elation',
}
SYSTEMS_FORBIDDEN_TOP = {'engine.characters'}
# 豁免：techniques.py 是纯注册壳（_PILOT_TECHNIQUES 读角色模块, 同 effect_resolver 形态；
# combat_engine 不在顶层导入 techniques, 无环）——顶层允许 characters。
SYSTEMS_EXEMPT = {'techniques.py'}


def _top_imports(path):
    with io.open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or '')
    return mods


class TestDependencyDirection:
    def test_combat_engine_top_imports_clean(self):
        mods = _top_imports(os.path.join(ENGINE, 'core', 'combat_engine.py'))
        bad = {m for m in mods
               if any(m == f or m.startswith(f + '.') for f in COMBAT_SIM_FORBIDDEN_TOP)}
        assert not bad, f"combat_engine 顶层出现禁入导入: {sorted(bad)}"

    def test_systems_top_imports_clean(self):
        for fname in os.listdir(os.path.join(ENGINE, 'systems')):
            if not fname.endswith('.py') or fname == '__init__.py' or fname in SYSTEMS_EXEMPT:
                continue
            mods = _top_imports(os.path.join(ENGINE, 'systems', fname))
            bad = {m for m in mods
                   if any(m == f or m.startswith(f + '.') for f in SYSTEMS_FORBIDDEN_TOP)}
            assert not bad, f"engine/systems/{fname} 顶层出现禁入导入: {sorted(bad)}"

    def test_characters_modules_present(self):
        """characters 包规模下限（PILOTS=42; 仅 stdlib 导入的模块亦合法）。"""
        chars_dir = os.path.join(ENGINE, 'characters')
        py = [f for f in os.listdir(chars_dir)
              if f.endswith('.py') and f != '__init__.py']
        assert len(py) >= 40, len(py)

    def test_elation_zero_char_literals(self):
        """v7.15.0 验收主断言：欢愉系统零角色 id 字面量（机制层不含角色分支）。"""
        roster = _roster_ids()
        path = os.path.join(ENGINE, 'systems', 'elation.py')
        tree = ast.parse(io.open(path, encoding='utf-8').read())
        hits = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Compare):
                continue
            for c in n.comparators:
                vals = c.elts if isinstance(c, ast.Tuple) else [c]
                for v in vals:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) \
                            and v.value in roster:
                        hits.append((n.lineno, v.value))
        assert hits == [], hits[:5]

    def test_remembrance_zero_char_literals(self):
        """v7.16.0 验收主断言：记忆系统零角色 id 字面量（机制层不含角色分支）。"""
        roster = _roster_ids()
        path = os.path.join(ENGINE, 'systems', 'remembrance.py')
        tree = ast.parse(io.open(path, encoding='utf-8').read())
        hits = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Compare):
                continue
            for c in n.comparators:
                vals = c.elts if isinstance(c, ast.Tuple) else [c]
                for v in vals:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) \
                            and v.value in roster:
                        hits.append((n.lineno, v.value))
        assert hits == [], hits[:5]

    def test_monkeypatch_visibility_exempt_imports_pinned(self):
        """钉扎 monkeypatch 豁免：_use_skill/_process_lc_effects 保持函数级导入
        （tests 会 patch combat_engine 属性; v7.15.0 迁移后的 AI/诗函数同样保持）。"""
        path = os.path.join(ENGINE, 'systems', 'remembrance.py')
        src = io.open(path, encoding='utf-8').read()
        indented_use = len([ln for ln in src.split('\n')
                            if ln.startswith((' ', '\t'))
                            and 'from engine.core.combat_engine import _use_skill' in ln])
        indented_lc = len([ln for ln in src.split('\n')
                           if ln.startswith((' ', '\t'))
                           and 'from engine.core.combat_engine import _process_lc_effects' in ln])
        assert indented_lc >= 4, indented_lc  # v7.15.0 实测 5 处
        # 迁移后的 6 个记忆 AI 所在模块保持函数级 _use_skill
        for fname in ('xiadie.py', 'xilian.py', 'aglaea.py', 'fengjin.py',
                      'robin_summeretto.py', 'trailblazer_remembrance.py'):
            msrc = io.open(os.path.join(ENGINE, 'characters', fname), encoding='utf-8').read()
            assert any(ln.startswith((' ', '\t'))
                       and 'from engine.core.combat_engine import _use_skill' in ln
                       for ln in msrc.split('\n')), fname
