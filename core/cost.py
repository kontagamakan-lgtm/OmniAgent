"""Estimation du coût en devise à partir des tokens facturés.

Les plafonds d'autonomie comptaient des tokens, unité que l'utilisateur ne voit
pas dans sa facture. Ce module convertit prompt et complétion en dollars US à
partir d'une table par modèle, remplaçable par configuration :

* `settings.llm.price_prompt_per_million` et
  `settings.llm.price_completion_per_million` (0 = utiliser la table) ;
* `data/pricing.json` pour une table complète, au format
  `{"modele": {"prompt": 0.27, "completion": 1.10}}`, prix par million.

La table par défaut est volontairement prudente : ce sont des ordres de grandeur
publics, pas une facture. Un modèle inconnu donne 0 et le budget en devise ne le
bloque donc pas ; le budget en tokens reste la garantie dure.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Mapping

from config import APP_ID, Settings

__all__ = ["DEFAULT_PRICES", "estimate_cost", "load_prices", "pricing_path"]

logger = logging.getLogger(f"{APP_ID}.cost")

#: Dollars US par million de tokens (prompt, complétion).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3-mini": (1.10, 4.40),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-1": (15.00, 75.00),
}


def pricing_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "pricing.json"


def load_prices(settings: Settings) -> dict[str, tuple[float, float]]:
    """Table par défaut, surchargée par `data/pricing.json` s'il est lisible."""
    prices = dict(DEFAULT_PRICES)
    try:
        raw = json.loads(pricing_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return prices
    if not isinstance(raw, dict):
        return prices
    for model, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            prices[str(model)] = (
                float(value.get("prompt", 0.0)),
                float(value.get("completion", 0.0)),
            )
        except (TypeError, ValueError):
            continue
    return prices


def _lookup(model: str, prices: Mapping[str, tuple[float, float]]) -> tuple[float, float]:
    name = str(model or "").strip().lower()
    if not name:
        return 0.0, 0.0
    if name in prices:
        return prices[name]
    # `deepseek-chat-2026-01` doit retrouver `deepseek-chat` sans confondre
    # `gpt-4o` et `gpt-4o-mini`.
    matches = [
        (len(key), value)
        for key, value in prices.items()
        if name.startswith(key.lower())
    ]
    if not matches:
        return 0.0, 0.0
    return max(matches, key=lambda item: item[0])[1]


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    prices: Mapping[str, tuple[float, float]] | None = None,
    prompt_price: float = 0.0,
    completion_price: float = 0.0,
) -> float:
    """Coût estimé en dollars US, arrondi au dix-millième."""
    prompt_rate, completion_rate = (
        (float(prompt_price), float(completion_price))
        if prompt_price or completion_price
        else _lookup(model, prices or DEFAULT_PRICES)
    )
    amount = (
        max(0, int(prompt_tokens or 0)) * prompt_rate
        + max(0, int(completion_tokens or 0)) * completion_rate
    ) / 1_000_000
    return round(amount, 4)
