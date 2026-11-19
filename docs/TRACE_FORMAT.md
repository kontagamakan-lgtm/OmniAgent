# Format des traces (version 3) et index des runs

Ce document décrit ce que les lecteurs (`core.trace.read_events`,
`core.replay`, `core.trace_lint`, `core.trace_rotate`) considèrent comme une
trace valide, et comment `runs.index` suit les fichiers. Toute évolution de
forme doit incrémenter `TRACE_FORMAT_VERSION` (`config.py`) et mettre ce
document à jour.

## Fichiers

- Un fichier par exécution : `<run_id>.jsonl`, ou `<run_id>.jsonl.gz` quand la
  rétention archive au lieu de supprimer.
- Un enregistrement JSON par ligne, UTF-8, terminé par `\n`.
- Une ligne **sans saut de ligne final** est une coupure d'écriture, pas une
  corruption : les lecteurs ignorent cette dernière ligne partielle.

## Ligne

```json
{"run_id": "...", "timestamp": "2026-11-19T10:00:00+00:00", "type": "tool_started", "seq": 4, "payload": {}}
```

| Champ | Rôle |
|---|---|
| `run_id` | Identifiant de l'exécution, identique sur toutes les lignes. |
| `timestamp` | ISO 8601 UTC, heure de l'événement. |
| `type` | Type d'événement (voir ci-dessous). |
| `seq` | Numéro strictement croissant dans le fichier, commence à 1. |
| `payload` | Objet JSON propre au type (chiffré au repos si activé). |
| `enc` | Enveloppe `{"v", "nonce", "ct"}` quand le chiffrement est actif. |
| `meta` | Sur `run_started` chiffré uniquement : version, modèle, espace de travail, format — de quoi reconstruire l'index sans la clé. |

### Manifeste (`run_started.payload`)

`task` (500 premiers caractères), `max_steps`, `app_version`, `trace_format`,
`model`, `backend`, `budget_tokens`, `settings_digest` (16 hexadécimaux, sans
`paths`), `workspace`, `python`. Aucun secret : le condensat couvre les réglages,
pas leur contenu, et la clé d'API n'y figure jamais.

### Types connus

| Type | Payload utile |
|---|---|
| `run_started` | manifeste ci-dessus |
| `step_started` | `step` |
| `llm_request` | `step`, `messages`, `tools` (mode contenu) |
| `llm_response` | `step`, `text`, `tool_calls`, `finish_reason` |
| `llm_retry` | `attempt`, `delay_s`, `error` |
| `stream_chunk` | `step`, `text` (jamais en mode métadonnées) |
| `context_trimmed` | `dropped`, `kept`, `tokens_before`, `tokens_after` |
| `tool_started` / `tool_finished` | `step`, `tool`, `arguments` / `ok`, `error_code`, `duration_ms` |
| `thought` | `step`, `text` |
| `final` | `text` |
| `error` | `code`, `message` |
| `run_finished` | `stop_reason`, `ok`, `tool_calls`, `recoveries`, `summary` |
| `trace_truncated` | écrit par la trace elle-même quand `trace.max_bytes` coupe l'écriture |

`summary` contient `duration_s`, `steps`, `tool_calls`, `recoveries`,
`requests`, `prompt_tokens`, `completion_tokens`, `trims`, `trimmed_tokens`,
`trim_cache_hits`.

## Chiffrement

AES-256-GCM, une enveloppe par ligne. Le `payload` seul est chiffré ; les
données authentifiées additionnelles lient `run_id`, `seq` et `type`, donc une
ligne déplacée d'un run à l'autre échoue au déchiffrement. Sans clé valide,
aucune trace n'est écrite (pas de repli en clair). `trace_lint` et `replay`
exigent la clé pour une trace chiffrée.

## Contrat vérifié par `core.trace_lint`

- un seul `run_started`, en première ligne, et un seul `run_id` ;
- `seq` strictement croissante ;
- chaque `tool_finished` répond à un `tool_started` de la même étape et du même
  outil ; un appel resté ouvert n'est toléré que sur une trace tronquée ;
- un terminal : `run_finished` (ou `trace_truncated` s'il n'y avait plus de
  place) ;
- format connu du lecteur (`trace_format` ≤ version courante).

Une ligne illisible en fin de fichier est un **avertissement**
(`unreadable_final_line`, coupure probable) ; au milieu, une **erreur**
(`unreadable_lines`, corruption).

## `runs.index`

Fichier latéral `<runs>/runs.index` (JSONL, une ligne par changement d'état),
jamais compté comme une trace car son nom n'est pas `*.jsonl`.

États : `started`, `finished`, `interrupted`, `archived` (les trois derniers
sont terminaux). Chaque ligne porte `run_id`, `state`, `path`, `started`,
`updated`, `app_version`, `model`, `workspace`, `trace_format`, `stop_reason`,
`ok`, `bytes`, `detail`.

Cycle de vie :

1. `run_started` → ligne `started` ;
2. `run_finished` → ligne `finished`, puis **compaction immédiate** ;
3. trace encore ouverte mais marqueur `.active` absent ou périmé au démarrage
   suivant → ligne `interrupted` (`scan_interrupted`) ;
4. trace archivée à la fermeture → ligne `finished` avec le chemin `.gz` ;
5. rétention → compaction après suppression/archivage.

Compaction : une seule ligne par exécution dont le fichier existe encore ;
états fusionnés (le plus récent fait foi, les champs du manifeste sont
conservés) ; lignes illisibles supprimées ; aucune réécriture si l'index est
déjà propre. Un index absent ou tronqué est un cache jetable :
`RunIndex.rebuild(reset=True)` le reconstruit depuis les fichiers, l'état étant
déduit de la fin du fichier (`run_finished`/`trace_truncated`, `.gz` compris).

Le marqueur `<run_id>.active` est rafraîchi pendant l'écriture ; sa fraîcheur
(`trace.active_ttl_s`) distingue une exécution vivante d'un run tué. Il est
retiré à la fermeture.

## Outils

```bat
python -m core.trace_lint <trace> --json
python -m core.replay <trace> --check
python -m core.replay <trace> --diff <autre>
python -m core.replay <trace> --export rapport.json
python -m core.replay --index-compact [dossier]
python -m core.replay --index-rebuild [dossier]
```
