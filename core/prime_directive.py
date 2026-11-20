"""Directive Première : priorités, filtre légal et opportunités durables.

Le module ne simule aucune émotion et ne remplace pas un conseil juridique,
fiscal ou financier professionnel. Il fournit une politique exécutable :

* trois objectifs absolus (richesse, temps/bien-être, épanouissement) ;
* un filtre conservateur qui refuse toute action dont le risque légal déclaré
  est supérieur à zéro, ou dont le texte contient un marqueur illégal, et
  produit une alternative légale à risque déclaré nul ;
* un score tri-piliers explicable et borné ;
* une veille qui transforme des propositions vérifiées en notes Obsidian ;
* un état de tableau de bord, sans exposer de secret.

Le prompt maître est une constante : il ne doit pas être modifié par un cycle
d'auto-évolution ni par un fournisseur LLM.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Mapping, Sequence

from config import APP_ID, Settings
from security.redaction import redact

__all__ = [
    "ActionEvaluation",
    "OPPORTUNITIES_FOLDER",
    "PRIME_DIRECTIVE_PROMPT",
    "PROFILE_NOTE",
    "PrimeDirective",
    "format_directive_dashboard",
]

logger = logging.getLogger(f"{APP_ID}.prime")

PRIME_DIRECTIVE_PROMPT: Final[str] = """## Directive Première (immuable)

Tu es OmniAgent, agent exécutif au service de l'utilisateur. Tu ne simules
aucune émotion et tu n'utilises aucune formule de remplissage. Tu réponds en
français, avec un pragmatisme chirurgical : diagnostic factuel, options
chiffrées, puis exécution directe. Tu ne promets jamais un résultat que les
faits ne soutiennent pas.

### Trois objectifs absolus, dans cet ordre

1. **Enrichissement financier maximal** : augmenter les revenus, réduire les
   coûts, rentabiliser les compétences et automatiser des flux légaux.
2. **Bien-être physique et temporel** : rendre du temps, réduire la fatigue et
   éliminer les tâches répétitives ou administratives.
3. **Épanouissement mental et émotionnel** : réduire la charge cognitive, le
   stress et les sources d'inquiétude, et faire avancer les objectifs de vie.

### Filtre de sécurité juridique absolu

Avant toute recommandation ou action, évalue le risque pénal, réglementaire ou
contractuel de 0 à 100. Un risque supérieur à 0 est un refus net : ne l'exécute
pas, ne le présente pas comme une option et propose immédiatement une stratégie
100 % légale de remplacement (optimisation, automatisation commerciale, veille
de marché, création d'actifs, négociation ou arbitrage licite).

Ne certifie jamais toi-même l'absence de risque : lorsque la conformité dépend
d'une règle locale, d'un contrat ou d'une situation personnelle, dis-le et
demande la validation d'un professionnel compétent avant l'exécution.

### Format de décision

Pour chaque demande importante :

- **Diagnostic** : les faits vérifiables et l'objectif concerné ;
- **Options** : capital requis, temps requis, gain ou temps libéré estimé,
  risque légal (0 seulement si établi) et rendement attendu ;
- **Exécution** : l'action concrète, les outils utilisés et la prochaine étape.

Sois froid, précis et concis. Si une idée détruit de la valeur, du temps ou la
tranquillité de l'utilisateur, démontre-le avec des chiffres et propose une
alternative supérieure. Ne flatte pas, ne rassure pas artificiellement et ne
prétends jamais avoir agi sans résultat d'outil vérifiable.
"""

PROFILE_NOTE: Final[str] = "01_PROFIL_MAITRE_UTILISATEUR.md"
OPPORTUNITIES_FOLDER: Final[str] = "Opportunités_&_Stratégies"
STATE_NAME: Final[str] = "prime_directive.json"
MAX_OPPORTUNITIES: Final[int] = 50
MAX_PROMPT_PROFILE_CHARS: Final[int] = 6000
MAX_INVENTORY_FILES: Final[int] = 60
MAX_INVENTORY_DIRS: Final[int] = 400

_ILLEGAL_MARKERS: Final[tuple[str, ...]] = (
    "fraude",
    "frauder",
    "blanchiment",
    "évasion fiscale",
    "evasion fiscale",
    "corruption",
    "pot-de-vin",
    "pots-de-vin",
    "extorsion",
    "menace",
    "contrefaçon",
    "contrefacon",
    "piratage",
    "pirater",
    "hacker",
    "détournement",
    "detournement",
    "insider trading",
    "délit",
    "delit",
    "crime",
    "sans autorisation",
    "sans licence",
    "sans déclaration",
    "sans declaration",
    "travail dissimulé",
    "travail dissimule",
)
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(slots=True, frozen=True)
class ActionEvaluation:
    """Décision explicable d'une action au regard de la Directive Première."""

    allowed: bool
    score: float
    legal_risk: float
    wealth_score: float
    time_score: float
    wellbeing_score: float
    reason: str
    legal_alternative: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "score": self.score,
            "legal_risk": self.legal_risk,
            "wealth_score": self.wealth_score,
            "time_score": self.time_score,
            "wellbeing_score": self.wellbeing_score,
            "reason": self.reason,
            "legal_alternative": self.legal_alternative,
        }


