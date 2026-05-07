"""Inter-Generation-Memory für forge.

Spec v0.3 Teil 6.8: Wenn Generation N+1 startet, splicen wir die akkumulierten
KEPT-Diffs der vorherigen Generationen als Kontext in den Prompt. Verhindert
"no_significant_change"-Wiederholungen, weil der Agent weiß, was schon
committed wurde, und auf das aufbauen soll.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Zielgröße für Diff-Snippets im Prompt. Wenn ein einzelner KEPT-Diff größer
# ist, wird er truncated — der Agent sieht dann eine Notiz, dass mehr da ist
# als gezeigt.
_MAX_DIFF_PER_GEN = 4000


@dataclass
class RunContext:
    """Sammelt KEPT-Diffs während eines Runs für die nächste Generation."""

    kept_diffs: list[tuple[int, str]] = field(default_factory=list)
    """(generation_idx, diff-text) — chronologisch geordnet."""

    def record_keep(self, *, generation_idx: int, diff: str) -> None:
        """Hinterlegt einen KEPT-Diff. Wird vom Runner nach jeder KEEP-Decision
        aufgerufen, BEVOR die nächste Generation startet."""
        if not diff or not diff.strip():
            return
        self.kept_diffs.append((generation_idx, diff))

    def has_history(self) -> bool:
        return bool(self.kept_diffs)

    def render_prompt_addendum(self) -> str:
        """Liefert einen Markdown-Block, den der Runner an den
        initial_prompt anhängt. Leer wenn keine History.

        Format ist absichtlich klar markiert, damit der Subagent versteht:
        das ist KEIN Teil des User-Auftrags, sondern Run-State.
        """
        if not self.kept_diffs:
            return ""

        parts: list[str] = [
            "",
            "---",
            "## forge run context — already committed in earlier generations",
            "",
            "Each block below is the diff of a generation that **already passed**",
            "and was committed to the run branch. Do **not** repeat or revert them.",
            "Build on top of these changes. If you don't see a meaningful next step,",
            "respond with the exact line `forge: nothing more to do` so the run stops.",
            "",
        ]
        for gen_idx, diff in self.kept_diffs:
            parts.append(f"### Generation {gen_idx} (KEPT)")
            parts.append("")
            parts.append("```diff")
            parts.append(_truncate(diff, _MAX_DIFF_PER_GEN))
            parts.append("```")
            parts.append("")
        return "\n".join(parts)


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n… (truncated {len(text) - n} chars)"


# Sentinel string — der Agent kann damit den Run beenden, wenn er nichts
# Sinnvolles mehr zu tun sieht. Spec v0.3 Teil 6.8.
NO_MORE_TO_DO_SIGNAL = "forge: nothing more to do"


def proposal_signals_done(diff_or_message: str) -> bool:
    """Prüft, ob der Agent das `nothing more to do`-Signal gesendet hat.

    Wir prüfen großzügig: case-insensitive, ignoriert führende/trailing
    whitespace und Markdown-Formatting (z.B. wenn der Agent `**forge:
    nothing more to do**` schreibt).
    """
    if not diff_or_message:
        return False
    normalized = diff_or_message.lower()
    # Markdown-Sterne und Backticks entfernen
    for char in ("*", "`", "_"):
        normalized = normalized.replace(char, "")
    return NO_MORE_TO_DO_SIGNAL.lower() in normalized
