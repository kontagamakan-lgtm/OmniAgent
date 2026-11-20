"""Panneau Clés & LLM : les rafraîchissements ne cassent pas les clics."""

from __future__ import annotations

from types import SimpleNamespace

from ui.key_panel import KeyTable


class _Widget:
    """Doublure minimale des widgets CustomTkinter utilisés par le tableau."""

    def __init__(self, master=None, **kwargs) -> None:
        self.master = master
        self.kwargs = dict(kwargs)
        self.packed = False
        self.grid_removed = False
        self.destroyed = False

    def pack(self, *args, **kwargs) -> None:
        self.packed = True

    def pack_forget(self) -> None:
        self.packed = False

    def grid(self, *args, **kwargs) -> None:
        self.grid_removed = False

    def grid_remove(self) -> None:
        self.grid_removed = True

    def grid_columnconfigure(self, *args, **kwargs) -> None:
        pass

    def configure(self, **kwargs) -> None:
        self.kwargs.update(kwargs)

    def destroy(self) -> None:
        self.destroyed = True

    def invoke(self) -> None:
        self.kwargs["command"]()


CTK = SimpleNamespace(CTkFrame=_Widget, CTkLabel=_Widget, CTkButton=_Widget)


def _row(
    key_id: str,
    status: str,
    *,
    remaining_s: int = 0,
) -> dict[str, object]:
    return {
        "id": key_id,
        "provider_label": "OpenAI",
        "label": f"clé {key_id}",
        "hint": "sk-…",
        "status": status,
        "remaining_s": remaining_s,
        "tokens": 12,
        "latency_ms": 3.5,
    }


def test_key_panel_updates_rows_in_place_and_keeps_buttons_clickable() -> None:
    reset: list[str] = []
    removed: list[str] = []
    table = KeyTable(
        _Widget(),
        CTK,
        on_reset=reset.append,
        on_remove=removed.append,
    )
    rows = [_row("a", "ACTIVE"), _row("b", "COOLDOWN", remaining_s=42)]
    table.render(rows)

    line_a = table.rows["a"]["line"]
    detail_a = table.rows["a"]["detail"]
    delete_b = table.rows["b"]["remove"]
    assert len(table.rows) == 2
    assert table.rows["a"]["reset"].grid_removed, "ACTIVE n'a pas de bouton Réactiver"
    assert not table.rows["b"]["reset"].grid_removed

    # Le rafraîchissement change le compte à rebours et les statuts sans
    # recréer les lignes ni les boutons : le clic en cours reste valide.
    rows = [_row("a", "COOLDOWN", remaining_s=17), _row("b", "ACTIVE")]
    table.render(rows)

    assert table.rows["a"]["line"] is line_a
    assert table.rows["a"]["detail"] is detail_a
    assert table.rows["a"]["detail"].kwargs["text"].endswith("17 s · 12 tokens · 4 ms")
    assert not table.rows["a"]["reset"].grid_removed
    assert table.rows["b"]["reset"].grid_removed
    assert table.rows["b"]["remove"] is delete_b

    delete_b.invoke()
    assert removed == ["b"], "le bouton doit encore viser la bonne clé"

    table.rows["a"]["reset"].invoke()
    assert reset == ["a"]

    table.render([_row("a", "ACTIVE")])
    assert "b" not in table.rows
    assert table.rows["a"]["line"] is line_a
    assert table.rows["a"]["line"].destroyed is False
