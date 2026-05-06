"""Mutator-Implementierungen.

In v1 nur `code` aktiv (siehe Spec Teil 6.2). `yaml-keys` und `text` sind
in der Spec definiert, aber für die ersten 100 Runs nicht nötig — werden
in M2 nachgezogen, wenn die ersten Daten zeigen, dass agent_prompts-Surfaces
mehr als nur Hand-Edits brauchen.
"""

from forge_execute.mutators.code import CodeMutator, MutationResult

__all__ = ["CodeMutator", "MutationResult"]
