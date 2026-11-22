"""Assistant de premier démarrage.

Un nouvel utilisateur lançait l'exécutable devant une fenêtre qui répondait
« clé absente » à chaque envoi, sans moyen de la renseigner. Cet assistant
recueille les réglages indispensables - fournisseur et clé API, dossier de
travail, canal du navigateur, phrase du coffre - puis les persiste :

* clé API  -> variable d'environnement du fournisseur choisi (``llm.api_key_env``)
  et fichier ``.env`` du répertoire d'installation (jamais ``config.json``) ;
  le fournisseur est imposé par la liste ou déduit du préfixe de la clé, ce
  qui règle ``llm.backend``, ``llm.base_url`` et le trio de modèles ;
* dossier  -> ajouté à ``files.allowed_roots`` (le confinement du
  ``FileManager`` le prend en compte au démarrage suivant) ;
* canal    -> ``browser.channel`` (``""`` = Chromium embarqué, sinon Edge/Chrome
  du système) ;
* coffre   -> créé avec la phrase saisie s'il n'existe pas déjà.

La logique de validation vit dans `apply_setup`, sans dépendance à Tk, pour
être testable ; `run_setup` n'est que la fenêtre.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from config import (
    APP_ID,
    Settings,
    api_key_problem,
    expand_path,
    insecure_http_url,
    install_dir,
)
from core.llm_router import PROVIDERS, detect_provider
from ui.layout import SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XS

__all__ = [
    "CHANNELS",
    "PROVIDER_CHOICES",
    "apply_setup",
    "current_provider",
    "needs_setup",
    "run_setup",
    "write_env_value",
]

logger = logging.getLogger(f"{APP_ID}.setup")

#: Canaux proposés : libellé affiché -> valeur de `browser.channel`.
CHANNELS: tuple[tuple[str, str], ...] = (
    ("Automatique (Chromium embarqué, sinon Edge)", ""),
    ("Microsoft Edge", "msedge"),
    ("Google Chrome", "chrome"),
)
_ALLOWED_CHANNELS = frozenset(value for _label, value in CHANNELS)
_MIN_PASSPHRASE = 8

#: Fournisseurs que le client unique sait appeler : endpoints compatibles
#: OpenAI, plus Anthropic par son API Messages. « custom » couvre tout autre
#: service compatible OpenAI : l'adresse et le modèle sont alors saisis à la
#: main.
#: slug -> (variable d'environnement, modèle, modèle rapide, raisonnement).
_PROVIDER_SETTINGS: dict[str, tuple[str, str, str, str]] = {
    "deepseek": (
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        "deepseek-chat",
        "deepseek-reasoner",
    ),
    "openai": ("OPENAI_API_KEY", "gpt-5-mini", "gpt-5-mini", "gpt-5"),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-opus-4-1",
    ),
    "gemini": (
        "GEMINI_API_KEY",
        "gemini-2.5-flash",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    ),
    "groq": (
        "GROQ_API_KEY",
        "llama-3.3-70b-versatile",
        "llama-3.3-70b-versatile",
        "llama-3.3-70b-versatile",
    ),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "deepseek/deepseek-chat",
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
    ),
    "mistral": (
        "MISTRAL_API_KEY",
        "mistral-large-latest",
        "mistral-small-latest",
        "mistral-large-latest",
    ),
    #: Tout autre service parlant `/chat/completions` : l'adresse et le modèle
    #: viennent des champs de l'assistant.
    "custom": ("OMNIAGENT_API_KEY", "", "", ""),
}

#: Choix affiché -> slug. « Automatique » lit le préfixe de la clé.
PROVIDER_CHOICES: tuple[tuple[str, str], ...] = (
    ("Automatique (d'après la clé)", "auto"),
    ("DeepSeek", "deepseek"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gemini", "gemini"),
    ("Groq", "groq"),
    ("OpenRouter", "openrouter"),
    ("Mistral", "mistral"),
    ("Autre (compatible OpenAI)", "custom"),
)
_LABELS = {slug: label for label, slug in PROVIDER_CHOICES if slug != "auto"}

def current_provider(settings: Settings) -> str:
    """Fournisseur déjà configuré, déduit de la variable de clé attendue."""
    env = settings.llm.api_key_env
    for slug, values in _PROVIDER_SETTINGS.items():
        if values[0] == env:
            return slug
    return "deepseek"


def _resolve_provider(settings: Settings, provider: str, key: str) -> tuple[str, str]:
    """Slug retenu pour cette saisie, et refus éventuel à afficher.

    Un fournisseur vide garde celui déjà configuré : l'assistant rouvert pour
    changer de modèle ne doit pas changer de fournisseur par surprise.
    """
    requested = str(provider or "").strip().lower()
    if not requested:
        return current_provider(settings), ""
    if requested == "auto":
        try:
            detected = detect_provider(key)
        except ValueError:
            return "", "fournisseur indétectable d'après la clé : choisissez-le dans la liste"
        return detected, ""
    if requested not in _PROVIDER_SETTINGS:
        return "", f"fournisseur inconnu : {provider!r}"
    return requested, ""


def _configure_llm(
    settings: Settings, slug: str, *, base_url: str = "", model: str = ""
) -> list[str]:
    """Aligne le client unique sur le fournisseur retenu.

    Un modèle choisi à la main dans `config.json` n'est pas écrasé par un
    réenregistrement de clé du même fournisseur : les modèles par défaut ne
    s'appliquent qu'au changement de fournisseur, et le champ « Modèle » ne
    prime que s'il diffère de ce défaut.
    """
    env_var, default_model, fast_model, reasoning_model = _PROVIDER_SETTINGS[slug]
    llm = settings.llm
    provider = PROVIDERS.get(slug)
    updates: list[str] = []
    endpoint = base_url.strip() or (provider.base_url if provider else "")
    chosen = model.strip()
    if chosen and chosen != default_model:
        llm.model = chosen
        llm.fast_model = chosen
        llm.reasoning_model = chosen
        updates.append(f"modèle : {chosen}")
    elif llm.api_key_env != env_var and default_model:
        llm.model = default_model
        llm.fast_model = fast_model
        llm.reasoning_model = reasoning_model
        updates.append(f"modèles : {default_model} et {reasoning_model}")
    if endpoint and endpoint != llm.base_url:
        updates.append(f"adresse : {endpoint}")
    llm.backend = slug if slug in {"deepseek", "anthropic"} else "openai_compatible"
    llm.base_url = endpoint
    llm.api_key_env = env_var
    return updates


def needs_setup(settings: Settings) -> bool:
    """Vrai si l'application ne peut pas encore générer de réponse.

    Un backend local (Ollama) n'a pas de clé : l'assistant ne s'impose pas.
    """
    if settings.llm.backend == "ollama" or settings.llm_router.enabled:
        return False
    return not settings.llm.has_credentials()


def write_env_value(path: Path, key: str, value: str) -> Path:
    """Écrit ``key=value`` dans un fichier `.env`, sans toucher aux autres lignes.

    Le fichier est mis à jour en place (les commentaires et les autres clés sont
    conservés) puis remplacé atomiquement. Les permissions sont restreintes au
    propriétaire là où le système le permet.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rendered = f'{key}={_quote_env(value)}'
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing = stripped.split("=", 1)[0].strip()
        if existing == key:
            lines[index] = rendered
            replaced = True
            break
    if not replaced:
        lines.append(rendered)
    text = "\n".join(lines).rstrip("\n") + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:  # Windows n'applique pas les modes POSIX ; sans effet ailleurs.
        path.chmod(0o600)
    except OSError:  # pragma: no cover - système de fichiers exotique
        pass
    return path


