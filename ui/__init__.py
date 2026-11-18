"""Interface de l'agent : contrôleur sans interface, puis fenêtre CustomTkinter.

`ui.controller` ne dépend ni de tkinter ni de customtkinter : il exécute la
boucle ReAct dans un thread et expose les événements, ce qui le rend testable
sans écran. `ui.app_gui` ajoute la fenêtre, importée seulement au lancement.
"""

from __future__ import annotations

from ui.controller import AgentController

__all__ = ["AgentController", "AgentRun"]
