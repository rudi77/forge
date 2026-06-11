"""`forge watch` — Live-Tracking eines laufenden Runs (read-only).

Die Propose-Phase eines Runs ist konstruktionsbedingt stumm: der Master-
``claude``-Prozess wird mit ``--output-format json`` + ``communicate()``
gepuffert, zwischen ``ProposalRequested`` und ``ProposalReceived`` wird kein
Event emittiert. Für den Operator sieht das aus wie ein Hänger.

``forge watch`` füllt diese Lücke **ohne** Eingriff in den propose-Pfad
(Mantra 3 bleibt intakt). Das primäre Live-Signal ist der **Worktree-Poll**
(git-Status + File-mtimes unter ``.forge/worktrees/<run_id>/``) — völlig
lock-frei. Die Event-Chronik aus DuckDB ist nur **opportunistisch** verfügbar:
solange ein forge-Prozess die ``events.duckdb`` schreibt, kann ein zweiter
Prozess sie nicht öffnen (DuckDB-Single-Writer-Limit, siehe store.py-Docstring)
— der Watcher degradiert dann sauber auf „Worktree-only".

Kern ist in pure Funktionen (``discover_active_run``, ``collect_worktree_state``,
``render_watch_view``, ``_watch_loop``) getrennt, damit ohne echten
``time.sleep``/Live-Prozess testbar — analog zu ``heartbeat.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from forge_core.events import Event, EventKind
from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from forge_cli.runtime import ContextError, ForgeContext, console, err_console, load_context

# Wie viele Events das Chronik-Panel maximal zeigt (jüngste unten).
_MAX_EVENT_ROWS = 14
# Wie viele geänderte Dateien das Worktree-Panel maximal listet.
_MAX_CHANGED_FILES = 8
# Wie viele Agent-Aktivitäten das Stream-Panel maximal zeigt (jüngste unten).
_MAX_ACTIVITY_ROWS = 16
# Tail-Fenster beim Lesen des Stream-Logs — die Logs wachsen über einen
# 50-Minuten-Run auf viele MB (Thinking-Blöcke); wir brauchen nur das Ende.
_STREAM_TAIL_BYTES = 512 * 1024


@dataclass
class WorktreeState:
    """Lock-freier Schnappschuss eines Run-Worktrees."""

    run_id: str
    wt_path: Path
    exists: bool
    changed_files: list[str] = field(default_factory=list)
    changed_count: int = 0
    forge_commits: int = 0
    newest_file: str | None = None
    last_write_age_s: float | None = None
    # "active" | "idle" | "unknown"
    liveness: str = "unknown"


# --- Discovery (lock-frei aus dem Dateisystem) --------------------------


def discover_active_run(worktree_root: Path) -> str | None:
    """Entdeckt den aktiven Run ohne DB-Zugriff.

    Jeder Eintrag unter ``.forge/worktrees/`` ist ein Run-ID-ULID. Der zuletzt
    modifizierte Worktree gewinnt — das ist der gerade laufende Run. Gibt
    ``None`` zurück, wenn kein Worktree existiert.
    """
    if not worktree_root.is_dir():
        return None
    dirs = [d for d in worktree_root.iterdir() if d.is_dir()]
    if not dirs:
        return None
    newest = max(dirs, key=lambda p: _safe_mtime(p))
    return newest.name


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --- Agent-Aktivität aus dem Stream-Log (read-only) ---------------------
#
# Der ClaudeCodeCLIAgent schreibt jeden claude-Event live als Envelope
# {"ts": <UTC-ISO>, "event": <stream-json-event>} nach
# `.forge/logs/<run_id>/propose-*.jsonl`. Das ist das Signal, das die
# Propose-Blackbox öffnet: Tool-Calls, Subagent-Spawns (Task), Turns.


@dataclass
class StreamActivity:
    """Geparste Sicht auf das Stream-Log eines propose-Aufrufs."""

    log_path: Path | None = None
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    """(HH:MM:SS lokal, Art, Detail) — jüngste zuletzt."""
    turns: int = 0
    tool_calls: int = 0
    subagent_calls: int = 0
    last_event_age_s: float | None = None
    finished: bool = False


def discover_stream_log(logs_dir: Path) -> Path | None:
    """Jüngstes ``*.jsonl`` unter ``.forge/logs/<run_id>/`` — der laufende
    bzw. letzte propose-Aufruf (gen 0, gen 1, … schreiben je eine Datei)."""
    if not logs_dir.is_dir():
        return None
    files = sorted(logs_dir.glob("*.jsonl"), key=_safe_mtime)
    return files[-1] if files else None


def _tail_lines(path: Path, *, max_bytes: int = _STREAM_TAIL_BYTES) -> list[str]:
    """Letzte Zeilen einer (potenziell großen) Datei, lock-frei.

    Liest höchstens ``max_bytes`` vom Ende; eine angeschnittene erste Zeile
    wird verworfen. Fehler (Datei verschwunden, Encoding) ⇒ leere Liste.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # erste Zeile ist vermutlich angeschnitten
    return lines


