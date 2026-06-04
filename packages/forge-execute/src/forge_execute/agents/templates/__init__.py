"""Subagent-Markdown-Templates.

Werden vom ClaudeCodeCLIAgent in den Worktree kopiert (nach .claude/agents/),
damit Claude Code sie als verfügbare Subagents erkennt.
"""

import re
from importlib.resources import files
from pathlib import Path

# Subagent-Rollen, die forge als Arbeitspferde kennt. Reihenfolge ist die
# kanonische Pipeline-Ordnung (architect plant, developer baut, tester prüft,
# reviewer liest am Ende kritisch gegen).
KNOWN_AGENTS: tuple[str, ...] = ("architect", "developer", "tester", "reviewer")

# Default-Roster, wenn der Operator nichts anderes konfiguriert. Der reviewer
# ist bewusst NICHT im Default — er ist opt-in pro Roster (kostet einen
# zusätzlichen read-only Subagent-Pass).
DEFAULT_AGENTS: tuple[str, ...] = ("architect", "developer", "tester")


def templates_dir() -> Path:
    """Liefert den Pfad zum Templates-Verzeichnis."""
    return Path(str(files("forge_execute.agents.templates")))


def list_templates(agents: list[str] | None = None) -> list[Path]:
    """`.md`-Templates im Verzeichnis (sortiert).

    `agents=None` liefert ALLE Templates (Rückwärtskompatibilität). Wird eine
    Roster-Liste übergeben, werden nur die Templates der enthaltenen Rollen
    zurückgegeben — so installiert forge nur die Arbeitspferde, die der Run
    laut Spec-Config tatsächlich nutzt.
    """
    all_templates = sorted(templates_dir().glob("*.md"))
    if agents is None:
        return all_templates
    wanted = {a.strip().lower() for a in agents}
    return [p for p in all_templates if p.stem.lower() in wanted]


def normalize_agents(agents: list[str] | None) -> list[str]:
    """Normalisiert eine Roster-Liste auf bekannte Rollen in Pipeline-Ordnung.

    Unbekannte/leere Einträge werden verworfen. `None` oder leer → Default.
    Duplikate werden zusammengefasst. Die Ausgabe-Reihenfolge folgt
    `KNOWN_AGENTS`, damit der Orchestrator-Prompt deterministisch ist.
    """
    if not agents:
        return list(DEFAULT_AGENTS)
    present = {a.strip().lower() for a in agents if a and a.strip()}
    ordered = [a for a in KNOWN_AGENTS if a in present]
    return ordered or list(DEFAULT_AGENTS)


def unknown_agents(agents: list[str] | None) -> list[str]:
    """Liefert die Roster-Einträge, die forge NICHT ausführen kann.

    Ein Eintrag ist „unbekannt", wenn sein Name (case-insensitive) nicht in
    `KNOWN_AGENTS` steht — also keine Rolle, für die ein Subagent-Template
    existiert. `normalize_agents` verwirft solche Einträge **still**; diese
    Funktion macht sie sichtbar, damit die CLI eine Warnung ausgeben kann
    (Tippfehler in `--agents`, oder eine in der Spec reservierte aber noch
    nicht implementierte Rolle wie `operations`). Reihenfolge = Input.
    """
    if not agents:
        return []
    known = set(KNOWN_AGENTS)
    return [
        a.strip()
        for a in agents
        if a and a.strip() and a.strip().lower() not in known
    ]


def roster_needs_orchestration(agents: list[str]) -> bool:
    """True, wenn der Roster echte Mehr-Agenten-Orchestrierung braucht.

    Ein einsamer ``developer`` braucht keinen Orchestrator (= klassischer
    Single-Agent-Run): kein Plan, kein Task-Tool, keine Subagent-Installation.
    Sobald architect oder tester mitwirken, orchestriert der Master.
    """
    return bool(set(agents) - {"developer"})


