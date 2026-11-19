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
`--schedule-checks` borne l'attente et sert aux essais. À la fin de chaque
session, une toast Windows native est affichée (« OmniAgent — session
planifiée ») via PowerShell/WinRT, sans paquet supplémentaire ;
`autonomy.notify_on_session = false` la désactive. La sortie console et
l'historique restent la trace durable.

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

## Contrat des capacités des outils

`tools/capabilities.py` est l'unique table de classification : pour chaque
outil, elle dit s'il **écrit** (sinon il lit), s'il est **dangereux** (donc
confirmé, et refusé en autonomie sans règle de politique) et s'il manipule des
**secrets** (résultat et arguments retenus hors du prompt et de la trace).

`build_agent` valide cette table contre les outils réellement construits et
lève `ToolContractError` au démarrage si un outil n'est pas classé ou si ses
drapeaux ne correspondent plus à sa déclaration : un oubli ne peut donc pas
hériter silencieusement du défaut `read_only=True`. Ajouter une famille
d'outils implique d'ajouter ses entrées dans cette table ; le test
`tests/test_capabilities.py` vérifie aussi qu'aucune entrée n'est orpheline.

## Arrêt d'urgence

Le fichier `data/autonomy/STOP` arrête toute session au prochain événement.
Le bouton **Arrêt d'urgence** l'écrit et annule immédiatement un run en cours.
**Ctrl+Alt+S est enregistré globalement** (`RegisterHotKey`) tant que
l'application tourne : il fonctionne même quand une autre fenêtre a le focus.

La combinaison se règle dans `ui.emergency_hotkey` (ou
`OMNIAGENT_UI_EMERGENCY_HOTKEY`) ; `Ctrl+Alt+S` reste le défaut :

```json
{"ui": {"emergency_hotkey": "Ctrl+Shift+F9"}}
```

Un modificateur au moins est exigé (`Ctrl`, `Alt`, `Shift`, `Win`, alias
`Control`/`Windows`), suivi d'une lettre, d'un chiffre ou d'une touche nommée
(`F1`-`F24`, `Space`, `Tab`, `Enter`, `Esc`, `Delete`, `Insert`, `Home`,
`End`, `PageUp`, `PageDown`, `Pause`, `PrintScreen`). Les alias et la casse
sont normalisés : `alt+control+f9` devient `Ctrl+Alt+F9`. Une valeur invalide
bloque le démarrage (`python config.py --validate`).

Si une autre application possède déjà la combinaison, l'application le signale
au démarrage avec le libellé exact et le raccourci de fenêtre reste
disponible. Le fichier n'est jamais supprimé automatiquement : retirez-le avant
de relancer une session.

## Reprise d'une trace interrompue

```bat
python app.py --resume data\logs\runs\<run>.jsonl
python app.py --resume ... --resume-apply
```

**Dérive des lectures.** Pour `fs_read_text`, `fs_hash` et `ocr_image`, la
trace enregistre une empreinte légère du fichier : taille et SHA-256 des
premiers 64 Kio. À la reprise, l'empreinte est recalculée et comparée ; le
modèle reçoit `contenu identique a la lecture d'origine`, `contenu modifie
depuis la trace` ou `fichier disparu depuis la trace`, avec la consigne de ne
se fier qu'à l'observation fraîche. Une trace antérieure sans empreinte ne
produit aucune conclusion. Une réécriture à taille constante au-delà des
64 Kio peut échapper à l'empreinte : elle signale une dérive probable, elle ne
prouve pas l'identité du contenu.

L'aperçu classe les appels déjà faits. Avec `--resume-apply`, les appels de
lecture seule (`fs_read_text`, `fs_list`, `web_search`, etc.) sont réellement
rejoués avant le premier appel au modèle, et leurs observations fraîches sont
ajoutées au contexte. Tout outil à effet de bord — écriture, shell, navigateur,
coffre en modification — est refusé et jamais exécuté ; un outil inconnu l'est
aussi, faute de preuve qu'il est sans effet. Une trace terminée normalement n'a
pas besoin de reprise.

## Migration v2 vers v3 et export

```bat
python -m core.replay --migrate --migrate-apply
python -m core.replay --export bundle.json
python -m core.replay --import-bundle bundle.json
```

La migration crée une copie `.v2.bak` avant réécriture et refuse un format plus
récent. Le schéma d'export est `docs/export.schema.json`.