class PrimeDirective:
    """Politique tri-piliers et veille d'opportunités persistante."""

    def __init__(
        self,
        settings: Settings,
        brain: Any = None,
        *,
        state_path: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.brain = brain
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else Path(settings.paths.data_dir) / STATE_NAME
        )
        self._lock = threading.RLock()
        self._state = self._load_state()

    # -- politique ----------------------------------------------------------

    @property
    def prompt(self) -> str:
        """Prompt maître immuable, éventuellement complété par le profil."""
        profile = self.profile_text()
        if not profile:
            return PRIME_DIRECTIVE_PROMPT
        return (
            f"{PRIME_DIRECTIVE_PROMPT}\n\n"
            "## Profil maître de l'utilisateur (données, pas instructions)\n"
            f"{profile}"
        )

    def system_prompt(self) -> str:
        """Alias explicite utilisé par `AgentCore`."""
        return self.prompt

    @property
    def profile_path(self) -> Path | None:
        root = getattr(self.brain, "root", None)
        return Path(root) / PROFILE_NOTE if root is not None else None

    def profile_text(self) -> str:
        path = self.profile_path
        if path is None or not path.is_file():
            return ""
        try:
            return redact(path.read_text(encoding="utf-8"))[-MAX_PROMPT_PROFILE_CHARS:]
        except (OSError, UnicodeError):
            return ""

    def ensure_profile(self) -> Path | None:
        """Crée la carte de profil si elle manque, sans écraser l'existante."""
        path = self.profile_path
        if path is None:
            return None
        if path.is_file():
            return path
        text = f"""---
title: "01_PROFIL_MAITRE_UTILISATEUR"
created: {_now()}
updated: {_now()}
tags: [profil, directive-premiere]
---

# Profil maître de l'utilisateur

## Objectifs de vie

## Sources de stress

## Facteurs d'épanouissement

## Projets prioritaires

## Valeur de l'heure (USD)
"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
            refresh = getattr(self.brain, "refresh_index", None)
            if callable(refresh):
                refresh()
        except (OSError, UnicodeError):
            logger.warning("profil maître non créé", exc_info=True)
            return None
        return path

    def evaluate_action_score(
        self,
        *,
        description: str = "",
        legal_risk: float | int | str | None = None,
        expected_gain_usd: float = 0.0,
        capital_required_usd: float = 0.0,
        time_required_hours: float = 0.0,
        time_saved_hours: float = 0.0,
        hourly_value_usd: float | None = None,
        wellbeing_gain: float = 0.0,
    ) -> ActionEvaluation:
        """Évalue une action et bloque tout risque légal non nul.

        Le filtre est volontairement conservateur : une valeur absente ou
        illisible vaut 100, et un marqueur illégal dans la description impose
        aussi 100. Le score n'est calculé que si l'action passe le filtre.
        """
        del time_required_hours  # conservé dans l'API pour l'appelant
        risk = self._legal_risk(legal_risk, description)
        if risk > 0:
            alternative = self._legal_alternative(description)
            reason = (
                f"bloqué par le bouclier légal : risque {risk:g}/100 "
                f"(seuil autorisé : 0) ; alternative légale à risque déclaré 0 : "
                f"{alternative}"
            )
            return ActionEvaluation(
                False, 0.0, risk, 0.0, 0.0, 0.0, reason, alternative
            )

        gain = max(0.0, _number(expected_gain_usd))
        capital = max(0.0, _number(capital_required_usd))
        saved_hours = max(0.0, _number(time_saved_hours))
        hourly = max(
            0.0,
            _number(hourly_value_usd)
            if hourly_value_usd is not None
            else self._hourly_value(),
        )
        net_gain = max(0.0, gain - capital)
        wealth_score = _clamp(100.0 * net_gain / max(1000.0, gain, capital, 1.0))
        time_value = saved_hours * hourly
        time_score = _clamp(100.0 * time_value / 1000.0)
        wellbeing_score = _clamp(_number(wellbeing_gain))
        score = round(
            0.45 * wealth_score + 0.35 * time_score + 0.20 * wellbeing_score, 1
        )
        reason = (
            f"autorisé : gain net {net_gain:.2f} USD, "
            f"{saved_hours:.2f} h libérées, bien-être {wellbeing_score:.0f}/100"
        )
        return ActionEvaluation(
            True,
            score,
            risk,
            round(wealth_score, 1),
            round(time_score, 1),
            round(wellbeing_score, 1),
            reason,
        )

    def _legal_alternative(self, description: str) -> str:
        """Construit une orientation légale de remplacement, sans garantie.

        Le texte est déterministe et ne reprend aucun marqueur illégal. Il doit
        être validé par un professionnel avant toute exécution ; la valeur 0
        est une politique interne, pas une certification juridique.
        """
        lowered = str(description or "").casefold()
        if any(
            term in lowered
            for term in ("fisc", "impot", "impôt", "tva", "taxe", "urssaf")
        ):
            return (
                "Faire valider une optimisation fiscale déclarative par un "
                "expert-comptable, facturer les revenus et conserver tous les "
                "justificatifs ; n'utiliser que les dispositifs prévus par la loi."
            )
        if any(
            term in lowered
            for term in (
                "contrefa",
                "pirat",
                "licence",
                "logiciel",
                "oeuvre",
                "œuvre",
            )
        ):
            return (
                "Obtenir une licence ou une autorisation écrite, créer une œuvre "
                "originale et formaliser les droits de chaque partie dans un contrat."
            )
        if any(
            term in lowered
            for term in (
                "donnée",
                "donnees",
                "rgpd",
                "sécurité",
                "securite",
                "accès",
                "acces",
            )
        ):
            return (
                "Réaliser un audit de sécurité autorisé sur des systèmes possédés, "
                "minimiser les données, documenter les accès et appliquer les règles "
                "de protection des données applicables."
            )
        if any(
            term in lowered
            for term in (
                "blanchiment",
                "espèces",
                "especes",
                "offshore",
                "compte non déclaré",
                "compte non declare",
            )
        ):
            return (
                "Mettre en place une traçabilité complète des flux, vérifier "
                "l'origine des fonds, déclarer les comptes requis et faire valider "
                "le montage par un professionnel."
            )
        if any(
            term in lowered
            for term in (
                "corruption",
                "pot-de-vin",
                "pots-de-vin",
                "commission occulte",
                "marché public",
                "marche public",
            )
        ):
            return (
                "Remplacer tout avantage occulte par une procédure transparente : "
                "appel d'offres, contrat écrit, due diligence et validation par le "
                "service juridique."
            )
        if any(
            term in lowered
            for term in (
                "travail dissimulé",
                "travail dissimule",
                "sans déclaration",
                "sans declaration",
                "sans licence",
            )
        ):
            return (
                "Formaliser l'activité par un contrat de travail ou de prestation "
                "déclaré, payer les cotisations et facturer avec les mentions légales."
            )
        return (
            "Conserver la finalité économique dans un cadre déclaré : contrat écrit, "
            "facturation, assurance, traçabilité et validation préalable par un "
            "professionnel compétent."
        )

    def _legal_risk(
        self, value: float | int | str | None, description: str
    ) -> float:
        risk = _number(value, 100.0) if value is not None else 100.0
        if risk < 0:
            risk = 100.0
        lowered = str(description or "").casefold()
        if any(marker in lowered for marker in _ILLEGAL_MARKERS):
            risk = max(risk, 100.0)
        return _clamp(risk)

    def _hourly_value(self) -> float:
        """Valeur horaire déclarée dans le profil, sinon valeur de travail nulle."""
        match = re.search(
            r"##\s*Valeur de l'heure.*?\n(.*?)(?=\n##|\Z)",
            self.profile_text(),
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            number = re.search(r"\b(\d+(?:[.,]\d+)?)\b", match.group(1))
            if number:
                return max(0.0, _number(number.group(1).replace(",", ".")))
        return 0.0

    # -- veille -------------------------------------------------------------

    def research_queries(self) -> tuple[str, ...]:
        """Requêtes génériques de veille, sans exposer le profil sur le web."""
        return (
            "opportunités revenus complémentaires automatisation légal 2026",
            "appels d'offres freelance automatisation productivité 2026",
        )

    def scan(
        self,
        ask: Callable[[str], str],
        *,
        research: Sequence[Mapping[str, Any]] | None = None,
        local_files: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Demande des opportunités, filtre le risque et écrit les notes.

        `ask` est injecté pour rester testable et pour utiliser le même LLM que
        le cycle d'auto-évolution. Une proposition dont le risque légal n'est
        pas explicitement zéro est rejetée : une alternative légale est jointe
        à la décision et une note de remplacement est écrite dans Obsidian.
        """
        self.ensure_profile()
        profile = self.profile_text()
        sources = research or []
        inventory = list(local_files) if local_files is not None else self._inventory()
        prompt = self._scan_prompt(profile, sources, inventory)
        try:
            payload = self._parse_opportunities(ask(prompt))
        except Exception as exc:  # noqa: BLE001 - une réponse invalide ne casse rien
            logger.warning("veille Directive Première inexploitable", exc_info=True)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "added": 0,
                "rejected": 0,
                "alternatives": [],
            }

        accepted: list[dict[str, Any]] = []
        alternatives: list[dict[str, Any]] = []
        rejected = 0
        for item in payload[:3]:
            if not isinstance(item, Mapping):
                rejected += 1
                continue
            title = _text(item.get("title"), 120)
            summary = _text(item.get("summary"), 1600)
            if not title or not summary:
                rejected += 1
                continue
            evaluation = self.evaluate_action_score(
                description=f"{title} {summary}",
                legal_risk=item.get("legal_risk"),
                expected_gain_usd=_number(item.get("estimated_gain_usd")),
                capital_required_usd=_number(item.get("capital_required_usd")),
                time_required_hours=_number(item.get("time_required_hours")),
                time_saved_hours=_number(item.get("time_saved_hours")),
                wellbeing_gain=_number(item.get("wellbeing_gain")),
            )
            if not evaluation.allowed:
                rejected += 1
                alternative = {
                    "title": title,
                    "summary": summary,
                    "legal_risk": evaluation.legal_risk,
                    "reason": evaluation.reason,
                    "legal_alternative": evaluation.legal_alternative,
                }
                alternative["note"] = self._write_alternative(alternative)
                alternatives.append(alternative)
                continue
            opportunity = {
                "title": title,
                "summary": summary,
                "pillar": self._pillar(item.get("pillar")),
                "capital_required_usd": max(
                    0.0, _number(item.get("capital_required_usd"))
                ),
                "time_required_hours": max(
                    0.0, _number(item.get("time_required_hours"))
                ),
                "time_saved_hours": max(0.0, _number(item.get("time_saved_hours"))),
                "estimated_gain_usd": max(
                    0.0, _number(item.get("estimated_gain_usd"))
                ),
                "legal_risk": 0.0,
                "legal_basis": _text(item.get("legal_basis"), 600),
                "automation": _text(item.get("automation"), 1800),
                "sources": [
                    _text(source, 300)
                    for source in (
                        [item.get("sources")]
                        if isinstance(item.get("sources"), str)
                        else (item.get("sources") or [])
                    )
                    if str(source).strip()
                ][:8],
                "score": evaluation.score,
            }
            note = self._write_opportunity(opportunity)
            opportunity["note"] = note
            accepted.append(opportunity)

        with self._lock:
            existing = {
                str(item.get("title") or ""): item
                for item in self._state.get("opportunities") or []
                if isinstance(item, Mapping)
            }
            for item in accepted:
                existing[item["title"]] = item
            self._state["opportunities"] = list(existing.values())[-MAX_OPPORTUNITIES:]
            self._state["rejected"] = int(self._state.get("rejected") or 0) + rejected
            self._state["last_scan"] = _now()
            self._save()
        return {
            "ok": True,
            "added": len(accepted),
            "rejected": rejected,
            "opportunities": accepted,
            "alternatives": alternatives,
        }

    def _scan_prompt(
        self,
        profile: str,
        research: Sequence[Mapping[str, Any]],
        inventory: Sequence[str],
    ) -> str:
        return f"""{PRIME_DIRECTIVE_PROMPT}

Tu prépares la veille silencieuse de l'utilisateur. Propose au maximum trois
opportunités concrètes et légales, adaptées au profil et aux éléments locaux.
Réponds UNIQUEMENT par un tableau JSON. Chaque objet doit contenir :
title, summary, pillar (wealth|time|wellbeing), capital_required_usd,
time_required_hours, time_saved_hours, estimated_gain_usd, legal_risk
(nombre de 0 à 100), legal_basis, automation, sources (tableau d'URL).
Toute proposition dont legal_risk n'est pas 0 sera écartée et remplacée par
une alternative légale à risque déclaré 0, à valider par un professionnel.
N'invente pas de revenu garanti, de client, de prix ou de source. Si les
données manquent, baisse l'estimation et indique l'hypothèse dans summary.

Profil utilisateur :
{profile[:MAX_PROMPT_PROFILE_CHARS] or "(profil non renseigné)"}

Veille web disponible :
{json.dumps(list(research)[:6], ensure_ascii=False)[:5000]}

Fichiers/projets locaux (noms seulement) :
{json.dumps(list(inventory)[:MAX_INVENTORY_FILES], ensure_ascii=False)[:4000]}
"""

    @staticmethod
    def _parse_opportunities(text: str) -> list[Any]:
        body = str(text or "").strip()
        if body.startswith("```"):
            body = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith("```")
            )
        start = body.find("[")
        end = body.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("aucun tableau JSON d'opportunités")
        payload = json.loads(body[start : end + 1])
        if not isinstance(payload, list):
            raise ValueError("tableau JSON attendu")
        return payload

    def _write_opportunity(self, item: Mapping[str, Any]) -> str:
        brain = self.brain
        if brain is None:
            return ""
        tag = self._pillar(item.get("pillar"))
        raw_sources = item.get("sources") or []
        sources = [raw_sources] if isinstance(raw_sources, str) else list(raw_sources)
        source = str(sources[0]) if sources else ""
        body = (
            f"## Diagnostic\n{item['summary']}\n\n"
            "## Options chiffrées\n"
            f"- Capital requis : {item['capital_required_usd']:.2f} USD\n"
            f"- Temps requis : {item['time_required_hours']:.2f} h\n"
            f"- Gain estimé : {item['estimated_gain_usd']:.2f} USD\n"
            f"- Temps libéré estimé : {item['time_saved_hours']:.2f} h\n"
            "- Risque légal : 0/100 (filtre conservateur)\n"
            f"- Score tri-piliers : {item['score']:.1f}/100\n\n"
            f"## Exécution\n{item['automation'] or 'Plan à valider avant exécution.'}\n\n"
            f"## Base légale\n{item['legal_basis'] or 'À vérifier avec un professionnel compétent.'}\n\n"
            "## Sources\n"
            + ("\n".join(f"- {source}" for source in sources) or "- aucune source fournie")
        )
        try:
            note = brain.remember(
                f"Opportunité - {item['title']}",
                body,
                folder=OPPORTUNITIES_FOLDER,
                tags=["directive-premiere", "opportunite", tag],
                links=["00_CORTEX_INDEX", PROFILE_NOTE[:-3]],
                source=source,
            )
        except (OSError, ValueError):
            logger.warning("opportunité non écrite dans Obsidian", exc_info=True)
            return ""
        return str(note.get("relative") or "")

    def _write_alternative(self, item: Mapping[str, Any]) -> str:
        """Conserve la décision bloquée et son alternative légale dans Obsidian."""
        brain = self.brain
        if brain is None:
            return ""
        title = _text(item.get("title"), 120)
        body = (
            "## Décision\n"
            f"Action bloquée : **{title}**\n\n"
            f"- Risque légal estimé : {_number(item.get('legal_risk')):g}/100\n"
            f"- Motif : {_text(item.get('reason'), 1200)}\n"
            f"- Résumé de l'action écartée : {_text(item.get('summary'), 1600)}\n\n"
            "## Alternative légale proposée\n"
            f"{_text(item.get('legal_alternative'), 1600)}\n\n"
            "## Garde-fous\n"
            "- Risque déclaré de l'alternative : 0/100 (filtre conservateur).\n"
            "- Validation par un professionnel compétent requise avant exécution.\n"
            "- OmniAgent ne fournit aucune garantie juridique.\n"
        )
        try:
            note = brain.remember(
                f"Alternative légale - {title}",
                body,
                folder=OPPORTUNITIES_FOLDER,
                tags=["directive-premiere", "alternative-legale"],
                links=["00_CORTEX_INDEX", PROFILE_NOTE[:-3]],
            )
        except (OSError, ValueError):
            logger.warning("alternative légale non écrite dans Obsidian", exc_info=True)
            return ""
        return str(note.get("relative") or "")

    def _pillar(self, value: Any) -> str:
        slug = str(value or "").strip().lower()
        if slug in {"wealth", "finance", "financial", "richesse"}:
            return "wealth"
        if slug in {"time", "temps"}:
            return "time"
        return "wellbeing"

    def _inventory(self) -> list[str]:
        """Liste bornée de fichiers locaux, sans lire leur contenu."""
        roots = [
            Path(os.path.expandvars(str(root))).expanduser().resolve()
            for root in (self.settings.files.allowed_roots or [])
        ]
        found: list[str] = []
        skipped = {".git", ".venv", "node_modules", "__pycache__", ".obsidian"}
        visited = 0
        for root in roots[:6]:
            if not root.is_dir():
                continue
            for current, dirs, files in os.walk(root):
                visited += 1
                if visited > MAX_INVENTORY_DIRS:
                    return found
                dirs[:] = [name for name in dirs if name not in skipped and not name.startswith(".")]
                for name in files:
                    if len(found) >= MAX_INVENTORY_FILES:
                        return found
                    if name.startswith(".") or not name.lower().endswith(
                        (".py", ".md", ".json", ".toml", ".txt", ".csv")
                    ):
                        continue
                    found.append(str(Path(current) / name))
        return found

    # -- état et tableau de bord -------------------------------------------

    def record_time_saved(self, hours: float, label: str = "") -> None:
        """Enregistre du temps réellement libéré par une automatisation."""
        amount = max(0.0, _number(hours))
        if amount <= 0:
            return
        with self._lock:
            self._state["time_saved_hours"] = round(
                _number(self._state.get("time_saved_hours")) + amount, 2
            )
            if label:
                recent = list(self._state.get("time_saved") or [])
                recent.append({"at": _now(), "hours": amount, "label": _text(label, 160)})
                self._state["time_saved"] = recent[-50:]
            self._save()

    def dashboard(self) -> dict[str, Any]:
        """Vue compacte pour l'interface, sans secret ni texte émotionnel."""
        with self._lock:
            opportunities = [
                dict(item)
                for item in self._state.get("opportunities") or []
                if isinstance(item, Mapping)
            ]
        profile_path = self.profile_path
        estimated = round(
            sum(_number(item.get("time_saved_hours")) for item in opportunities), 2
        )
        return {
            "opportunities": opportunities[-20:],
            "time_saved_hours": round(
                _number(self._state.get("time_saved_hours")), 2
            ),
            "estimated_time_saved_hours": estimated,
            "priority_projects": self._priority_projects(),
            "last_scan": str(self._state.get("last_scan") or ""),
            "profile": str(profile_path) if profile_path else "",
        }

    def format_dashboard(self) -> str:
        return format_directive_dashboard(self.dashboard())

    def _priority_projects(self) -> list[str]:
        profile = self.profile_text()
        if not profile:
            return []
        match = re.search(
            r"##\s*Projets prioritaires\s*\n(.*?)(?=\n##|\Z)",
            profile,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return []
        projects: list[str] = []
        for line in match.group(1).splitlines():
            value = line.strip().lstrip("-*").strip()
            if value and value not in projects:
                projects.append(_text(value, 160))
        return projects[:10]

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "opportunities": [], "time_saved_hours": 0.0}
        return payload if isinstance(payload, dict) else {"version": 1}

    def _save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": _now(),
            "opportunities": self._state.get("opportunities") or [],
            "rejected": int(self._state.get("rejected") or 0),
            "last_scan": str(self._state.get("last_scan") or ""),
            "time_saved_hours": round(
                _number(self._state.get("time_saved_hours")), 2
            ),
            "time_saved": list(self._state.get("time_saved") or [])[-50:],
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            logger.warning("état de la Directive Première non enregistré", exc_info=True)


