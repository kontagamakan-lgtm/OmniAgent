"""Boîte d'approbation explicite, indépendante de CustomTkinter.

Trois actions seulement : autoriser une fois, autoriser pour la session ou
refuser. `Refuser` est le bouton par défaut ; fermer la fenêtre ou appuyer sur
Échap refuse également. Ce module utilise Tk standard pour rester utilisable
dans la fenêtre CustomTkinter comme dans le contrôle visuel isolé.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

__all__ = [
    "APPROVAL_CHOICES",
    "DEFAULT_DECISION",
    "ApprovalDialog",
    "ask_tool_approval",
    "decision_for",
    "dialog_title",
]

#: Libellés volontairement explicites : jamais Oui/Non, dont le sens est ambigu.
APPROVAL_CHOICES: tuple[tuple[str, str], ...] = (
    ("once", "Autoriser une fois"),
    ("session", "Autoriser pour la session"),
    ("deny", "Refuser"),
)
DEFAULT_DECISION = "deny"
_DECISIONS = {decision for decision, _label in APPROVAL_CHOICES}


def dialog_title(tool: str) -> str:
    """Titre stable utilisé par la fenêtre et le contrôle visuel."""
    return f"Confirmation requise - {tool}"


def decision_for(choice: str | None) -> str:
    """Normalise un choix ; une valeur absente ou inconnue vaut « refuser »."""
    return choice if choice in _DECISIONS else DEFAULT_DECISION


def _parent_window(parent: object | None) -> tk.Misc | None:
    """Retourne un parent Tk réellement initialisé, ou None sinon.

    Les tests et le contrôle visuel construisent parfois une fausse fenêtre
    avec `object.__new__` : elle est bien une sous-classe Tk, mais n'a pas de
    `tk` initialisé. La passer à `Toplevel` provoquerait une récursion.
    """
    if isinstance(parent, tk.Misc) and "tk" in vars(parent):
        return parent
    return None


class ApprovalDialog(tk.Toplevel):
    """Dialogue modal à trois boutons, avec refus par défaut."""

    def __init__(
        self,
        parent: tk.Misc | None,
        request: Any,
        on_decision: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self._on_decision = on_decision
        self._decided = False
        self.withdraw()
        self.title(dialog_title(str(getattr(request, "tool", "outil"))))
        self.resizable(False, False)
        if parent is not None:
            self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._refuse)
        self.bind("<Escape>", self._refuse)

        heading = tk.Label(
            self,
            text=dialog_title(str(getattr(request, "tool", "outil"))),
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            justify="left",
            wraplength=560,
        )
        heading.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(
            self,
            text="Choisissez explicitement l'action à appliquer :",
            anchor="w",
            justify="left",
            wraplength=560,
        ).pack(fill="x", padx=18, pady=(0, 8))

        preview_frame = tk.Frame(self)
        preview_frame.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        preview = tk.Text(
            preview_frame,
            height=8,
            width=64,
            wrap="word",
            relief="solid",
            borderwidth=1,
        )
        scrollbar = tk.Scrollbar(preview_frame, command=preview.yview)
        preview.configure(yscrollcommand=scrollbar.set)
        preview.insert("1.0", str(getattr(request, "preview", ""))[:1500])
        preview.configure(state="disabled")
        preview.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        buttons = tk.Frame(self)
        buttons.pack(fill="x", padx=14, pady=(0, 16))
        self._buttons: dict[str, tk.Button] = {}
        for decision, label in APPROVAL_CHOICES:
            button = tk.Button(
                buttons,
                text=label,
                width=22,
                command=lambda value=decision: self._choose(value),
                takefocus=True,
            )
            button.pack(side="left", expand=True, fill="x", padx=4)
            self._buttons[decision] = button
        self.refuse_button = self._buttons[DEFAULT_DECISION]
        self.refuse_button.configure(default=tk.ACTIVE)

        self.update_idletasks()
        width = max(620, self.winfo_reqwidth())
        height = max(360, self.winfo_reqheight())
        if parent is not None and parent.winfo_viewable():
            x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
            y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        else:
            x = max(0, (self.winfo_screenwidth() - width) // 2)
            y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.wait_visibility()
        self.grab_set()
        self.lift()
        self.refuse_button.focus_force()
        self.after_idle(self.refuse_button.focus_set)

    def _choose(self, decision: str) -> None:
        """Enregistre une décision une seule fois, puis referme le dialogue."""
        if self._decided:
            return
        self._decided = True
        self._on_decision(decision_for(decision))
        try:
            self.grab_release()
        finally:
            self.destroy()

    def _refuse(self, _event: object | None = None) -> None:
        """Fermeture (X), Échap ou toute action sans décision : refus."""
        self._choose(DEFAULT_DECISION)


def ask_tool_approval(parent: object | None, request: Any) -> str:
    """Ouvre la boîte modale et retourne la décision choisie.

    L'appelant fournit la demande d'approbation et reçoit toujours l'une des
    valeurs `once`, `session` ou `deny`. Une erreur de construction laisse
    l'appelant refuser par défaut.
    """
    outcome = {"value": DEFAULT_DECISION}
    dialog = ApprovalDialog(
        _parent_window(parent),
        request,
        lambda decision: outcome.__setitem__("value", decision),
    )
    dialog.wait_window()
    return outcome["value"]
