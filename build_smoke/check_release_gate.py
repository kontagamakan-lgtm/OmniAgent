"""Garde-fous exécutés avant de publier une release.

Deux règles que le workflow de publication énonçait sans les vérifier :

1. **Le tag décrit le contenu publié.** `APP_VERSION` vit dans `config.py`, et
   c'est lui qui nomme l'installateur (`OmniAgent-<version>-Setup.exe`). Rien
   n'empêchait de pousser `v1.0.3` sans toucher `config.py` : la release
   s'appelait `v1.0.3` et contenait des fichiers `1.0.2`. Le contrôle échoue
   quand `tag != "v" + APP_VERSION`, sauf `--force-version` explicite, qui
   laisse republier un tag historique dont la version a changé depuis.

2. **Le tag pointe sur un commit déjà validé.** Le commentaire du workflow
   disait « le tag doit pointer sur un commit déjà passé par la CI » : c'était
   une convention, pas une garde. Le contrôle interroge les check-runs du
   commit et exige que le check agrégé — le seul que la protection de `master`
   exige — soit terminé et réussi.

   Ce check n'existe pas encore quand le tag part juste après la fusion :
   l'agrégat du commit de fusion est créé quelques dizaines de secondes plus
   tard, puis court plusieurs minutes. Le premier passage de la 1.1.1 a échoué
   là-dessus. Le contrôle réinterroge donc l'API pendant
   `--wait-check` secondes (600 par défaut) tant que le check est absent ou en
   cours ; une conclusion déjà mauvaise (échec, annulation) échoue sans
   attendre, puisqu'une attente n'y changerait rien.

Usage :
    python build_smoke/check_release_gate.py --tag v1.0.2 \
        --repo kontagamakan-lgtm/OmniAgent --require-ci

`--tag` accepte aussi le nom de référence poussé (`GITHUB_REF_NAME`) ; le
commit est résolu par `git rev-parse`, dans le dépôt courant.

Le dépôt est lu dans `GITHUB_REPOSITORY` quand `--repo` est absent, et le jeton
dans `GH_TOKEN` / `GITHUB_TOKEN`. Sans jeton, `--require-ci` échoue
explicitement plutôt que de supposer que la CI est verte.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.py"
DEFAULT_REQUIRED_CHECK = "Vérifications obligatoires"
DEFAULT_WAIT_S = 600.0
_POLL_INTERVAL_S = 15.0
_ATTEMPTS = 3
_BACKOFF_S = 2.0
# Conclusions qui ne changeront plus : attendre ne sert à rien.
_FINAL_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "stale",
    "startup_failure",
}

_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)


class GateError(RuntimeError):
    """Une règle de publication n'est pas satisfaite."""


def parse_app_version(text: str) -> str:
    """Version déclarée par `config.py`, sans exécuter le module."""
    match = _VERSION_RE.search(text)
    if match is None:
        raise GateError('APP_VERSION introuvable dans config.py')
    version = match.group(1).strip()
    if not version:
        raise GateError("APP_VERSION est vide dans config.py")
    return version


def expected_tag(version: str) -> str:
    """Tag attendu pour une version applicative."""
    return f"v{version}"


def check_tag_version(tag: str, version: str, *, force: bool = False) -> str:
    """Lève si le tag ne correspond pas à la version, sauf `force`."""
    expected = expected_tag(version)
    if tag == expected or force:
        return expected
    raise GateError(
        f"le tag « {tag} » ne correspond pas à APP_VERSION={version} : "
        f"attendu « {expected} ». Publier ainsi produirait une release "
        f"« {tag} » contenant des fichiers {version}. "
        "Utilisez --force-version pour republier un tag historique volontairement."
    )


def resolve_commit(tag: str, repo_root: Path = ROOT) -> str:
    """SHA du commit pointé par un tag annoté ou léger."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GateError(f"impossible de résoudre le tag {tag} : {error}") from error
    sha = completed.stdout.strip()
    if completed.returncode != 0 or not sha:
        detail = completed.stderr.strip()[:200] or "aucune sortie"
        raise GateError(f"tag introuvable localement : {tag} ({detail})")
    return sha


def evaluate_check_runs(
    payload: dict[str, object], required: str = DEFAULT_REQUIRED_CHECK
) -> str:
    """Vérifie que `required` est terminé et réussi parmi les check-runs."""
    raw = payload.get("check_runs")
    if not isinstance(raw, list) or not raw:
        raise GateError(
            "aucun check-run pour ce commit : le tag pointe sur un commit que "
            "la CI n'a jamais vu."
        )
    found: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        status = str(item.get("status", "")).strip()
        conclusion = str(item.get("conclusion") or "").strip()
        found[name] = conclusion if status == "completed" else status or "inconnu"
    state = found.get(required)
    if state is None:
        raise GateError(
            f"le check « {required} » est absent des check-runs "
            f"({', '.join(sorted(found)) or 'aucun'})."
        )
    if state != "success":
        raise GateError(f"le check « {required} » n'est pas réussi : {state}.")
    return state


def _still_pending(payload: dict[str, object], required: str) -> bool:
    """Vrai tant que l'état du check exigé peut encore changer."""
    raw = payload.get("check_runs")
    if not isinstance(raw, list):
        return True
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).strip() != required:
            continue
        if str(item.get("status", "")).strip() != "completed":
            return True
        conclusion = str(item.get("conclusion") or "").strip()
        return conclusion not in _FINAL_CONCLUSIONS
    # Aucun check-run, ou pas encore celui qu'on attend : la CI peut encore
    # le créer (c'est le cas juste après la poussée du tag).
    return True