def build_orchestrator_prompt(agents: list[str]) -> str:
    """Baut den `--append-system-prompt` aus dem aktivierten Roster.

    Nur die Rollen aus `agents` werden beschrieben und in den Workflow
    eingewoben. Ohne ``architect`` entfällt der Plan-Schritt (und die
    ``---FORGE-PLAN-...---``-Marker); ohne ``tester`` entfallen die
    Verifikations-Schritte.
    """
    roster = normalize_agents(agents)
    has_architect = "architect" in roster
    has_tester = "tester" in roster
    has_reviewer = "reviewer" in roster

    # --- Rollenbeschreibungen (nur aktivierte) ----------------------
    descriptions: list[str] = []
    if has_architect:
        descriptions.append(
            "- **architect** (read-only): produces a structured plan in "
            "Markdown given a task description. Always invoke FIRST for any "
            "non-trivial task."
        )
    descriptions.append(
        "- **developer**: implements exactly ONE subtask"
        + (" from the architect's plan" if has_architect else "")
        + ". Invoke once per subtask, in order."
    )
    if has_tester:
        descriptions.append(
            "- **tester**: writes failing tests OR runs the test suite to "
            "verify a subtask's acceptance criteria. Invoke before AND after "
            "a developer run when the task requires new tests."
        )
    if has_reviewer:
        descriptions.append(
            "- **reviewer** (read-only): critically reviews the cumulative "
            "diff for correctness, design adherence, security and test "
            "quality. Reports BLOCKING and non-blocking findings. Invoke LAST, "
            "once the implementation is complete"
            + (" and the tester reports green" if has_tester else "")
            + "."
        )

    # --- Workflow-Schritte (konditional) ----------------------------
    steps: list[str] = [
        "1. Read the user's task and the project's `CLAUDE.md` + "
        "`.forge/project.yaml`."
    ]
    n = 2
    if has_architect:
        steps.append(
            f"{n}. Call the **architect** subagent with the task. Read its "
            "plan output."
        )
        n += 1
        steps.append(
            f"{n}. If the plan reports \"Insufficient context\", relay that to "
            "the user and stop."
        )
        n += 1
        subtask_src = "the plan"
    else:
        subtask_src = "the task"

    sub_lines = [f"{n}. For each subtask in {subtask_src}, in order:"]
    letter = ord("a")
    if has_tester:
        sub_lines.append(
            f"   {chr(letter)}. If the subtask requires a test, call the "
            "**tester** subagent first."
        )
        letter += 1
    sub_lines.append(
        f"   {chr(letter)}. Call the **developer** subagent"
        + (" with the subtask number" if has_architect else "")
        + "."
    )
    letter += 1
    if has_architect:
        sub_lines.append(
            f"   {chr(letter)}. If the developer reports \"Plan needs "
            "revision\", call the architect again with the new context and "
            "update the plan."
        )
    steps.append("\n".join(sub_lines))
    n += 1

    if has_tester:
        steps.append(
            f"{n}. After all subtasks, call the **tester** one more time to "
            "run the full verification suite."
        )
        n += 1

    if has_reviewer:
        steps.append(
            f"{n}. Call the **reviewer** subagent with the cumulative diff "
            "and the acceptance criteria. If it returns BLOCKING findings, "
            "hand them back to the developer to fix (two rounds max), then "
            "re-run the reviewer"
            + (" and the tester" if has_tester else "")
            + ". Carry any non-blocking findings into the run summary."
        )
        n += 1

    # --- Abschluss-Output -------------------------------------------
    if has_architect:
        steps.append(
            f"{n}. Stop. Output a final short summary AND the verbatim plan, "
            "formatted exactly as below (forge parses this — do not reformat "
            "or omit the markers):\n\n"
            "```\n"
            f"{PLAN_BEGIN_MARKER}\n"
            "<the architect's plan markdown. You MAY prepend a status "
            "checkbox to each subtask line — `[x]` done, `[ ]` not done, "
            "`[!]` failed — but change nothing else.>\n"
            f"{PLAN_END_MARKER}\n\n"
            "## Run summary\n"
            "<2-4 bullets: which subtasks done, anything still open, any "
            "caveats>\n\n"
            f"{AGENTS_BEGIN_MARKER}\n"
            "<comma-separated list of the subagent roles you ACTUALLY "
            "invoked this run, e.g. `architect, developer, tester`>\n"
            f"{AGENTS_END_MARKER}\n"
            "```\n\n"
            f"   The `{PLAN_BEGIN_MARKER}` / `{PLAN_END_MARKER}` and "
            f"`{AGENTS_BEGIN_MARKER}` / `{AGENTS_END_MARKER}` markers are "
            "MANDATORY and must appear on their own lines."
        )
    else:
        steps.append(
            f"{n}. Stop. Output a short summary of which subtask(s) you "
            "implemented and the verification result, then the roles you "
            "ACTUALLY invoked, formatted exactly as below (keep the markers "
            "on their own lines — forge parses this):\n\n"
            "```\n"
            f"{AGENTS_BEGIN_MARKER}\n"
            "<comma-separated roles you invoked, e.g. `developer, tester`>\n"
            f"{AGENTS_END_MARKER}\n"
            "```"
        )

    # --- Verbote (konditional) --------------------------------------
    nevers = ["- Edit files yourself. Delegate to the developer subagent."]
    if has_architect:
        nevers.append(
            "- Skip the architect step, even for tasks that look simple. The "
            "plan is proof that you understood the constraints."
        )
    if has_tester:
        nevers.append(
            "- Continue past a failed verification. If the tester reports "
            "red, hand back to the developer with the failure detail and let "
            "them fix it. Two retries max — then stop and report."
        )
    nevers.append(
        "- Touch any file matching a pattern in `.forge/project.yaml` "
        "`forbidden` list."
    )
    nevers.append(
        f"- Omit the `{AGENTS_BEGIN_MARKER}` / `{AGENTS_END_MARKER}` block, or "
        "list a role you did not actually invoke. forge records it as the "
        "real participating roster."
    )
    if has_reviewer:
        nevers.append(
            "- Finalize while the reviewer has unresolved BLOCKING findings. "
            "Non-blocking findings may ship — note them in the run summary."
        )
    if has_architect:
        nevers.append(
            f"- Omit the `{PLAN_BEGIN_MARKER}` / `{PLAN_END_MARKER}` markers. "
            "forge needs them to persist the plan as a first-class artefact."
        )

    roster_str = ", ".join(roster)
    return (
        "You are the lead engineer in a forge software factory. Your active "
        f"subagent team for this task is: {roster_str}. Use them via the Task "
        "tool:\n\n"
        + "\n".join(descriptions)
        + "\n\n## Your workflow\n\n"
        + "\n".join(steps)
        + "\n\n## What you NEVER do\n\n"
        + "\n".join(nevers)
        + "\n"
    )