def _quote_env(value: str) -> str:
    if not value or any(c in value for c in ' \t#"\'') or value != value.strip():
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _custom_endpoint_problem(endpoint: str, model: str) -> str:
    """Un fournisseur libre doit fournir son adresse et son modèle."""
    if not endpoint:
        return (
            "adresse de base requise pour un fournisseur compatible OpenAI "
            "(par exemple https://api.exemple.com/v1)"
        )
    if not endpoint.startswith(("http://", "https://")):
        return f"adresse de base invalide : {endpoint!r} (http:// ou https:// attendu)"
    if not model.strip():
        return "modèle requis pour un fournisseur compatible OpenAI"
    return ""


def _normalize_roots(settings: Settings, workdir: str) -> tuple[str, str | None]:
    """Valide le dossier choisi et retourne (chemin, erreur éventuelle)."""
    candidate = workdir.strip()
    if not candidate:
        return "", None
    try:
        target = expand_path(candidate)
    except OSError as exc:
        return "", f"dossier de travail illisible : {exc}"
    if not target.is_dir():
        return "", f"dossier de travail introuvable : {target}"
    return str(target), None


def apply_setup(
    settings: Settings,
    *,
    api_key: str = "",
    provider: str = "",
    base_url: str = "",
    model: str = "",
    workdir: str = "",
    channel: str | None = None,
    passphrase: str = "",
    confirm: str = "",
    env_path: Path | None = None,
    save: bool = True,
) -> list[str]:
    """Applique les réglages saisis et retourne la liste des erreurs.

    La fonction est volontairement tolérante : elle applique ce qui est valide,
    rapporte le reste, et ne laisse jamais fuiter la clé dans un message. Une
    liste vide signifie « prêt à générer ».
    """
    errors: list[str] = []
    updates: list[str] = []

    key = api_key.strip()
    # Une clé malformée était acceptée puis écrite dans `.env` : l'application
    # la rejetait au démarrage suivant, et l'utilisateur devait tout ressaisir
    # sans savoir pourquoi. Le préflight la refuse ici, avant l'écriture, avec
    # les règles du fournisseur retenu plutôt que celles de DeepSeek.
    if key:
        slug, problem = _resolve_provider(settings, provider, key)
        env_var = _PROVIDER_SETTINGS[slug][0] if slug else settings.llm.api_key_env
        endpoint = base_url.strip()
        if not problem and slug == "custom":
            problem = _custom_endpoint_problem(endpoint, model)
        if not problem and endpoint and insecure_http_url(endpoint):
            problem = (
                "adresse de base en clair refusée : https est requis hors "
                "boucle locale, sinon la clé traverse le réseau lisible"
            )
        if not problem:
            problem = api_key_problem(key, provider=slug, origin=env_var)
        if problem:
            errors.append(problem)
        else:
            os.environ[env_var] = key
            updates.append(f"clé {_LABELS[slug]} enregistrée dans {env_var}")
            updates.extend(
                _configure_llm(settings, slug, base_url=endpoint, model=model)
            )
            target_env = Path(env_path) if env_path else install_dir() / ".env"
            try:
                write_env_value(target_env, env_var, key)
                updates.append(f"clé écrite dans {target_env}")
            except OSError as exc:
                errors.append(
                    f"impossible d'écrire {target_env} : {exc} (clé gardée en mémoire)"
                )
    elif needs_setup(settings):
        errors.append(
            f"clé API requise : renseignez {settings.llm.api_key_env} pour générer des réponses"
        )

    if channel is not None:
        candidate = channel.strip()
        if candidate not in _ALLOWED_CHANNELS:
            errors.append(f"canal navigateur inconnu : {candidate!r}")
        elif settings.browser.channel != candidate:
            settings.browser.channel = candidate
            updates.append(f"navigateur : {candidate or 'automatique'}")

    root, problem = _normalize_roots(settings, workdir)
    if problem:
        errors.append(problem)
    elif root:
        existing = {str(expand_path(raw)) for raw in settings.files.allowed_roots}
        if root not in existing:
            settings.files.allowed_roots = [*settings.files.allowed_roots, root]
            updates.append(f"dossier de travail autorisé : {root}")

    if passphrase or confirm:
        if passphrase != confirm:
            errors.append("les deux phrases du coffre ne correspondent pas")
        elif len(passphrase) < _MIN_PASSPHRASE:
            errors.append(f"phrase du coffre trop courte (au moins {_MIN_PASSPHRASE} caractères)")
        else:
            from tools.vault import Vault, VaultError

            vault = Vault(settings)
            if vault.exists:
                updates.append("coffre existant conservé")
            else:
                try:
                    vault.create(passphrase)
                    updates.append(f"coffre créé : {settings.paths.vault_file}")
                except VaultError as exc:
                    errors.append(f"coffre non créé : {exc}")

    if save and updates:
        try:
            settings.save()
        except OSError as exc:
            errors.append(f"configuration non enregistrée : {exc}")
    if updates:
        logger.info("Assistant de démarrage : %s", " ; ".join(updates))
    return errors


