"""États d'une liste qui dépend de données, et l'étiquette qui les dit.

Charger, être vide et échouer ne se disent pas de la même façon : un cadre muet
se lit comme « il n'y a rien », alors qu'il peut aussi bien signaler une lecture
qui a échoué. Ce module réunit la phrase et l'étiquette pour que les trois
listes de l'interface (journal, propositions, clés) se comportent pareil.
"""

from __future__ import annotations

from typing import Any

from ui.layout import SPACE_SM

__all__ = [
    "STATE_EMPTY",
    "STATE_ERROR",
    "STATE_LOADING",
    "StateLabel",
    "list_state_text",
]

STATE_LOADING = "chargement"
STATE_EMPTY = "vide"
STATE_ERROR = "erreur"


def list_state_text(
    state: str, *, subject: str, loading: str = "", empty: str = "", reason: str = ""
) -> str:
    """Phrase d'une liste selon son état, jamais un vide ambigu."""
    if state == STATE_LOADING:
        return loading
    if state == STATE_ERROR:
        return f"{subject} indisponible : {reason.strip() or 'cause inconnue'}"
    return empty


class StateLabel:
    """Étiquette d'état d'une liste, du chargement au contenu définitif.

    Elle s'ouvre sur « chargement » : la lecture qui suit se fait souvent sur le
    fil Tk, et `flush()` peint cet état avant elle pour qu'une lecture lente ne
    laisse pas un cadre muet. `show()` la fait réapparaître si elle avait été
    masquée par des lignes de tableau.
    """

    def __init__(
        self,
        ctk_module: Any,
        parent: Any,
        *,
        subject: str,
        loading: str,
        empty: str,
        pady: int = SPACE_SM,
        visible: bool = True,
    ) -> None:
        self.subject = subject
        self.empty = empty
        self.pady = pady
        self.visible = False
        self.widget = ctk_module.CTkLabel(
            parent,
            text=list_state_text(STATE_LOADING, subject=subject, loading=loading),
            anchor="w",
            justify="left",
        )
        if visible:
            self.show(STATE_LOADING)

    def show(self, state: str, *, reason: str = "") -> None:
        """Affiche l'état demandé, en le rendant visible s'il ne l'était pas."""
        self.widget.configure(
            text=list_state_text(
                state, subject=self.subject, empty=self.empty, reason=reason
            )
        )
        if not self.visible:
            self.widget.pack(fill="x", padx=SPACE_SM, pady=self.pady)
            self.visible = True

    def text(self, value: str, **options: Any) -> None:
        """Remplace l'état par un contenu définitif (journal, compte rendu)."""
        self.widget.configure(text=value, **options)

    def flush(self) -> None:
        """Peint l'état courant avant une lecture qui bloque le fil Tk."""
        self.widget.update_idletasks()

    def hide(self) -> None:
        if self.visible:
            self.widget.pack_forget()
            self.visible = False

    def destroy(self) -> None:
        self.widget.destroy()
        self.visible = False