# Markers used by ClaudeCodeCLIAgent to extract the architect's plan from
# the master claude's final result text. See ORCHESTRATOR_SYSTEM_PROMPT.
PLAN_BEGIN_MARKER = "---FORGE-PLAN-BEGIN---"
PLAN_END_MARKER = "---FORGE-PLAN-END---"

# Markers used to extract WHICH subagent roles the master actually invoked,
# as opposed to the configured roster. Self-reported by the master (same
# trust model as the plan checkboxes) — best-effort, falls back to the
# configured roster when absent. Makes orchestration fidelity measurable
# (Mantra 1: nur Messbares lässt sich optimieren).
AGENTS_BEGIN_MARKER = "---FORGE-AGENTS-BEGIN---"
AGENTS_END_MARKER = "---FORGE-AGENTS-END---"


def extract_plan_from_master_output(text: str) -> str | None:
    """Extracts the architect's plan from the master claude's final output.

    Returns None when the markers are absent (master ignored instructions, or
    single-agent run with no plan).
    """
    begin = text.find(PLAN_BEGIN_MARKER)
    end = text.find(PLAN_END_MARKER)
    if begin == -1 or end == -1 or end <= begin:
        return None
    plan = text[begin + len(PLAN_BEGIN_MARKER) : end].strip()
    return plan or None


def extract_agents_from_master_output(text: str) -> list[str] | None:
    """Extracts the subagent roles the master reported it ACTUALLY invoked.

    Reads the `---FORGE-AGENTS-...---` block, keeps only known roles and
    returns them in `KNOWN_AGENTS` order. Returns None when the markers are
    absent or no known role survived — the caller then falls back to the
    configured roster. Defensiv: läuft nur auf Subagent-Output, kein eval.
    """
    begin = text.find(AGENTS_BEGIN_MARKER)
    end = text.find(AGENTS_END_MARKER)
    if begin == -1 or end == -1 or end <= begin:
        return None
    blob = text[begin + len(AGENTS_BEGIN_MARKER) : end]
    tokens = {t.strip().lower() for t in re.split(r"[,\s]+", blob) if t.strip()}
    ordered = [a for a in KNOWN_AGENTS if a in tokens]
    return ordered or None


# Rückwärtskompatibler Default-Prompt (volles Roster). Neuer Code soll
# `build_orchestrator_prompt(agents)` mit dem konkreten Roster nutzen.
ORCHESTRATOR_SYSTEM_PROMPT = build_orchestrator_prompt(list(DEFAULT_AGENTS))
