"""GitHub-Side-Effects der Triage-Phase: Kommentar + Close.

Getrennt vom LLM-Aufruf, damit der board-loop sie unabhängig
schalten kann (Spec ``triage.auto_comment`` / ``triage.auto_close``)
und damit Tests die Klassifikation ohne ``gh``-Mock testen können.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


class GHTriageError(RuntimeError):
    """``gh``-Aufruf für Kommentar/Close ist fehlgeschlagen."""


def comment_issue(
    *,
    repo: Path,
    issue_number: int,
    body: str,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> None:
    """Postet ``body`` als Kommentar an Issue ``#issue_number``.

    Best-effort: bei Fehlern wird ``GHTriageError`` geworfen; der
    board-loop fängt das und macht weiter — ein fehlgeschlagener
    Kommentar darf den Triage-Skip nicht in einen Run umdrehen.
    """
    result = run_subprocess(
        [gh_bin, "issue", "comment", str(issue_number), "--body", body],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GHTriageError(
            f"gh issue comment #{issue_number} failed (exit "
            f"{result.returncode}): {result.stderr.strip() or '<no stderr>'}"
        )


def close_issue(
    *,
    repo: Path,
    issue_number: int,
    reason: str = "not planned",
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> None:
    """Schließt Issue ``#issue_number`` mit ``--reason``.

    ``reason`` ist ``not planned`` für ``stale`` / ``duplicate``
    und ``completed`` für ``already_solved``. Das macht den
    Operator-View im GitHub-UI lesbarer; gh kennt nur diese zwei
    String-Werte.
    """
    result = run_subprocess(
        [
            gh_bin, "issue", "close", str(issue_number),
            "--reason", reason,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GHTriageError(
            f"gh issue close #{issue_number} failed (exit "
            f"{result.returncode}): {result.stderr.strip() or '<no stderr>'}"
        )
