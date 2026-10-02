"""Erzwingt die Package-Grenzen aus CLAUDE.md per AST-Scan.

* ``forge-core`` importiert nichts aus execute/cli/adapters.
* ``forge-execute`` importiert nichts aus cli/adapters.
* ``forge-cli`` kennt keinen konkreten Anbieter: aus ``forge_adapters`` nur die
  neutralen Module (base, text, registry, fake). Einzige Ausnahme ist der
  Legacy-``run_subprocess``-Pfad in ``review_pr`` (baut explizit GitHub).
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]


def _imports(src_dir: Path) -> list[tuple[Path, str]]:
    out = []
    for py in src_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append((py, node.module))
            elif isinstance(node, ast.Import):
                out.extend((py, alias.name) for alias in node.names)
    return out


def test_core_has_no_upward_imports() -> None:
    bad = [
        (p.name, m)
        for p, m in _imports(PACKAGES / "forge-core" / "src")
        if m.split(".")[0] in {"forge_execute", "forge_cli", "forge_adapters"}
    ]
    assert bad == []


def test_execute_has_no_cli_or_adapter_imports() -> None:
    bad = [
        (p.name, m)
        for p, m in _imports(PACKAGES / "forge-execute" / "src")
        if m.split(".")[0] in {"forge_cli", "forge_adapters"}
    ]
    assert bad == []


_NEUTRAL = {"forge_adapters", "forge_adapters.base", "forge_adapters.text",
            "forge_adapters.registry", "forge_adapters.fake"}


def test_cli_only_uses_provider_neutral_adapter_modules() -> None:
    bad = [
        (p.name, m)
        for p, m in _imports(PACKAGES / "forge-cli" / "src")
        if m.startswith("forge_adapters")
        and m not in _NEUTRAL
        and not (p.name == "review_pr.py" and m == "forge_adapters.github")
    ]
    assert bad == []