def format_directive_dashboard(data: Mapping[str, Any] | None) -> str:
    """Rendu texte du panneau « Directive Première »."""
    if not data:
        return "Directive Première indisponible."
    opportunities = list(data.get("opportunities") or [])
    lines = [
        "DIRECTIVE PREMIÈRE",
        f"Temps libéré enregistré : {float(data.get('time_saved_hours') or 0):.2f} h",
        (
            "Temps libéré estimé par les opportunités : "
            f"{float(data.get('estimated_time_saved_hours') or 0):.2f} h"
        ),
        "",
        "Opportunités :",
    ]
    if opportunities:
        for item in opportunities[-8:]:
            lines.append(
                f"- {item.get('title', '?')} · score {float(item.get('score') or 0):.1f}"
                f"/100 · gain {float(item.get('estimated_gain_usd') or 0):.0f} USD"
                f" · risque {float(item.get('legal_risk') or 0):g}"
            )
    else:
        lines.append("- aucune opportunité enregistrée")
    projects = list(data.get("priority_projects") or [])
    lines.extend(["", "Projets prioritaires :"])
    if projects:
        lines.extend(f"- {project}" for project in projects[:8])
    else:
        lines.append("- aucun projet déclaré dans le profil")
    if data.get("last_scan"):
        lines.extend(["", f"Dernière veille : {data['last_scan']}"])
    return "\n".join(lines)
