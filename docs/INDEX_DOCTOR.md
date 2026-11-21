# Diagnostic de l'index des traces

`python -m core.replay --index-doctor [DOSSIER]` compare `runs.index` aux
traces présentes. `app.py --check` en affiche les points bloquants, et le
panneau Traces a un bouton **Diagnostic** qui inclut les remèdes ci-dessous.

`app.py --check --fix` exécute les réparations sûres : il ne supprime jamais une
trace, seulement des entrées d'index périmées et des marqueurs `.active`.

| Code | Cause | Remède |
| --- | --- | --- |
| `missing_directory` | aucune trace écrite | lancer une tâche ; le dossier sera créé |
| `missing_index` | traces sans index | `--index-rebuild` |
| `index_unreadable` | ligne d'index non JSON | `--index-rebuild` ou `--check --fix` |
| `index_duplicate` | plusieurs états pour un run | `--index-compact` ou `--check --fix` |
| `index_orphan` | entrée sans trace | `--index-compact` ou `--check --fix` |
| `index_stale_bytes` | taille notée obsolète | `--check --fix` |
| `index_stale_path` | chemin noté obsolète | `--check --fix` |
| `index_oversized` | index plus gros que les traces | `--index-compact` |
| `index_lost_state` | un état n'a pas pu être écrit après tous les essais | `--index-rebuild` (l'état se relit dans la trace) |
| `trace_not_indexed` | trace absente de l'index | `--index-rebuild` |
| `active_orphan` | `.active` sans trace | `--check --fix` supprime le marqueur |
| `active_stale` | marqueur périmé + trace sans fin | `--check --fix` marque `interrupted` |

Un index est un cache : sa reconstruction ne perd aucune trace. Un run dont le
fichier porte `run_finished` reste terminé, même si l'index avait disparu.

## États non écrits

Quand l'écriture de l'index échoue à tous les essais (fichier tenu par un
antivirus ou une sauvegarde), l'état visé est consigné dans `runs.index.lost`,
à côté de l'index. Le diagnostic le signale sous `index_lost_state` : c'est le
seul cas où l'index est *incomplet* plutôt que seulement en retard, et une
reconstruction le répare puisque la trace sur disque porte l'information.

Sans ce fichier, la perte n'existait que dans une ligne de log, que personne ne
relit après coup.