# ---------------------------------------------------------------------------
# Fenêtre (customtkinter optionnel : le module s'importe sans lui)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - dépend de l'environnement
    import customtkinter as ctk
    from tkinter import filedialog
except ImportError:  # pragma: no cover
    ctk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]


def run_setup(settings: Settings, *, parent: object | None = None) -> bool:
    """Ouvre l'assistant et retourne True si les réglages ont été enregistrés.

    Sans customtkinter (tests, exécution sans interface), retourne False sans
    rien tenter : l'appelant décide quoi afficher.
    """
    if ctk is None:  # pragma: no cover - environnement sans interface
        return False
    wizard = SetupWizard(settings, parent=parent)
    try:
        wizard.wait_window()
    except Exception:  # pragma: no cover - fenêtre détruite par l'utilisateur
        return False
    return bool(wizard.saved)


if ctk is not None:  # pragma: no branch - une seule branche est définie

    class SetupWizard(ctk.CTkToplevel):  # type: ignore[misc, name-defined]
        """Fenêtre modale en quatre étapes, sans dépendre de la fenêtre principale."""

        def __init__(self, settings: Settings, *, parent: object | None = None) -> None:
            super().__init__(parent)  # type: ignore[arg-type]
            self.settings = settings
            self.saved = False
            self.title("Configuration initiale - OmniAgent")
            self.geometry("620x520")
            self.resizable(False, False)
            self.transient(parent)  # type: ignore[arg-type]
            self.protocol("WM_DELETE_WINDOW", self._skip)

            title_font = (settings.ui.font_family, settings.ui.font_size + 2, "bold")
            ctk.CTkLabel(
                self,
                text="Bienvenue : branchez votre fournisseur et votre clé",
                font=title_font,
                anchor="w",
                justify="left",
                wraplength=580,
            ).pack(fill="x", padx=SPACE_LG, pady=(SPACE_LG, SPACE_XS))
            ctk.CTkLabel(
                self,
                text=(
                    "Ces valeurs sont enregistrées localement. La clé va dans .env, "
                    "jamais dans config.json."
                ),
                text_color=("#556070", "#8A95A3"),
                anchor="w",
                justify="left",
                wraplength=580,
            ).pack(fill="x", padx=SPACE_LG, pady=(0, SPACE_MD))

            form = ctk.CTkFrame(self)
            # `fill="x"` sans `expand` : huit lignes remplissent la fenêtre, et
            # l'espace résiduel ne se creuse pas entre la confirmation et les
            # boutons quand un champ de plus est ajouté.
            form.pack(fill="x", padx=SPACE_LG, pady=SPACE_XS)
            form.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(form, text="Fournisseur", anchor="w").grid(
                row=0, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            labels = [label for label, _slug in PROVIDER_CHOICES]
            slugs = [slug for _label, slug in PROVIDER_CHOICES]
            start = current_provider(settings)
            self.provider_menu = ctk.CTkOptionMenu(form, values=labels)
            self.provider_menu.set(labels[slugs.index(start)] if start in slugs else labels[1])
            self.provider_menu.grid(
                row=0, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM
            )

            ctk.CTkLabel(form, text="Clé API", anchor="w").grid(
                row=1, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            self.key_entry = ctk.CTkEntry(
                form, show="•", placeholder_text="collez la clé du fournisseur"
            )
            self.key_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            ctk.CTkLabel(form, text="Adresse de base", anchor="w").grid(
                row=2, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            self.base_entry = ctk.CTkEntry(
                form, placeholder_text="https://api.exemple.com/v1"
            )
            self.base_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            ctk.CTkLabel(form, text="Modèle", anchor="w").grid(
                row=3, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            self.model_entry = ctk.CTkEntry(form, placeholder_text="nom du modèle")
            self.model_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            ctk.CTkLabel(form, text="Dossier de travail", anchor="w").grid(
                row=4, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            default_root = settings.files.allowed_roots[0] if settings.files.allowed_roots else ""
            self.dir_entry = ctk.CTkEntry(form, placeholder_text=default_root)
            self.dir_entry.grid(row=4, column=1, sticky="ew", padx=(SPACE_MD, SPACE_SM), pady=SPACE_SM)
            ctk.CTkButton(form, text="Parcourir…", width=110, command=self._pick_dir).grid(
                row=4, column=2, sticky="e", padx=(0, SPACE_MD), pady=SPACE_SM
            )

            ctk.CTkLabel(form, text="Navigateur", anchor="w").grid(
                row=5, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            current = settings.browser.channel
            labels = [label for label, value in CHANNELS]
            values = [value for _label, value in CHANNELS]
            start = labels[values.index(current)] if current in values else labels[0]
            self.channel_menu = ctk.CTkOptionMenu(form, values=labels)
            self.channel_menu.set(start)
            self.channel_menu.grid(row=5, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            vault_hint = (
                "Coffre existant : laissez vide pour le conserver"
                if settings.paths.vault_file.is_file()
                else "Crée le coffre-fort des identifiants (optionnel)"
            )
            ctk.CTkLabel(form, text="Phrase du coffre", anchor="w").grid(
                row=6, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            self.pass_entry = ctk.CTkEntry(form, show="•", placeholder_text=vault_hint)
            self.pass_entry.grid(row=6, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            ctk.CTkLabel(form, text="Confirmation", anchor="w").grid(
                row=7, column=0, sticky="w", padx=SPACE_MD, pady=SPACE_SM
            )
            self.confirm_entry = ctk.CTkEntry(form, show="•")
            self.confirm_entry.grid(row=7, column=1, columnspan=2, sticky="ew", padx=SPACE_MD, pady=SPACE_SM)

            # L'adresse et le modèle suivent le fournisseur choisi : le menu
            # les remplit avec ses valeurs, puis l'utilisateur peut les corriger
            # (« Autre » part d'une page blanche).
            self.provider_menu.configure(command=self._sync_provider_fields)
            self._sync_provider_fields()

            self.message = ctk.CTkLabel(
                self,
                text="",
                text_color=("#A33B33", "#E08B82"),
                anchor="w",
                wraplength=580,
            )
            self.message.pack(fill="x", padx=SPACE_LG, pady=(SPACE_XS, 0))

            buttons = ctk.CTkFrame(self)
            buttons.pack(fill="x", padx=SPACE_LG, pady=(SPACE_XS, SPACE_LG))
            ctk.CTkButton(buttons, text="Plus tard", width=110, command=self._skip).pack(
                side="left"
            )
            ctk.CTkButton(buttons, text="Enregistrer et démarrer", command=self._save).pack(
                side="right"
            )
            self.key_entry.focus_set()
            self.after(120, self._focus_self)

        def _focus_self(self) -> None:
            try:
                self.grab_set()
                self.lift()
            except Exception:  # pragma: no cover - gestionnaire de fenêtres
                pass

        def _pick_dir(self) -> None:
            chosen = filedialog.askdirectory(parent=self) if filedialog else ""
            if chosen:
                self.dir_entry.delete(0, "end")
                self.dir_entry.insert(0, chosen)

        def _sync_provider_fields(self, _choice: str | None = None) -> None:
            slug = self._selected_provider()
            provider = PROVIDERS.get(slug)
            self.base_entry.delete(0, "end")
            self.model_entry.delete(0, "end")
            if provider is not None:
                self.base_entry.insert(0, provider.base_url)
            default_model = _PROVIDER_SETTINGS.get(slug, ("", "", "", ""))[1]
            if default_model:
                self.model_entry.insert(0, default_model)

        def _selected_provider(self) -> str:
            label = self.provider_menu.get()
            for text, slug in PROVIDER_CHOICES:
                if text == label:
                    return slug
            return ""

        def _selected_channel(self) -> str:
            label = self.channel_menu.get()
            for text, value in CHANNELS:
                if text == label:
                    return value
            return ""

        def _save(self) -> None:
            errors = apply_setup(
                self.settings,
                api_key=self.key_entry.get(),
                provider=self._selected_provider(),
                base_url=self.base_entry.get(),
                model=self.model_entry.get(),
                workdir=self.dir_entry.get(),
                channel=self._selected_channel(),
                passphrase=self.pass_entry.get(),
                confirm=self.confirm_entry.get(),
            )
            if errors:
                self.message.configure(text="\n".join(f"• {line}" for line in errors))
                return
            self.saved = True
            self._close()

        def _skip(self) -> None:
            self.saved = False
            self._close()

        def _close(self) -> None:
            try:
                self.grab_release()
            except Exception:  # pragma: no cover - fenêtre déjà libérée
                pass
            self.destroy()

else:  # pragma: no cover - environnement sans customtkinter
    SetupWizard = None  # type: ignore[assignment, misc]
