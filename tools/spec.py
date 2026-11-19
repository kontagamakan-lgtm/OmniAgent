"""Spécification d'outil, en complément des classes `Tool`.

Un `ToolSpec` décrit une simple fonction Python (nom, schéma, drapeaux) ;
`as_tool()` la convertit en `Tool` classique, pour qu'un outil écrit dans ce
style rejoigne les outils historiques sans changer la boucle ReAct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from tools.base import FunctionTool, Tool

__all__ = ["ToolSpec"]


@dataclass(slots=True)
class ToolSpec:
    """Déclaration d'un outil : nom, schéma JSON, fonction, drapeaux."""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False
    read_only: bool = True
    sensitive: bool = False

    def as_tool(self) -> Tool:
        """Convertit la spécification en outil exécutable par le registre."""
        return FunctionTool(
            self.name,
            self.description,
            self.parameters,
            self.func,
            read_only=self.read_only,
            dangerous=self.dangerous,
            sensitive=self.sensitive,
        )
