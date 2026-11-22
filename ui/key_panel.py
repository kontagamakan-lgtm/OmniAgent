"""Tableau des clés du panneau « Clés & LLM ».

Les lignes sont créées une fois par identifiant de clé puis mises à jour en
place. Un rafraîchissement périodique ne détruit donc pas le bouton sous le
curseur : un clic sur « Supprimer » ou « Réactiver » ne peut pas être perdu
entre l'appui et le relâchement.
"""

from __future__ import annotations

from typing import Any, Callable
from ui.layout import SPACE_SM, SPACE_XS

STATUS_COLORS = {
    "ACTIVE": "#16A34A",
    "COOLDOWN": "#D97706",
    "EXHAUSTED": "#DC2626",
    "DEAD": "#7F1D1D",
}

STATUS_LABELS = {
    "ACTIVE": "active",
    "COOLDOWN": "en pause",
    "EXHAUSTED": "épuisée",
    "DEAD": "invalide",
}


def format_key_detail(row: dict[str, Any]) -> str:
    """Texte d'une ligne, recalculé sans reconstruire les widgets."""
    detail = (
        f"{row['provider_label']} · {row['label']} · {row['hint']} · "
        f"{STATUS_LABELS.get(row['status'], row['status'])}"
    )
    remaining = row.get("remaining_s") or 0
    if row["status"] == "COOLDOWN" and remaining:
        detail += f" {remaining} s"
    elif row["status"] == "EXHAUSTED" and remaining:
        seconds = int(remaining)
        detail += (
            f" {seconds // 3600} h"
            if seconds >= 3600
            else f" {max(1, seconds // 60)} min"
        )
    detail += (
        f" · {row.get('tokens', 0)} tokens · "
        f"{float(row.get('latency_ms') or 0):.0f} ms"
    )
    return detail


class KeyTable:
    """Lignes persistantes du panneau des clés, mises à jour en place."""

    def __init__(
        self,
        parent: Any,
        ctk_module: Any,
        *,
        on_reset: Callable[[str], None],
        on_remove: Callable[[str], None],
    ) -> None:
        self.parent = parent
        self.ctk = ctk_module
        self.on_reset = on_reset
        self.on_remove = on_remove
        self.rows: dict[str, dict[str, Any]] = {}
        self._empty: Any | None = None
        self._empty_visible = False

    def render(self, rows: list[dict[str, Any]]) -> None:
        """Synchronise les widgets avec l'état courant, sans tout recréer."""
        wanted = {str(row["id"]): row for row in rows}
        for key_id in list(self.rows):
            if key_id not in wanted:
                self.rows.pop(key_id)["line"].destroy()

        if not wanted:
            self._show_empty()
            return
        self._hide_empty()
        for key_id, row in wanted.items():
            self._upsert(key_id, row)

    def render_state(self, text: str) -> None:
        """Affiche un état (chargement, vide, échec) à la place des lignes.

        Une lecture de clés qui échoue ne doit pas ressembler à une absence de
        clés : l'appelant décide de la phrase, la table se contente de la rendre.
        """
        for entry in self.rows.values():
            entry["line"].destroy()
        self.rows.clear()
        self._show_empty()
        if self._empty is not None:
            self._empty.configure(text=text)

    def _show_empty(self) -> None:
        if self._empty is None:
            self._empty = self.ctk.CTkLabel(
                self.parent,
                text="Aucune clé : Ollama local sert de secours.",
                anchor="w",
            )
        if not self._empty_visible:
            self._empty.pack(fill="x", padx=SPACE_SM, pady=SPACE_SM)
            self._empty_visible = True

    def _hide_empty(self) -> None:
        if self._empty is not None and self._empty_visible:
            self._empty.pack_forget()
            self._empty_visible = False

    def _upsert(self, key_id: str, row: dict[str, Any]) -> None:
        entry = self.rows.get(key_id)
        if entry is None:
            line = self.ctk.CTkFrame(self.parent)
            line.pack(fill="x", pady=SPACE_XS)
            line.grid_columnconfigure(1, weight=1)
            dot = self.ctk.CTkLabel(line, text="●", width=18)
            dot.grid(row=0, column=0, padx=(SPACE_SM, SPACE_XS), pady=SPACE_XS)
            detail = self.ctk.CTkLabel(line, text="", anchor="w", justify="left")
            detail.grid(row=0, column=1, sticky="ew", padx=SPACE_XS, pady=SPACE_XS)
            reset = self.ctk.CTkButton(
                line,
                text="Réactiver",
                width=90,
                fg_color="#1D4ED8",
                hover_color="#1E40AF",
                command=lambda key=key_id: self.on_reset(key),
            )
            reset.grid(row=0, column=2, padx=SPACE_SM, pady=SPACE_XS)
            remove = self.ctk.CTkButton(
                line,
                text="Supprimer",
                width=90,
                fg_color="#7F1D1D",
                hover_color="#991B1B",
                command=lambda key=key_id: self.on_remove(key),
            )
            remove.grid(row=0, column=3, padx=SPACE_SM, pady=SPACE_XS)
            entry = {
                "line": line,
                "dot": dot,
                "detail": detail,
                "reset": reset,
                "remove": remove,
            }
            self.rows[key_id] = entry

        entry["dot"].configure(
            text_color=STATUS_COLORS.get(row["status"], "#6B7280")
        )
        entry["detail"].configure(text=format_key_detail(row))
        if row["status"] == "ACTIVE":
            entry["reset"].grid_remove()
        else:
            entry["reset"].grid()
