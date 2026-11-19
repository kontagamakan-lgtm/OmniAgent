# Opérations: sauvegarde, planification, reprise

## Sauvegarde et restauration

```bat
set OMNIAGENT_BACKUP_PASSPHRASE=une phrase longue
python app.py --backup
python app.py --restore data\backups\omniagent-....ombak
python app.py --restore ... --apply-restore
```

Sans `OMNIAGENT_BACKUP_PASSPHRASE`, l'archive est un ZIP clair (le message le
dit). Avec la variable, l'archive est scellée en AES-256-GCM (PBKDF2-SHA256,
200 000 tours). `--restore` vérifie toujours avant d'écrire : manifeste, chemins
sans `..`, index JSONL, traces relisibles, format de trace connu. `--apply-restore`
est la seule façon d'écrire.

## Rotation de clé des traces

```bat
set OMNIAGENT_TRACE_KEY=<ancienne>
set OMNIAGENT_TRACE_KEY_NEW=<nouvelle>
python -m core.replay --rotate-key --dry-run
python -m core.replay --rotate-key --key-backup data\ancienne-cle.scellee
```

`--dry-run` ne réécrit rien. La rotation vérifie ensuite chaque trace avec la
nouvelle clé ; `--key-backup` scelle l'ancienne clé avec
`OMNIAGENT_BACKUP_PASSPHRASE` le temps de la transition. Sans clé valide au
démarrage, `--check` signale que les traces chiffrées ne seront pas écrites.

## Mode autonome planifié

`data/autonomy/schedule.json` :

```json
{"start_hour": 1, "end_hour": 6, "min_idle_s": 300,
 "require_ac": true, "check_interval_s": 60}
```

```bat
python app.py --autonomy-schedule
python app.py --autonomy-schedule --schedule-checks 1
```

Une session au plus par fenêtre (l'historique est `autonomy/sessions.jsonl`).
`--schedule-checks` borne l'attente et sert aux essais. Aucune notification
système n'est envoyée : la sortie console et l'historique en tiennent lieu.

## Politique d'outils

`data/autonomy/policy.json` autorise des outils dangereux au cas par cas (la
première règle qui correspond décide, sinon refus) :

```json
{"rules": [
  {"tool": "fs_write", "action": "allow", "args": {"path": ["reports/"]}, "max_calls": 5},
  {"tool": "shell_run", "action": "allow", "args": {"command": ["dir"]}},
  {"tool": "*", "action": "deny"}
]}
```

Chaque décision est journalisée dans `autonomy/journal.jsonl`.
`max_calls` est un plafond par outil pour la session ; atteint, il refuse sans
chercher une règle plus permissive.

## Arrêt d'urgence

Le fichier `data/autonomy/STOP` arrête toute session au prochain événement ; le
bouton **Arrêt d'urgence** (Ctrl+Alt+S) l'écrit et annule immédiatement un run
en cours. Il n'est jamais supprimé automatiquement : retirez-le avant de
relancer une session.

## Reprise d'une trace interrompue

```bat
python app.py --resume data\logs\runs\<run>.jsonl
python app.py --resume ... --resume-apply
```

L'aperçu classe les appels déjà faits. Aucun outil de la trace n'est rejoué :
le contexte reconstruit est transmis au modèle, qui doit vérifier l'état avant
d'agir. Une trace terminée normalement n'a pas besoin de reprise.

## Migration v2 vers v3 et export

```bat
python -m core.replay --migrate --migrate-apply
python -m core.replay --export bundle.json
python -m core.replay --import-bundle bundle.json
```

La migration crée une copie `.v2.bak` avant réécriture et refuse un format plus
récent. Le schéma d'export est `docs/export.schema.json`.
