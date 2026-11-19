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
 "require_ac": true, "check_interval_s": 60,
 "catch_up": "latest", "catch_up_hours": 18}
```

```bat
python app.py --autonomy-schedule
python app.py --autonomy-schedule --schedule-checks 1
python app.py --schedule-install
python app.py --schedule-uninstall
```

Une session au plus par fenêtre (l'historique est `autonomy/sessions.jsonl`).

**Rattrapage.** Hors fenêtre, `catch_up = "latest"` autorise une seule
session de rattrapage pour la dernière fenêtre terminée, si elle s'est finie
il y a moins de `catch_up_hours` heures ; `"none"` l'interdit. La fenêtre
rattrapée est enregistrée avec son propre identifiant et l'indicateur
`catch_up`, donc elle ne peut pas repartir : le verrou anti-double exécution
reste `last_slot`.

**Tâche Windows (facultative).** `--schedule-install` (ou le bouton
**Planificateur** de l'interface, qui propose aussi **Désinscrire**) écrit
`cache/omniagent-task.xml` puis appelle
`schtasks /Create /TN "OmniAgent - Session planifiee" /XML ... /F`. La tâche
se déclenche à l'ouverture de session et à l'heure de début de fenêtre, avec
répétition horaire pendant la fenêtre et `WakeToRun` pour le réveil ;
`StartWhenAvailable` la relance après une veille ou un poste éteint, et c'est
`core.schedule` qui décide si le rattrapage est encore acceptable.
`MultipleInstancesPolicy=IgnoreNew` empêche deux instances de la tâche de se
chevaucher. La désinscription (`--schedule-uninstall` ou le bouton) appelle
`schtasks /Delete /TN ... /F`. Aucune tâche n'est créée sans cette demande
explicite, et sous un autre système que Windows les deux commandes refusent
sans rien exécuter.

Le test d'intégration réel est lui aussi opt-in : sous Windows, avec
`OMNIAGENT_TASK_INTEGRATION=1`, `tests/test_taskscheduler_integration.py`
enregistre une tâche à nom unique, relit sa définition via
`schtasks /Query /XML`, puis la désinscrit et vérifie qu'elle a disparu. Sans
cette variable (ou hors Windows), le test est ignoré et la suite ne touche
jamais au Planificateur.
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

Chaque entrée déclare aussi sa **portée** (fichiers, coffre, web, navigateur,
shell, index, OCR), son accès **réseau** et son droit de **rejeu**. La liste
blanche de `core.resume` est calculée depuis ce manifeste : elle n'est plus
recopiée à la main, ce qui avait laissé `fs_read` (nom disparu) dans l'ancienne
liste.

`build_agent` valide cette table contre les outils réellement construits et
lève `ToolContractError` au démarrage si un outil n'est pas classé ou si ses
drapeaux ne correspondent plus à sa déclaration : un oubli ne peut donc pas
hériter silencieusement du défaut `read_only=True`. Ajouter une famille
d'outils implique d'ajouter ses entrées dans cette table ; le test
`tests/test_capabilities.py` vérifie aussi qu'aucune entrée n'est orpheline.
`python -m tools.capabilities` affiche la table, aussi disponible dans
`docs/TOOLS.md`.

## Approbation au niveau de l'outil

Avant tout outil à effet, l'interface affiche l'aperçu exact : diff pour
`fs_write_text`, arborescence et taille pour `fs_delete`, commande pour
`shell_run`, hôte pour le web. La décision vaut **une fois**, **pour la
session** (même outil, sans nouvelle demande) ou **refus** ; un refus, une
fenêtre fermée ou une panne refuse par défaut, et l'outil n'est jamais
dispatché avant la décision. Chaque décision est journalisée dans
`data/approvals.jsonl` (outil, portée, décision, empreinte des arguments), sans
contenu de fichier ni secret.

Mode dans `config.json` : `approvals.mode` = `dangerous` (défaut, seuls les
outils destructeurs), `write` (toute écriture), `all` ou `none` ;
`approvals.session_grants` désactive l'autorisation de session. En CLI, un
confirmateur booléen est adapté une fois à la construction (`decision_from_confirm`)
puis vaut « une fois » ; la porte n'appelle plus que `decide(request)`. Le mode
autonome continue de refuser
les outils dangereux sans règle de politique.

## Budget global

`data/budget/ledger.jsonl` garde un enregistrement par run (tokens, coût
estimé) : le cumul survit aux redémarrages. `budget.daily_cost`,
`weekly_cost`, `monthly_cost`, `daily_tokens` et `monthly_tokens` fixent les
plafonds (0 = aucun). Le budget est vérifié avant le run et avant chaque appel
au modèle ; un dépassement arrête avec `stop_reason = "budget"` sans envoyer la
requête suivante. L'interface affiche la jauge du jour et la marge restante.

```bat
python app.py --budget
```

## Mémoire projet

Les notes durables vivent dans `data/memory/notes.json` : texte, portée,
confiance, date, expiration facultative et **source** avec une empreinte. Seules
les notes actives sont injectées dans le prompt, avec leur provenance. Si la
source change, la note passe `stale` : elle n'est plus présentée comme une
vérité, mais reste consultable avec sa provenance.

```bat
python -m core.memory --list
python -m core.memory --add "les rapports vont dans rapports/" --source docs\conventions.md --confidence 0.9 --ttl-days 90
python -m core.memory --edit <id> --text "nouvelle formulation" --source docs\conventions.md
python -m core.memory --delete <id>
python -m core.memory --forget   :: supprime expirées et sources disparues
```

## Isolation par run

`isolation.profile` (ou `--profile`) choisit les familles d'outils exposées :
`read-only`, `files`, `files-web` ou `full`. Un outil hors profil n'est ni
proposé au modèle ni exécutable (`unknown_tool`), même si le modèle l'appelle.
`isolation.allowed_domains` restreint les hôtes des outils réseautés (sous-domaines
acceptés) ; chaque tentative est écrite dans `data/network.jsonl`.

```bat
python app.py --task "resume mes notes" --profile read-only
```

C'est une isolation au niveau des outils : un outil conservé garde les droits
du processus, ce n'est pas un bac à sable système.

## Journal d'audit

Chaque trace scellée enregistre dans `data/audit/chain.jsonl` le nombre de ses
enregistrements et la racine SHA-256 de leurs empreintes **dans l'ordre**,
reliée au scellement précédent. Une ligne modifiée, supprimée ou réordonnée est
détectée ; les décisions de politique sont chaînées de la même façon.

```bat
python app.py --audit-seal      :: migre les traces existantes
python app.py --verify-audit
```

Un attaquant qui possède la clé peut réécrire la trace et tout le journal :
recopiez régulièrement `chain.jsonl` hors de la machine pour un ancrage externe.

## Notifications Windows

```bat
python app.py --notify-install  :: raccourci du menu Demarrer + protocole omniagent://
python app.py --notify-action omniagent://trace/<run_id>
python app.py --notify-action omniagent://stop
```

Le raccourci porte l'AppUserModelID `OmniAgent.Desktop` ; les toasts de fin de
session proposent « Ouvrir la trace » et « Arrêter ». Si Windows refuse la
version avec boutons, une toast simple est affichée ; si PowerShell est
indisponible, la session continue et l'échec est journalisé.

## Répétition de restauration

```bat
python app.py --restore-drill data\backups\omniagent-....ombak
```

L'archive est d'abord inspectée (refus avant toute écriture), puis restaurée
dans `data/.restore-drill-*`, où les traces sont relues, la base passe
`PRAGMA integrity_check`, l'index et la configuration sont parsés. Le dossier
est supprimé ensuite ; les données vivantes ne sont jamais modifiées.
`--keep` conserve le dossier pour inspection.

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

## Transaction par run et annulation

Chaque exécution ouvre une transaction dans `data/transactions/<run_id>/` :
`journal.jsonl` note l'intention **avant** chaque mutation (écriture, copie,
déplacement, suppression, création, extraction, conversion), et `snapshots/`
ne contient que les fichiers réellement touchés, en-tête compris. Les outils de
lecture ne créent ni copie ni entrée.

```bat
python -m core.file_tx --list
python -m core.file_tx --undo <run_id>          :: apercu, aucune écriture
python -m core.file_tx --undo <run_id> --apply  :: restaure
python app.py --undo-run <run_id>               :: meme annulation via la CLI
python app.py --undo-run <run_id> --undo-apply
```

L'interface expose **Annuler ce run** : elle affiche l'aperçu du journal et ne
restaure qu'après confirmation.

L'annulation rejoue les intentions à l'envers : elle restaure les octets
d'origine, supprime ce que le run a créé et remet en place ce qu'il a déplacé.
Elle ne touche que les chemins du journal et refuse un chemin sorti des racines
enregistrées. L'instantané est plafonné (256 Mio par run) : au-delà,
l'intention reste notée mais l'annulation signale qu'elle ne peut pas
restaurer. Ce n'est pas une sauvegarde, seulement le retour arrière d'un run.

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
