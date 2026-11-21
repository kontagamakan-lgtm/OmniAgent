"""Vérifie que GitHub a réellement enregistré le workflow de publication.

Un fichier de workflow présent dans le dépôt ne suffit pas : GitHub ne
l'indexe qu'à l'occasion d'un push qui le modifie. Un workflow déclenché
uniquement par un tag reste alors invisible dans la liste, aucun tag ne le
lance, et rien ne le signale — ce silence a déjà empêché une publication
entière (le fichier était sur `master`, le workflow n'existait pas pour
GitHub).

Ce contrôle interroge l'API des workflows et échoue si celui de publication
n'y figure pas, ou s'il n'est pas actif.

Usage :
    GH_TOKEN=... python build_smoke/check_publication_workflow.py

Le dépôt est lu dans `GITHUB_REPOSITORY` (fourni par Actions) et peut être
passé en argument. `OMNIAGENT_EXPECTED_WORKFLOW` permet de viser un autre
chemin, ce qui sert à vérifier que le contrôle échoue bien quand il le doit.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_PATH = ".github/workflows/release.yml"
_ATTEMPTS = 3
_BACKOFF_S = 2.0


def _workflows(repo: str, token: str) -> list[dict[str, object]]:
    """Liste des workflows enregistrés, avec quelques essais sur incident réseau."""
    url = f"{API}/repos/{repo}/actions/workflows?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "omniagent-ci",
    }
    last: str = ""
    for attempt in range(1, _ATTEMPTS + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:300]
            raise SystemExit(
                f"ECHEC : l'API des workflows a répondu {error.code} pour {repo} : {body}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = str(error)
            if attempt < _ATTEMPTS:
                time.sleep(_BACKOFF_S * attempt)
                continue
            raise SystemExit(
                f"ECHEC : impossible de joindre l'API GitHub : {last}"
            ) from error
        workflows = payload.get("workflows")
        if not isinstance(workflows, list):
            raise SystemExit(f"ECHEC : réponse inattendue de l'API : {payload!r:.200}")
        return [item for item in workflows if isinstance(item, dict)]
    raise SystemExit(f"ECHEC : impossible de joindre l'API GitHub : {last}")


def main(argv: list[str]) -> int:
    repo = (
        argv[1]
        if len(argv) > 1
        else os.environ.get("GITHUB_REPOSITORY", "").strip()
    )
    if not repo:
        print(
            "ECHEC : dépôt inconnu ; GITHUB_REPOSITORY est absent et aucun "
            "argument ne l'a fourni.",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print(
            "ECHEC : aucun jeton ; renseignez GH_TOKEN pour interroger l'API.",
            file=sys.stderr,
        )
        return 2
    expected = os.environ.get("OMNIAGENT_EXPECTED_WORKFLOW", DEFAULT_PATH)

    workflows = _workflows(repo, token)
    for workflow in workflows:
        if str(workflow.get("path", "")).strip() != expected:
            continue
        state = str(workflow.get("state", "")).strip()
        if state != "active":
            print(
                f"ECHEC : le workflow {expected} est enregistré mais son état est "
                f"« {state} » ; un tag ne le lancerait pas.",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK : {workflow.get('name')} enregistré ({workflow.get('id')}), "
            f"chemin {expected}, état {state}."
        )
        return 0

    known = ", ".join(sorted(str(item.get("path", "")) for item in workflows)) or "aucun"
    print(
        f"ECHEC : {expected} n'est pas enregistré par GitHub (workflows connus : "
        f"{known}). Un workflow déclenché seulement par un tag n'est indexé qu'à "
        "l'occasion d'un push qui modifie ce fichier : modifiez-le puis poussez.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
