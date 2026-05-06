"""Tests für forge_execute.capabilities."""

from __future__ import annotations

from forge_core.spec import ProjectSpec
from forge_execute.capabilities import Capabilities


def _spec(overrides: dict | None = None) -> ProjectSpec:
    base = {
        "spec_version": "1.0",
        "name": "test",
        "cost_caps": {
            "per_generation_usd": "0.5",
            "per_run_usd": "5",
            "per_project_per_day_usd": "30",
            "per_project_per_month_usd": "500",
        },
        "surfaces": {
            "backend_logic": {
                "paths": ["backend/src/services/", "backend/src/agents/tools/"],
                "type": "code",
            },
            "agent_prompts": {
                "paths": ["backend/agents/maler.yaml"],
                "type": "yaml-keys",
                "allowed_keys": ["system_prompt", "tools"],
            },
        },
        "forbidden": [
            "backend/src/routes/auth.py",
            "backend/src/routes/payments.py",
            "backend/migrations/**",
            ".forge/**",
            "CLAUDE.md",
        ],
        "capabilities": {
            "read": ["**/*"],
            "edit": ["{surfaces}"],
            "run": ["pytest *", "ruff *", "npm test", "npm run lint*"],
            "network_egress": ["api.anthropic.com", "api.github.com"],
        },
    }
    if overrides:
        base.update(overrides)
    return ProjectSpec.model_validate(base)


def test_edit_in_surface_allowed() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("backend/src/services/quote_calculator.py")
    assert r.allowed is True


def test_edit_outside_surface_denied() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("frontend/src/App.tsx")
    assert r.allowed is False
    assert r.guardrail_id == "surface_violation"


def test_edit_forbidden_path_denied() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("backend/src/routes/auth.py")
    assert r.allowed is False
    assert r.guardrail_id == "forbidden_path"


def test_edit_forbidden_glob_denied() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("backend/migrations/0042_add_user_role.py")
    assert r.allowed is False
    assert r.guardrail_id == "forbidden_path"


def test_edit_yaml_keys_surface_allowed() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("backend/agents/maler.yaml")
    assert r.allowed is True


def test_edit_claude_md_denied() -> None:
    """CLAUDE.md ist forbidden — selbst wenn es in einem Surface läge.

    Aus todos.txt Q4: forge optimiert nicht ihre eigene Konfiguration.
    """
    caps = Capabilities(_spec())
    r = caps.check_edit("CLAUDE.md")
    assert r.allowed is False
    assert r.guardrail_id == "forbidden_path"


def test_edit_dotforge_denied() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit(".forge/project.yaml")
    assert r.allowed is False


def test_read_allowed_for_anything_except_forbidden() -> None:
    caps = Capabilities(_spec())
    assert caps.check_read("backend/src/services/foo.py").allowed is True
    assert caps.check_read("frontend/src/App.tsx").allowed is True
    assert caps.check_read("backend/src/routes/auth.py").allowed is False


def test_run_allowed_command() -> None:
    caps = Capabilities(_spec())
    assert caps.check_run("pytest -x tests/").allowed is True
    assert caps.check_run("ruff check src/").allowed is True
    assert caps.check_run("npm test").allowed is True


def test_run_denied_command() -> None:
    caps = Capabilities(_spec())
    r = caps.check_run("git push origin main")
    assert r.allowed is False
    assert r.guardrail_id == "capability_denied"

    r2 = caps.check_run("rm -rf /")
    assert r2.allowed is False


def test_action_commit_open_pr_allowed_by_default() -> None:
    caps = Capabilities(_spec())
    assert caps.check_action("commit").allowed is True
    assert caps.check_action("open_pr").allowed is True


def test_merge_pr_categorically_disabled() -> None:
    caps = Capabilities(_spec())
    r = caps.check_action("merge_pr")
    assert r.allowed is False
    assert "categorically" in (r.detail or "")


def test_push_to_main_categorically_disabled() -> None:
    caps = Capabilities(_spec())
    assert caps.check_action("push_to_main").allowed is False


def test_push_force_categorically_disabled() -> None:
    caps = Capabilities(_spec())
    assert caps.check_action("push_force").allowed is False


def test_egress_allowed_host() -> None:
    caps = Capabilities(_spec())
    assert caps.check_egress("api.anthropic.com").allowed is True


def test_egress_denied_host() -> None:
    caps = Capabilities(_spec())
    r = caps.check_egress("evil.example.com")
    assert r.allowed is False
    assert r.guardrail_id == "egress_denied"


def test_to_violation_payload() -> None:
    caps = Capabilities(_spec())
    r = caps.check_edit("backend/src/routes/auth.py")
    payload = r.to_violation("write backend/src/routes/auth.py")
    assert payload.guardrail_id == "forbidden_path"
    assert payload.blocked is True
    assert "auth.py" in payload.attempted_action


def test_allowed_tools_string_basic() -> None:
    caps = Capabilities(_spec())
    s = caps.allowed_tools_string()
    assert "Read" in s
    assert "Edit" in s
    assert "Bash(pytest:*)" in s
    assert "Bash(ruff:*)" in s
    assert "Bash(npm test:*)" in s


def test_allowed_tools_no_run_capabilities() -> None:
    spec = ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "test",
            "cost_caps": {
                "per_generation_usd": "0.5",
                "per_run_usd": "5",
                "per_project_per_day_usd": "30",
                "per_project_per_month_usd": "500",
            },
            "capabilities": {"run": []},
        }
    )
    caps = Capabilities(spec)
    s = caps.allowed_tools_string()
    assert "Bash(" not in s