def await_required_check(
    fetch: Callable[[], dict[str, object]],
    required: str = DEFAULT_REQUIRED_CHECK,
    *,
    timeout_s: float = DEFAULT_WAIT_S,
    interval_s: float = _POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Interroge les check-runs jusqu'à ce que `required` ait conclu.

    Renvoie la dernière réponse de `fetch` quand le check est réussi ; lève
    `GateError` sur une conclusion définitive ou une fois `timeout_s` écoulé.
    `fetch`, `clock` et `sleep` sont injectés pour rendre l'attente testable.
    """
    deadline = clock() + max(0.0, timeout_s)
    tries = 0
    last = ""
    while True:
        payload = fetch()
        tries += 1
        try:
            evaluate_check_runs(payload, required)
            return payload
        except GateError as error:
            last = str(error)
            if not _still_pending(payload, required):
                raise
        remaining = deadline - clock()
        if remaining <= 0:
            raise GateError(
                f"{last} (après {tries} essais sur {timeout_s:.0f} s d'attente ; "
                "augmentez --wait-check si la CI est plus lente)"
            )
        pause = min(interval_s, remaining)
        if log is not None:
            log(f"attente : {last} — nouvel essai dans {pause:.0f} s")
        sleep(pause)


def fetch_check_runs(repo: str, sha: str, token: str) -> dict[str, object]:
    """Check-runs du commit, avec quelques essais sur incident réseau."""
    url = f"{API}/repos/{repo}/commits/{sha}/check-runs?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "omniagent-release-gate",
    }
    last = ""
    for attempt in range(1, _ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise GateError(
                f"l'API des check-runs a répondu {error.code} pour {sha[:7]} : {body}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = str(error)
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_S * attempt)
                continue
            raise GateError(f"impossible de joindre l'API GitHub : {last}") from error
        if not isinstance(payload, dict):
            raise GateError(f"réponse inattendue de l'API : {payload!r:.200}")
        return payload
    raise GateError(f"impossible de joindre l'API GitHub : {last}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="tag à publier (ex. v1.0.2)")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "").strip(),
        help="dépôt GitHub (défaut : GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--require-ci",
        action="store_true",
        help="exiger que le commit du tag ait le check agrégé réussi",
    )
    parser.add_argument(
        "--required-check",
        default=DEFAULT_REQUIRED_CHECK,
        help=f"nom du check exigé (défaut : {DEFAULT_REQUIRED_CHECK})",
    )
    parser.add_argument(
        "--force-version",
        action="store_true",
        default=os.environ.get("OMNIAGENT_FORCE_VERSION", "").strip() in {"1", "true", "oui"},
        help="autoriser un tag qui ne correspond pas à APP_VERSION "
        "(ou OMNIAGENT_FORCE_VERSION=1)",
    )
    parser.add_argument(
        "--sha",
        default="",
        help="commit à vérifier (défaut : celui du tag, résolu par git rev-parse)",
    )
    parser.add_argument(
        "--wait-check",
        type=float,
        default=DEFAULT_WAIT_S,
        metavar="SECONDES",
        help="attente maximale du check agrégé, absent ou en cours "
        f"(défaut : {DEFAULT_WAIT_S:.0f})",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG),
        help="chemin de config.py (défaut : celui du dépôt)",
    )
    args = parser.parse_args(argv[1:])

    try:
        version = parse_app_version(Path(args.config).read_text(encoding="utf-8"))
        expected = check_tag_version(args.tag, version, force=args.force_version)
        print(
            f"OK : tag {args.tag} cohérent avec APP_VERSION={version}"
            + (" (forcé)" if args.force_version else "")
            + ("" if args.tag == expected else f" ; attendu {expected}")
        )
        if args.require_ci and os.environ.get("OMNIAGENT_SKIP_CI_CHECK", "").strip() in {
            "1",
            "true",
            "oui",
        }:
            # Republier un tag historique dont le commit a été validé par une
            # CI qui ne connaissait pas encore le check agrégé. Explicitement
            # demandé, jamais déduit.
            print("ATTENTION : contrôle de CI explicitement ignoré.")
        elif args.require_ci:
            if not args.repo:
                raise GateError(
                    "dépôt inconnu : renseignez --repo ou GITHUB_REPOSITORY."
                )
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
            if not token:
                raise GateError(
                    "aucun jeton : renseignez GH_TOKEN pour vérifier la CI."
                )
            sha = args.sha.strip() or resolve_commit(args.tag)
            payload = await_required_check(
                lambda: fetch_check_runs(args.repo, sha, token),
                args.required_check,
                timeout_s=args.wait_check,
                log=lambda message: print(message, flush=True),
            )
            state = evaluate_check_runs(payload, args.required_check)
            print(
                f"OK : {args.tag} -> {sha[:7]}, check « {args.required_check} » "
                f"{state}."
            )
    except GateError as error:
        print(f"ECHEC : {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