_TOOL_DETAIL_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt")


def _tool_detail(name: str, tool_input: dict) -> str:
    """Kompakte Ein-Zeilen-Beschreibung eines Tool-Calls."""
    if name == "Task":
        sub = tool_input.get("subagent_type") or "?"
        desc = tool_input.get("description") or ""
        return f"{sub} — {desc}"
    for key in _TOOL_DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\n", " ⏎ ")
    return ""


def _clip(text: str, limit: int = 76) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_stream_activity(
    lines: list[str],
    *,
    now: datetime,
    log_path: Path | None = None,
    max_rows: int = _MAX_ACTIVITY_ROWS,
) -> StreamActivity:
    """Übersetzt Envelope-Zeilen in menschenlesbare Aktivitäts-Rows (rein).

    Gezeigt werden Tool-Calls (inkl. ``↳``-Präfix für Subagent-Aktivität via
    ``parent_tool_use_id``), Task-Spawns, Assistant-Text-Snippets, Fehler-
    Tool-Results und das finale result-Event. Thinking-Blöcke und Tool-Result-
    Erfolge sind bewusst ausgelassen — Signal statt Rauschen.
    """
    activity = StreamActivity(log_path=log_path)
    last_ts: datetime | None = None

    for line in lines:
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        event = envelope.get("event")
        ts_str = str(envelope.get("ts") or "")
        if not isinstance(event, dict):
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
            last_ts = ts
            local = ts.astimezone().strftime("%H:%M:%S")
        except ValueError:
            local = "--:--:--"

        etype = event.get("type")
        indent = "↳ " if event.get("parent_tool_use_id") else ""

        if etype == "system" and event.get("subtype") == "init":
            model = event.get("model") or "?"
            activity.rows.append((local, "session", f"claude gestartet — model {model}"))
        elif etype == "assistant":
            activity.turns += 1
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    activity.tool_calls += 1
                    if name == "Task":
                        activity.subagent_calls += 1
                    detail = _clip(_tool_detail(name, block.get("input") or {}))
                    activity.rows.append((local, f"{indent}{name}", detail))
                elif block.get("type") == "text":
                    text = _clip(str(block.get("text") or ""))
                    if text:
                        activity.rows.append((local, f"{indent}text", f"[dim]{text}[/dim]"))
        elif etype == "user":
            content = (event.get("message") or {}).get("content") or []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error")
                ):
                    activity.rows.append(
                        (local, f"{indent}tool✗", "[red]tool_result error[/red]")
                    )
        elif etype == "result":
            activity.finished = True
            cost = event.get("total_cost_usd")
            cost_part = f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""
            activity.rows.append(
                (
                    local,
                    "result",
                    f"{event.get('subtype')}, {event.get('num_turns')} turns{cost_part}",
                )
            )

    if last_ts is not None:
        activity.last_event_age_s = max(0.0, (now - last_ts).total_seconds())
    if len(activity.rows) > max_rows:
        activity.rows = activity.rows[-max_rows:]
    return activity


# --- Worktree-State (git + mtimes, read-only) ---------------------------


def collect_worktree_state(
    wt_path: Path,
    run_id: str,
    *,
    now: datetime,
    idle_threshold_s: float,
) -> WorktreeState:
    """Liest den Worktree-Zustand read-only: git-Status, forge-Commits, mtimes.

    Das Liveness-Verdikt vergleicht das Alter des jüngsten Schreibvorgangs mit
    ``idle_threshold_s``: jünger → ``active`` (forge arbeitet sichtbar), älter →
    ``idle`` (möglicher Hänger oder lange claude-/eval-Phase ohne Datei-IO),
    keine Datei → ``unknown``.
    """
    if not wt_path.is_dir():
        return WorktreeState(run_id=run_id, wt_path=wt_path, exists=False)

    changed = _git_status_paths(wt_path)
    forge_commits = _count_forge_commits(wt_path)
    newest_mtime, newest_path = _newest_file_mtime(wt_path)

    age: float | None = None
    liveness = "unknown"
    if newest_mtime is not None:
        age = max(0.0, now.timestamp() - newest_mtime)
        liveness = "active" if age < idle_threshold_s else "idle"

    return WorktreeState(
        run_id=run_id,
        wt_path=wt_path,
        exists=True,
        changed_files=changed[:_MAX_CHANGED_FILES],
        changed_count=len(changed),
        forge_commits=forge_commits,
        newest_file=newest_path,
        last_write_age_s=age,
        liveness=liveness,
    )


