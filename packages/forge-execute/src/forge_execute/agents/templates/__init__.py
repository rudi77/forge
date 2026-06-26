"""Subagent-Markdown-Templates.

Werden vom ClaudeCodeCLIAgent in den Worktree kopiert (nach .claude/agents/),
damit Claude Code sie als verfügbare Subagents erkennt.
"""

import re
from importlib.resources import files
from pathlib import Path

# Subagent-Rollen, die forge als Arbeitspferde kennt. Reihenfolge ist die
# kanonische Pipeline-Ordnung (architect plant, developer baut, tester prüft,
# simplify räumt auf, reviewer liest am Ende kritisch gegen).
#
# `simplify` ist ein Sonderfall: KEIN Task-Subagent (kein `.md`-Template), sondern
# die built-in `/simplify`-Skill der Claude Code CLI, die der Master direkt über das
# `Skill`-Tool aufruft. Steht trotzdem in KNOWN_AGENTS, damit der Operator sie wie
# jede andere Rolle ins Roster (`--agents`, `agents:[...]`) nimmt und der Master sie
# als „mitgewirkt" zurückmelden kann (`extract_agents_from_master_output`).
KNOWN_AGENTS: tuple[str, ...] = (
    "architect",
    "developer",
    "tester",
    "simplify",
    "reviewer",
)

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
    has_simplify = "simplify" in roster
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
    if has_simplify:
        descriptions.append(
            "- **/simplify** (built-in skill, invoked via the **Skill** tool — "
            "NOT a Task subagent): a finishing cleanup pass over the cumulative "
            "diff (reuse of existing helpers, simplification, efficiency, right "
            "level of abstraction). It applies the cleanups itself. See the "
            "workflow for when to run it."
        )
    if has_reviewer:
        descriptions.append(
            "- **reviewer** (read-only): critically reviews the cumulative "
            "diff for correctness, design adherence, security and test "
            "quality. Reports BLOCKING and non-blocking findings. Invoke LAST, "
            "once the implementation is complete"
            + (" and the tester reports green" if has_tester else "")
            + (" and /simplify has run" if has_simplify else "")
            + "."
        )

    # --- Workflow-Schritte (konditional) ----------------------------
    steps: list[str] = [
        "1. Read the user's task. If the prompt contains "
        "`## forge project memory`, treat it as authoritative orientation "
        "for stable project layout, patterns, and prior plans — do not "
        "re-read the whole codebase to rediscover those facts. Still read "
        "task-specific acceptance criteria and Spec sections named in the "
        "task. If no project memory block is present, read `CLAUDE.md` + "
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

    sub_lines = [
        f"{n}. Work the subtasks in {subtask_src}. Subtasks that touch "
        "DISJOINT files and have no data dependency on each other may run in "
        "PARALLEL: issue several **developer** Task calls in a SINGLE message "
        "and let them run concurrently. Subtasks that share files or depend on "
        "a prior subtask's output run in dependency order. For each subtask (or "
        "each parallel batch):"
    ]
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
        + " (one developer per subtask; batch independent ones in one message)."
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

    if has_simplify:
        steps.append(
            f"{n}. Invoke the built-in **/simplify** skill via the Skill tool "
            "on the cumulative diff to apply reuse / simplification / "
            "efficiency / right-altitude cleanups. The skill edits the worktree "
            "itself — do not hand-edit."
            + (
                " Then call the **tester** again to confirm the suite is still "
                "green; if a cleanup broke a test, hand the failure to the "
                "**developer** to fix (two rounds max), then re-verify."
                if has_tester
                else ""
            )
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
            f"{AGENTS_END_MARKER}\n\n"
            f"{LESSONS_BEGIN_MARKER}\n"
            "<0-5 durable lessons a FUTURE run on this repo should know, one "
            "per line. Each line: an optional `[pattern|pitfall|convention|"
            "tooling]` tag, then one sentence. Optionally end with "
            "`(files: a.py, b.py)`. Capture only non-obvious, lasting facts "
            "(a project convention, a pitfall that cost you time, a reusable "
            "pattern) — NOT task-specific status or anything already obvious "
            "from the code. Omit the lines entirely if you learned nothing "
            "worth persisting.>\n"
            f"{LESSONS_END_MARKER}\n"
            "```\n\n"
            f"   The `{PLAN_BEGIN_MARKER}` / `{PLAN_END_MARKER}` and "
            f"`{AGENTS_BEGIN_MARKER}` / `{AGENTS_END_MARKER}` markers are "
            "MANDATORY and must appear on their own lines. The "
            f"`{LESSONS_BEGIN_MARKER}` / `{LESSONS_END_MARKER}` block is "
            "optional — include it only when you have a real lesson."
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
            f"{AGENTS_END_MARKER}\n\n"
            f"{LESSONS_BEGIN_MARKER}\n"
            "<optional: 0-5 durable lessons a future run should know, one per "
            "line, each an optional `[pattern|pitfall|convention|tooling]` tag "
            "then one sentence. Only non-obvious, lasting facts — omit if "
            "none.>\n"
            f"{LESSONS_END_MARKER}\n"
            "```"
        )

    # --- Kontext- & Kosten-Disziplin --------------------------------
    # Adressiert die zwei gemessenen Kostentreiber orchestrierter Runs:
    # Subagents starten kalt und re-scannen Files, die der Master schon
    # kennt (Cache-Read-Explosion), und interaktive Fragen verbrennen im
    # Headless-Mode einen vollen API-Round-Trip.
    disciplines: list[str] = [
        "- **Context handoff:** subagents start with an EMPTY context — "
        "they see only what you put into the Task prompt. When dispatching "
        "any subagent, paste in: the relevant `## forge project memory` "
        "excerpt, the exact file paths and spec sections already "
        "identified"
        + (
            ", and (for the developer) the full subtask block from the plan"
            if has_architect
            else ""
        )
        + ". A subagent that must rediscover files you already know wastes "
        "a cold re-scan of the repo.",
        "- **Targeted verification:** while working a subtask, run only the "
        "tests/builds that subtask touches (single test file, filtered run, "
        "incremental build — e.g. skip a rebuild when nothing compiled "
        "changed). The FULL suite runs exactly once, at the final "
        "verification step.",
        "- **Parallel safety:** all subagents share ONE worktree. Run "
        "developers concurrently ONLY when their subtasks edit disjoint files "
        "and neither needs the other's output — otherwise serialize. Never let "
        "two developers write the same file in the same batch (lost-update "
        "race). A subtask's tester waits for that subtask's developer. When in "
        "doubt, serialize: a correct slow run beats a corrupted fast one.",
        "- **Headless run:** there is no operator to ask — interactive "
        "questions (AskUserQuestion or similar) are auto-denied and burn a "
        "full turn. Choose the spec-faithful default instead and record it"
        + (
            " (architect: under `Design decisions`, otherwise"
            if has_architect
            else ""
        )
        + " in the run summary"
        + (")" if has_architect else "")
        + ".",
    ]

    # --- Verbote (konditional) --------------------------------------
    nevers = [
        "- Edit files yourself. Delegate to the developer subagent."
        + (
            " (Invoking the **/simplify** skill is the one sanctioned exception "
            "— that is a tool call that edits, not manual hand-editing.)"
            if has_simplify
            else ""
        )
    ]
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

    # `simplify` ist kein Task-Subagent — aus der Team-Zeile raushalten, damit
    # „Use them via the Task tool" stimmt. Der Skill-Schritt steht separat in den
    # descriptions + im Workflow.
    roster_str = ", ".join(r for r in roster if r != "simplify")
    return (
        "You are the lead engineer in a forge software factory. Your active "
        f"subagent team for this task is: {roster_str}. Use them via the Task "
        "tool:\n\n"
        + "\n".join(descriptions)
        + "\n\n## Your workflow\n\n"
        + "\n".join(steps)
        + "\n\n## Context & cost discipline\n\n"
        + "\n".join(disciplines)
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

# Markers used to extract curated, distilled lessons the master wants future
# runs to remember (conventions, pitfalls, patterns). Optional and fail-open:
# absent markers simply mean "no lessons this run" — forge never fabricates
# them. Each line in the block is one lesson; see `extract_lessons_block`.
LESSONS_BEGIN_MARKER = "---FORGE-LESSONS-BEGIN---"
LESSONS_END_MARKER = "---FORGE-LESSONS-END---"


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


def extract_lessons_block(text: str) -> str | None:
    """Returns the raw inner text of the ``---FORGE-LESSONS-...---`` block.

    None when the markers are absent — the common case (lessons are optional,
    forge never fabricates them). Structured parsing into individual lessons
    happens in ``forge_execute._lesson_parser.parse_lessons``; this stays a
    pure marker-slice, mirroring ``extract_plan_from_master_output``.
    """
    begin = text.find(LESSONS_BEGIN_MARKER)
    end = text.find(LESSONS_END_MARKER)
    if begin == -1 or end == -1 or end <= begin:
        return None
    block = text[begin + len(LESSONS_BEGIN_MARKER) : end].strip()
    return block or None


# Rückwärtskompatibler Default-Prompt (volles Roster). Neuer Code soll
# `build_orchestrator_prompt(agents)` mit dem konkreten Roster nutzen.
ORCHESTRATOR_SYSTEM_PROMPT = build_orchestrator_prompt(list(DEFAULT_AGENTS))
