"""Assistant de premier démarrage.

Un nouvel utilisateur lançait l'exécutable devant une fenêtre qui répondait
« clé absente » à chaque envoi, sans moyen de la renseigner. Cet assistant
recueille les quatre réglages indispensables — clé API, dossier de travail,
canal du navigateur, phrase du coffre — puis les persiste :

* clé API  → variable d'environnement du fournisseur (``llm.api_key_env``) et
  fichier ``.env`` du répertoire d'installation (jamais ``config.json``) ;
* dossier  → ajouté à ``files.allowed_roots`` (le confinement du
  ``FileManager`` le prend en compte au démarrage suivant) ;
* canal    → ``browser.channel`` (``""`` = Chromium embarqué, sinon Edge/Chrome
  du système) ;
* coffre   → créé avec la phrase saisie s'il n'existe pas déjà.

La logique de validation vit dans `apply_setup`, sans dépendance à Tk, pour
être testable ; `run_setup` n'est que la fenêtre.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from config import APP_ID, Settings, expand_path, install_dir

__all__ = [
    "CHANNELS",
    "apply_setup",
    "needs_setup",
    "run_setup",
    "write_env_value",
]

logger = logging.getLogger(f"{APP_ID}.setup")

#: Canaux proposés : libellé affiché → valeur de `browser.channel`.
CHANNELS: tuple[tuple[str, str], ...] = (
    ("Automatique (Chromium embarqué, sinon Edge)", ""),
    ("Microsoft Edge", "msedge"),
    ("Google Chrome", "chrome"),
)
_ALLOWED_CHANNELS = frozenset(value for _label, value in CHANNELS)
_MIN_PASSPHRASE = 8


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
    if key:
        if settings.llm.api_key_env:
            os.environ[settings.llm.api_key_env] = key
        updates.append(f"clé {settings.llm.api_key_env or 'API'} enregistrée")
        target_env = Path(env_path) if env_path else install_dir() / ".env"
        try:
            write_env_value(target_env, settings.llm.api_key_env or "OMNIAGENT_API_KEY", key)
            updates.append(f"clé écrite dans {target_env}")
        except OSError as exc:
            errors.append(f"impossible d'écrire {target_env} : {exc} (clé gardée en mémoire)")
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
            self.title("Configuration initiale — OmniAgent")
            self.geometry("620x520")
            self.resizable(False, False)
            self.transient(parent)  # type: ignore[arg-type]
            self.protocol("WM_DELETE_WINDOW", self._skip)

            title_font = (settings.ui.font_family, settings.ui.font_size + 2, "bold")
            ctk.CTkLabel(
                self,
                text="Bienvenue : quatre réglages avant de commencer",
                font=title_font,
                anchor="w",
            ).pack(fill="x", padx=18, pady=(16, 2))
            ctk.CTkLabel(
                self,
                text=(
                    "Ces valeurs sont enregistrées localement. La clé va dans .env, "
                    "jamais dans config.json."
                ),
                text_color="#9CA3AF",
                anchor="w",
            ).pack(fill="x", padx=18, pady=(0, 10))

            form = ctk.CTkFrame(self)
            form.pack(fill="both", expand=True, padx=18, pady=4)
            form.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(form, text="Clé API", anchor="w").grid(
                row=0, column=0, sticky="w", padx=10, pady=8
            )
            self.key_entry = ctk.CTkEntry(form, show="•", placeholder_text="sk-…")
            self.key_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

            ctk.CTkLabel(form, text="Dossier de travail", anchor="w").grid(
                row=1, column=0, sticky="w", padx=10, pady=8
            )
            default_root = settings.files.allowed_roots[0] if settings.files.allowed_roots else ""
            self.dir_entry = ctk.CTkEntry(form, placeholder_text=default_root)
            self.dir_entry.grid(row=1, column=1, sticky="ew", padx=(10, 6), pady=8)
            ctk.CTkButton(form, text="Parcourir…", width=110, command=self._pick_dir).grid(
                row=1, column=2, sticky="e", padx=(0, 10), pady=8
            )

            ctk.CTkLabel(form, text="Navigateur", anchor="w").grid(
                row=2, column=0, sticky="w", padx=10, pady=8
            )
            current = settings.browser.channel
            labels = [label for label, value in CHANNELS]
            values = [value for _label, value in CHANNELS]
            start = labels[values.index(current)] if current in values else labels[0]
            self.channel_menu = ctk.CTkOptionMenu(form, values=labels)
            self.channel_menu.set(start)
            self.channel_menu.grid(row=2, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

            vault_hint = (
                "Coffre existant : laissez vide pour le conserver"
                if settings.paths.vault_file.is_file()
                else "Crée le coffre-fort des identifiants (optionnel)"
            )
            ctk.CTkLabel(form, text="Phrase du coffre", anchor="w").grid(
                row=3, column=0, sticky="w", padx=10, pady=8
            )
            self.pass_entry = ctk.CTkEntry(form, show="•", placeholder_text=vault_hint)
            self.pass_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

            ctk.CTkLabel(form, text="Confirmation", anchor="w").grid(
                row=4, column=0, sticky="w", padx=10, pady=8
            )
            self.confirm_entry = ctk.CTkEntry(form, show="•")
            self.confirm_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=10, pady=8)

            self.message = ctk.CTkLabel(self, text="", text_color="#F87171", anchor="w", wraplength=580)
            self.message.pack(fill="x", padx=18, pady=(4, 0))

            buttons = ctk.CTkFrame(self)
            buttons.pack(fill="x", padx=18, pady=(4, 16))
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