def _git_lines(wt_path: Path, *args: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(wt_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _git_status_paths(wt_path: Path) -> list[str]:
    """Pfade aus ``git status --short`` — die gerade angefassten Dateien."""
    paths: list[str] = []
    for line in _git_lines(wt_path, "status", "--short"):
        # Format: "XY <path>" (XY = 2-Zeichen-Status, dann Space, dann Pfad).
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            paths.append(path)
    return paths


def _count_forge_commits(wt_path: Path) -> int:
    """Zahl der ``forge:``-Commits = bislang gehaltene Generationen.

    Der Runner committet gehaltene Generationen mit Subject-Prefix ``forge:``
    (vgl. WorktreeManager.commit). Andere Commits (Basis-History) ignorieren wir.
    """
    subjects = _git_lines(wt_path, "log", "--format=%s", "-n", "300")
    return sum(1 for s in subjects if s.lower().startswith("forge"))


def _newest_file_mtime(wt_path: Path) -> tuple[float | None, str | None]:
    """Jüngster mtime im Worktree (exkl. ``.git``) + relativer Pfad."""
    newest: float | None = None
    newest_path: str | None = None
    for root, dirs, files in os.walk(wt_path):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in files:
            full = os.path.join(root, name)
            try:
                mtime = os.stat(full).st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
                newest_path = os.path.relpath(full, wt_path)
    return newest, newest_path


# --- Rendering (pure) ---------------------------------------------------


def render_watch_view(
    run_id: str,
    events: list[Event] | None,
    wt_state: WorktreeState,
    *,
    now: datetime,
    activity: StreamActivity | None = None,
) -> Panel:
    """Baut das kombinierte Live-Panel (rein, ohne IO)."""
    finished = _events_show_finished(events)
    border = _border_for(wt_state, finished)

    header = Table.grid(padding=(0, 2))
    header.add_row("[bold]run_id[/bold]", run_id)
    header.add_row("[bold]phase[/bold]", _phase_label(events))
    header.add_row("[bold]elapsed[/bold]", _elapsed_label(events, now))
    header.add_row("[bold]generations[/bold]", _generations_label(events, wt_state))
    header.add_row("[bold]cost[/bold]", _cost_label(events))
    if activity is not None and activity.rows:
        header.add_row("[bold]agent[/bold]", _activity_summary(activity))

    parts: list[RenderableType] = [header, _activity_block(activity)]
    parts.append(_worktree_block(wt_state))
    parts.append(_events_block(events))

    title = "forge watch"
    if finished:
        title = "forge watch — run beendet"
    return Panel(Group(*parts), title=title, border_style=border)


def _activity_summary(activity: StreamActivity) -> str:
    parts = [f"{activity.turns} turns", f"{activity.tool_calls} tool-calls"]
    if activity.subagent_calls:
        parts.append(f"{activity.subagent_calls} subagent-spawns")
    if activity.last_event_age_s is not None and not activity.finished:
        parts.append(f"letztes Event vor {format_age(activity.last_event_age_s)}")
    return " · ".join(parts)


def _activity_block(activity: StreamActivity | None) -> RenderableType:
    """Panel mit den jüngsten claude-Aktionen aus dem Stream-Log."""
    if activity is None or activity.log_path is None:
        return Panel(
            "[dim]kein Stream-Log unter .forge/logs/<run_id>/ — Run stammt von "
            "einer forge-Version ohne stream-json-Logging.[/dim]",
            title="agent-aktivität",
            border_style="dim",
        )
    if not activity.rows:
        return Panel(
            "[dim]Stream-Log existiert, aber noch keine Events — claude startet.[/dim]",
            title="agent-aktivität",
            border_style="dim",
        )

    table = Table.grid(padding=(0, 2))
    for local_ts, kind, detail in activity.rows:
        table.add_row(f"[dim]{local_ts}[/dim]", f"[bold]{kind}[/bold]", detail)
    subtitle = f"[dim]{activity.log_path.name}[/dim]"
    return Panel(table, title="agent-aktivität", subtitle=subtitle, border_style="dim")


def _border_for(wt_state: WorktreeState, finished: bool) -> str:
    if finished:
        return "cyan"
    return {"active": "green", "idle": "yellow", "unknown": "cyan"}.get(
        wt_state.liveness, "cyan"
    )


def _phase_label(events: list[Event] | None) -> str:
    if events is None:
        return "[dim]propose? — events-db gesperrt (laufender forge-Prozess)[/dim]"
    if not events:
        return "[dim]noch keine Events[/dim]"
    return events[-1].kind.value


def _elapsed_label(events: list[Event] | None, now: datetime) -> str:
    if not events:
        return "[dim]n/a[/dim]"
    started = events[0].ts
    return format_age(max(0.0, (now - started).total_seconds()))


def _generations_label(events: list[Event] | None, wt_state: WorktreeState) -> str:
    if events:
        started = sum(1 for e in events if e.kind == EventKind.GENERATION_STARTED)
        return f"{started} gestartet, {wt_state.forge_commits} gehalten"
    return f"{wt_state.forge_commits} gehalten (aus git log)"


def _cost_label(events: list[Event] | None) -> str:
    if not events:
        return "[dim]n/a[/dim]"
    total = sum((e.cost_usd for e in events), Decimal("0"))
    return f"${total:.4f}"


def _worktree_block(wt_state: WorktreeState) -> RenderableType:
    if not wt_state.exists:
        return Panel(
            "[dim]Worktree existiert nicht (mehr) — Run evtl. beendet/aufgeräumt.[/dim]",
            title="worktree",
            border_style="dim",
        )

    dot = {"active": "[green]●[/green]", "idle": "[yellow]○[/yellow]"}.get(
        wt_state.liveness, "[cyan]○[/cyan]"
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_row("[bold]status[/bold]", f"{dot} {_liveness_text(wt_state)}")
    grid.add_row(
        "[bold]geändert[/bold]",
        f"{wt_state.changed_count} Datei(en) (uncommitted)",
    )
    if wt_state.newest_file:
        grid.add_row("[bold]letzter Write[/bold]", wt_state.newest_file)

    body: list[RenderableType] = [grid]
    if wt_state.changed_files:
        files = "\n".join(f"  • {p}" for p in wt_state.changed_files)
        extra = wt_state.changed_count - len(wt_state.changed_files)
        if extra > 0:
            files += f"\n  [dim]… +{extra} weitere[/dim]"
        body.append(files)
    return Panel(Group(*body), title="worktree", border_style="dim")


def _liveness_text(wt_state: WorktreeState) -> str:
    if wt_state.liveness == "active":
        age = format_age(wt_state.last_write_age_s or 0.0)
        return f"aktiv — letzter Write vor {age}"
    if wt_state.liveness == "idle":
        age = format_age(wt_state.last_write_age_s or 0.0)
        return (
            f"keine Datei-Aktivität seit {age} — claude denkt, lange Eval-Phase, "
            "oder Hänger. Harte Grenzen (max_turns + Cost-Cap) beenden jeden Run."
        )
    return "keine Dateien im Worktree"


def _events_block(events: list[Event] | None) -> RenderableType:
    if events is None:
        return Panel(
            "[dim]events.duckdb wird vom laufenden forge-Prozess gehalten "
            "(DuckDB lässt keinen parallelen Zugriff zu). Die volle Chronik "
            "erscheint, sobald der Run/board-loop die DB freigibt.[/dim]",
            title="event-chronik",
            border_style="dim",
        )
    if not events:
        return Panel("[dim]noch keine Events[/dim]", title="event-chronik", border_style="dim")

    table = Table.grid(padding=(0, 2))
    for evt in events[-_MAX_EVENT_ROWS:]:
        local_ts = evt.ts.astimezone().strftime("%H:%M:%S")
        ok = ""
        if evt.success is True:
            ok = "[green]✓[/green]"
        elif evt.success is False:
            ok = "[red]✗[/red]"
        cost = f"${evt.cost_usd:.4f}" if evt.cost_usd and evt.cost_usd > 0 else ""
        gen = evt.generation_id[-6:] if evt.generation_id else ""
        table.add_row(f"[dim]{local_ts}[/dim]", evt.kind.value, gen, cost, ok)
    return Panel(table, title="event-chronik", border_style="dim")


def format_age(seconds: float) -> str:
    """Menschenlesbares Alter: ``42s`` / ``3m 4s`` / ``1h 2m``."""
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def _events_show_finished(events: list[Event] | None) -> bool:
    return bool(events) and any(e.kind == EventKind.RUN_FINISHED for e in events)


# --- Opportunistischer DB-Read -----------------------------------------


def _try_read_events(ctx: ForgeContext, run_id: str) -> list[Event] | None:
    """Liest die Events read-only — oder ``None``, wenn die DB gesperrt ist.

    Solange ein forge-Prozess die ``events.duckdb`` schreibend hält, schlägt das
    Öffnen fehl (DuckDB-Single-Writer-Limit). Das ist kein Fehler, sondern der
    erwartete Live-Fall: wir liefern ``None`` und der Renderer degradiert.
    """
    store = None
    try:
        store = ctx.open_store()
        return store.events_for_run(run_id)
    except Exception:
        return None
    finally:
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()


# --- Loop (testbar via injizierte Deps) ---------------------------------


def _watch_loop(
    tick: Callable[[], tuple[Panel, list[Event] | None]],
    *,
    interval_s: float,
    sleep: Callable[[float], None],
    should_stop: Callable[[], bool],
    on_render: Callable[[Panel], None],
    max_ticks: int | None = None,
    is_done: Callable[[list[Event] | None], bool] | None = None,
) -> int:
    """Generische Poll-Schleife. Gibt die Zahl der gerenderten Ticks zurück.

    Injizierte ``sleep``/``should_stop``/``max_ticks`` machen sie ohne echten
    Schlaf testbar (gleiches Muster wie ``run_heartbeat``).
    """
    ticks = 0
    while not should_stop():
        if max_ticks is not None and ticks >= max_ticks:
            break
        view, events = tick()
        on_render(view)
        ticks += 1
        if is_done is not None and is_done(events):
            break
        # Kein Schlaf nach dem letzten geplanten Tick (vgl. run_heartbeat).
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(interval_s)
    return ticks


# --- CLI-Command --------------------------------------------------------


def watch_command(
    run_id: Annotated[
        str | None,
        typer.Argument(
            help="Run-ID (ULID). Default: aktivster Worktree unter .forge/worktrees/.",
        ),
    ] = None,
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml."),
    ] = None,
    interval: Annotated[
        float,
        typer.Option("--interval", "-i", help="Poll-Intervall in Sekunden."),
    ] = 2.0,
    once: Annotated[
        bool,
        typer.Option("--once", help="Einen Snapshot rendern und beenden (non-TTY)."),
    ] = False,
) -> None:
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    worktree_root = ctx.forge_dir / "worktrees"
    resolved = run_id or discover_active_run(worktree_root)
    if not resolved:
        err_console.print(
            "[red]error[/red]: kein aktiver Run gefunden "
            f"(keine Worktrees unter {worktree_root}). Run-ID explizit angeben?"
        )
        raise typer.Exit(code=1)

    wt_path = worktree_root / resolved
    logs_dir = ctx.forge_dir / "logs" / resolved
    idle_threshold_s = max(interval * 3.0, 30.0)

    def tick() -> tuple[Panel, list[Event] | None]:
        now = datetime.now(UTC)
        wt_state = collect_worktree_state(
            wt_path, resolved, now=now, idle_threshold_s=idle_threshold_s
        )
        events = _try_read_events(ctx, resolved)
        log_path = discover_stream_log(logs_dir)
        activity: StreamActivity | None = None
        if log_path is not None:
            activity = parse_stream_activity(
                _tail_lines(log_path), now=now, log_path=log_path
            )
        return (
            render_watch_view(resolved, events, wt_state, now=now, activity=activity),
            events,
        )

    if once:
        view, _ = tick()
        console.print(view)
        return

    stop_flag = {"stop": False}

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_flag["stop"] = True

    prev_handlers: list[tuple[int, object]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Nicht im Main-Thread (z.B. Tests) → kein Signal-Handler möglich.
        with contextlib.suppress(ValueError, OSError):
            prev_handlers.append((sig, signal.signal(sig, _request_stop)))

    console.print(
        f"[bold green]forge watch[/bold green] run {resolved[:10]} "
        f"(interval {interval:.0f}s) — Ctrl-C zum Beenden."
    )
    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            _watch_loop(
                tick,
                interval_s=interval,
                sleep=time.sleep,
                should_stop=lambda: stop_flag["stop"],
                on_render=live.update,
                is_done=_events_show_finished,
            )
    finally:
        for sig, handler in prev_handlers:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)  # type: ignore[arg-type]
