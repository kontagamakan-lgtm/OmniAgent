# Opérations: sauvegarde, planification, reprise

## Limites connues et vérifications manuelles

Cette section est l'état de référence. Elle distingue les garanties logicielles
des vérifications qui n'ont pas été réalisées dans cet environnement ; les
détails d'implémentation et les procédures complètes restent dans les sections
indiquées.

- **Isolation (#11) - au niveau des outils, pas un bac à sable système.**
  Les profils filtrent les outils, mais **un outil conservé garde les droits du
  processus hôte** : il peut accéder à ce que le compte OmniAgent peut lire et
  lancer des processus avec la même identité. Ce sont des contrôles
  applicatifs, pas un jeton, un espace de noms ou une ACL système. Une
  séparation réelle exige un conteneur, AppContainer ou une machine virtuelle,
  non implémenté ici. Détails : « Isolation par run ».
- **Audit (#9) - chaîne falsifiable par le détenteur de la clé, ancrage
  externe requis.** Un attaquant qui possède la clé peut réécrire la trace et
  tout le journal puis recalculer les empreintes ; `--verify-audit` seul peut
  alors afficher une chaîne cohérente. La parade est une copie de
  `data/audit/chain.jsonl` vers un support externe contrôlé (clé USB ou partage
  réseau distinct), après chaque session autonome et au minimum une fois par
  jour ; `--audit-anchor` effectue la copie et
  `--verify-audit --audit-anchor` compare le préfixe conservé. Cela détecte
  une réécriture des entrées déjà copiées, mais ne protège pas si la
  destination est compromise ; ce n'est ni une signature ni une restauration
  du contenu des traces. Procédure complète : « Journal d'audit ».
- **Notifications Windows (#8) - raccourci, protocole et boutons vérifiés
  sous Windows.** Vérifié le 2026-11-19 : `--notify-install` a été exécuté deux
  fois (deux succès), le raccourci
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\OmniAgent.lnk` pointe vers
  `pythonw.exe` avec `app.py`, l'AUMID `OmniAgent.Desktop` est présent, et la
  clé `HKCU:\Software\Classes\omniagent\shell\open\command` lance désormais
  `pythonw.exe "…\app.py" --notify-action "%1"` (le script manquait avant
  correction). Le raccourci a été lancé depuis le menu Démarrer et a créé un
  `pythonw.exe`, arrêté après le contrôle. Une toast réelle avec les boutons
  `Ouvrir la trace` et `Arrêter` a été affichée ; un clic réel sur `Arrêter` a
  écrit `data/autonomy/STOP`, supprimé après l'essai. Un clic réel sur `Ouvrir
  la trace` a ouvert l'Explorateur sur `data/logs/runs` (trace jetable), sans
  créer `STOP` ; le raccourci a été relancé après réinstallation avec l'icône
  `assets/icon.ico` et a créé un `pythonw.exe`, arrêté ensuite.
- **Approbation (#6) - dialogue explicite et parcours réel testés.** Les trois
  choix sont `Autoriser une fois`, `Autoriser pour la session` et `Refuser` ;
  `Refuser` est le bouton par défaut, et fermer la fenêtre (ou Échap) refuse.
  Vérifié le 2026-11-19 : les trois clics et la fermeture ont été testés sur la
  boîte réelle (`once`/`session`/`deny`). Les tests
  `tests/test_approval.py::test_real_fs_delete_is_denied_then_authorized_once`
  et `::test_real_session_approval_reuses_the_grant_for_a_second_delete`
  utilisent le vrai `FileManager`, suppriment des fichiers jetables et vérifient
  `data/approvals.jsonl` ; ils ne passent pas par la fenêtre graphique, dont
  les clics sont vérifiés séparément. La capture
  `artifacts/visual/approval_dialog.png` montre les trois libellés.
- **Planificateur (#5) - test d'intégration réel opt-in, exécuté avec succès
  sous Windows.** Le test n'est pas lancé par défaut. Pour l'activer :
  `set FREEBUFF_SCHTASKS_INTEGRATION=1` puis
  `python -m pytest tests/test_taskscheduler_integration.py -vv` (PowerShell :
  `$env:FREEBUFF_SCHTASKS_INTEGRATION = "1"`). Il appelle le vrai
  `install_task()`, laisse `schtasks.exe` enregistrer une tâche jetable, relit
  sa définition avec `schtasks /Query /XML`, puis la supprime via
  `uninstall_task()` dans un `try/finally` et vérifie son absence. Exécuté le
  2026-11-19 sous Windows : **1 passed**, aucune tâche résiduelle. Les tests
  `tests/test_taskscheduler_subprocess.py` simulent seulement l'exécutable avec
  un faux `schtasks` en tête de `PATH` et ne prouvent pas l'enregistrement
  Windows réel.

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
`FREEBUFF_SCHTASKS_INTEGRATION=1`, `tests/test_taskscheduler_integration.py`
enregistre une tâche à nom unique via le vrai `install_task()`, relit sa
définition via `schtasks /Query /XML`, puis la désinscrit et vérifie qu'elle a
disparu. Sans cette variable (ou hors Windows), le test est ignoré avec la
raison explicite et la suite ne touche jamais au Planificateur. Le nettoyage
est dans un `try/finally` : même si la vérification échoue, la tâche est
désinscrite. Ce test a été exécuté explicitement sur cette machine le
2026-11-19 avec `FREEBUFF_SCHTASKS_INTEGRATION=1` : **1 passed**, et la requête
de contrôle n'a trouvé aucune tâche résiduelle. Il reste ignoré par défaut.

```powershell
$env:FREEBUFF_SCHTASKS_INTEGRATION = "1"
python -m pytest tests/test_taskscheduler_integration.py -q
# Vérifier ensuite qu'aucune tâche de test ne reste :
schtasks /Query /FO TABLE | Select-String "OmniAgent - Test integration"
```

Le test factice `tests/test_taskscheduler_subprocess.py` s'exécute dans la
suite normale, sans Windows : il place un faux `schtasks` en tête de `PATH`,
vérifie les arguments exacts de création/suppression, le code retour et le
message d'échec.
`--schedule-checks` borne l'attente et sert aux essais. À la fin de chaque
session, une toast Windows native est affichée (« OmniAgent - session
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

La boîte de dialogue affiche trois boutons explicites : `Autoriser une fois`,
`Autoriser pour la session` et `Refuser`. `Refuser` est sélectionné par défaut ;
fermer la fenêtre ou appuyer sur Échap refuse. Le parcours réel d'une
suppression, le refus et la réutilisation d'une autorisation de session sont
couverts par `tests/test_approval.py` ; les clics de la boîte sont vérifiés
séparément dans la section « Limites connues et vérifications manuelles ».

## Budget global

`data/budget/ledger.jsonl` garde un enregistrement par run (tokens, coût
estimé) : le cumul survit aux redémarrages. `budget.daily_cost`,
`weekly_cost`, `monthly_cost`, `daily_tokens` et `monthly_tokens` fixent les
plafonds (0 = aucun). Le budget est vérifié avant le run et avant chaque appel
au modèle ; un dépassement arrête avec `stop_reason = "budget"` sans envoyer la
requête suivante. L'interface affiche la jauge du jour et la marge restante.

Au démarrage, le coût estimé du run (**prompt connu + une sortie maximale**,
relevé au moins à la moyenne des runs terminés du mois) est **réservé
atomiquement** : lecture du registre, contrôle des plafonds et ajout de la
réservation se font sous le même verrou de fichier. Un run multi-étapes appelle
le modèle plusieurs fois ; sa réservation ne se limite donc pas à une seule
sortie. `budget.estimate_multiplier` (1 par défaut) ajoute une marge explicite
sur le plancher et cette moyenne. Deux runs simultanés ne peuvent pas passer
sur le même reste de budget : le second est refusé avant tout appel au modèle.
À la fin, la réservation est remplacée par le coût réel ; si le run s'arrête
sans aucun échange, elle est simplement libérée. Une
réservation orpheline (processus tué) expire au bout de six heures. Si le
registre est inaccessible, la réservation refuse plutôt que de dépenser à
l'aveugle.

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

## Cerveau Obsidian

Le vault `data/Obsidian/OmniAgent_Brain/` est la mémoire longue durée
Markdown. `00_CORTEX_INDEX.md` est régénéré après chaque note ; les outils
`obsidian_remember`, `obsidian_search` et `obsidian_status` permettent à
l'agent de l'enrichir et de le consulter. `obsidian_remember` accepte un
sous-dossier relatif personnalisé (`02_Knowledge/Projets/Atlas`) : il est créé
au besoin, apparaît dans `00_CORTEX_INDEX.md` et dans `obsidian_status`. Les
chemins absolus, les traversées `..`, les dossiers masqués, les noms réservés
Windows et les caractères interdits sont refusés, et le dossier résolu doit
rester dans le vault. Les lectures web, fichiers, OCR et
recherches documentaires sont transformées automatiquement en notes dans
`01_Sources/` ; une note de journal est ajoutée à la fin de chaque run.

Au premier lancement graphique, `obsidian.auto_install` lance si nécessaire
`winget install --id Obsidian.Obsidian --exact --silent`. Si winget est absent
ou échoue, l'installateur officiel de la dernière version est téléchargé depuis
la release GitHub `obsidianmd/obsidian-releases` (asset `Obsidian-<version>.exe`),
contrôlé (en-tête `MZ`, taille bornée à 512 Mio, lecture https uniquement),
puis sa signature Authenticode est vérifiée via PowerShell : statut `Valid`,
et CN et O exactement égaux à `Dynalist Inc` ou `Obsidian` (casse ignorée),
refus sinon - une simple sous-chaîne comme « Evil Obsidian Ltd » ne passe pas
(y compris si
PowerShell manque). L'installateur n'est exécuté avec `/S /currentuser`
qu'après cette vérification, et l'installation n'est déclarée réussie que si
`Obsidian.exe` est retrouvé. `OBSIDIAN_INSTALLER_URL` remplace l'API GitHub
par une URL https directe, notamment derrière un proxy. Si tout échoue, le
vault reste utilisable et l'application continue ; `python app.py
--install-obsidian` relance explicitement le bootstrap. Le `--check` affiche le
vault et la présence de l'application sans installer.

Les résultats sensibles (coffre, cookies) ne sont jamais mémorisés ; titre,
corps et source sont masqués par le `Redactor` avant écriture (source ramenée
sur une ligne pour ne pas injecter de frontmatter). Les dossiers cachés
(`.obsidian`, `.trash`) sont ignorés par l'index et la recherche, et une racine
de disque est refusée comme vault. Les fichiers du vault sont des données
utilisateur : ne les versionnez pas et ne les incluez pas dans un paquet.

## Auto-évolution bornée

`evolution.enabled = true` démarre un thread d'arrière-plan qui attend
`idle_minutes` d'inactivité, puis exécute au plus `max_cycles_per_day` cycles
par jour, espacés d'au moins `interval_minutes`. Les clics « Forcer un cycle »
sont plafonnés séparément à `max_forced_cycles_per_day` (3 par défaut, 0 les
désactive) et comptent aussi dans le total quotidien. Chaque cycle diagnostique
les outils (`registry.stats()`), cherche des pistes via `web_search`, forge une
compétence et écrit des leçons. La forge n'est tentée que si le diagnostic
signale un outil lent, en échec ou une erreur récente ; sans signal, elle est
ignorée (`forge_skipped`) et seules les leçons sont écrites. La version ne monte
qu'après un test de compétence réussi et un chargement effectif : un test en
échec laisse la version et le cortex inchangés.

Avant de démarrer, automatique ou forcé, le moteur appelle `core.budget.check()`
avide l'estimation de ses deux appels au modèle. Si les plafonds `budget.*`
(coût ou tokens, jour/semaine/mois) sont déjà atteints, le cycle est refusé,
journalisé `cycle_refused` et aucun token n'est dépensé. Un registre de budget
illisible refuse aussi (fail-closed).

Fichiers :

- `evolution_core/skills/*.py` : compétences chargées à chaud par
  `core/skill_loader.py` (module + `NAME`, `DESCRIPTION`, `PARAMETERS`,
  `run(**kwargs)`) ;
- `evolution_core/prompts/adaptations.md` : prompt d'adaptation, borné par
  `max_prompt_chars` ;
- `data/evolution/journal.jsonl` : cycle démarré, diagnostic, veille, forge,
  leçons, fin de cycle ;
- `data/evolution/state.json` : version (`v1.0.N - Auto-Générée`), cycles du
  jour et cycles forcés du jour (réinitialisés au changement de date UTC) ;
- `data/evolution/backups/<horodatage>/` : instantané de `evolution_core/`
  avant chaque forge ;
- `data/evolution/tests/` : tests générés, exécutés avec `test_timeout_s`.

Rollback : si le test échoue, le fichier de compétence est restauré depuis
l'instantané (ou supprimé s'il est nouveau) et l'échec est journalisé ; rien
n'est chargé. Un cycle raté n'arrête pas la boucle.

Sécurité : les écritures sont confinées à `evolution_core/` ; les imports
`subprocess`, `socket`, `ctypes`, `winreg`, `multiprocessing` et les appels
`eval`, `exec`, `compile`, `os.system`, `os.popen` sont refusés avant
chargement. `pip install` n'est possible qu'avec `allow_pip = true`, sur un nom
de paquet validé, avec `pip_timeout_s`. Le test en sous-processus isole les
crashs, pas un code malveillant : ne générez des compétences qu'avec un
fournisseur de confiance.

Panneau **Évolution** : version, compétences, journal, bouton « Forcer un cycle
maintenant » (l'outil `evolution_force_cycle` est `dangerous`, donc soumis à
approbation hors panneau).

## Préflight des clés DeepSeek

Quand le backend est `deepseek`, une clé est refusée avant tout appel réseau
si elle est absente, mal formée ou manifestement factice. Le contrôle est
partagé par `LLMConfig.api_key_problem()` et `LLMRouter.add_key()` : il
s'applique donc au client unique, à l'assistant de démarrage, au panneau de
diagnostics et aux clés ajoutées au routeur multi-LLM.

Règles :

- un préfixe d'un autre fournisseur (`sk-proj-`, `sk-svcacct-`, `sk-ant-`,
  `sk-or-`, `sk-mistral-`, `gsk_`, `AIza`) est refusé en le nommant : la clé
  n'irait pas loin sur `api.deepseek.com` ;
- la clé doit commencer par `sk-` ;
- elle doit contenir au moins **20 caractères** au total, préfixe compris ;
- les valeurs d'exemple sont refusées : les marqueurs `test`, `fake`, `dummy`,
  `example`, `placeholder`, `change-me`, `changeme`, `your-key`, `votre-cle`
  et `xxx` sont détectés sans tenir compte de la casse ;
- un corps composé d'un seul caractère répété après `sk-` est refusé, par
  exemple `sk-00000000000000000000` ou `sk-aaaaaaaaaaaaaaaaaaaa`.

Pour les autres fournisseurs, `config.api_key_problem()` ne refuse que le
vide, un espace interne, une longueur inférieure à 12 caractères et les
valeurs d'exemple : OpenAI (`sk-proj-`), Gemini (`AIza`), Groq (`gsk_`) et
Anthropic (`sk-ant-`) ont des formats incompatibles avec un préfixe unique, et
leur HTTP 401 est plus précis qu'une devinette locale.

Anthropic est servi par le client unique quand `llm.backend = "anthropic"` :
l'API Messages impose son endpoint `/messages`, la clé dans `x-api-key`, la
version `2023-06-01`, un prompt système séparé et des blocs `tool_use` /
`tool_result`. Le flux SSE est décodé comme chez les autres fournisseurs :
`content_block_delta` de type `text_delta` pousse chaque fragment à `on_token`,
un bloc `tool_use` s'annonce par `content_block_start` puis reconstitue ses
arguments à partir des `input_json_delta`, et `message_start`/`message_delta`
fournissent les compteurs. Une annulation ferme la réponse en cours, comme
ailleurs. Le routeur multi-clés continue de parler le même protocole de son
côté, en non-flux puisqu'il doit pouvoir basculer de fournisseur.

Un échec de préflight rend `needs_setup()` vrai et le contrôle « clé du
fournisseur » bloquant dans `app.diagnostics()`. Le routeur refuse la clé avant
de l'écrire dans le coffre. Les messages d'erreur indiquent la variable ou le
composant concerné et la raison du refus, mais **n'affichent jamais la valeur
de la clé**, ni dans l'interface, ni dans les erreurs, ni dans les journaux.

## Routeur multi-LLM

`llm_router.enabled = true` fait construire `core/llm_router.py` par la
factory ; l'agent reçoit ce client à la place de `LLMClient`. Les clés
s'ajoutent dans le panneau **Clés & LLM** (une par ligne, fournisseur `auto`
ou imposé) et sont stockées dans le coffre sous le service `llm.<fournisseur>`,
avec un identifiant aléatoire comme nom d'utilisateur. Le coffre doit être
déverrouillé : sans cela, aucune clé cloud n'est lisible et seul Ollama peut
répondre.

État persistant : `data/llm_router/keys.json` (indices masqués, statuts,
compteurs de tokens, observations par modèle, ajustements de score). Les
secrets ne sont jamais écrits dans ce fichier. États :

- `ACTIVE` : clé utilisable ;
- `COOLDOWN` : HTTP 429, réactivation à `cooldown_until` (le `Retry-After`
  prime) ;
- `EXHAUSTED` : HTTP 402 ou `insufficient_quota`, réactivation après
  `llm_router.exhausted_hours` (24 h par défaut) ou via « Réactiver » ;
- `DEAD` : HTTP 401/403, à supprimer puis réenregistrer.

Sélection : modèles triés par score effectif (base ± ajustement mesuré), puis
clés actives triées par taux de réussite. Une bascule consomme au plus
`llm_router.max_attempts` essais. Les erreurs fatales de requête, notamment
400 (requête invalide), remontent immédiatement sans faire tourner les clés et
sans mettre la clé en `COOLDOWN` : seule une erreur de disponibilité doit
suspendre une clé. Les 5xx, délais et erreurs de transport mettent la clé en
pause courte. Enfin, Ollama local
(`llm_router.ollama_model`) est tenté même sans clé.

Le routeur n'expose pas de flux (`supports_stream = False`) : la bascule
prime avant le streaming. Anthropic utilise son API Messages native, Ollama
son API locale `/api/chat` ; les autres fournisseurs passent par un endpoint
OpenAI-compatible.

## Isolation par run

`isolation.profile` (ou `--profile`) choisit les familles d'outils exposées :
`read-only`, `files`, `files-web` ou `full`. Un outil hors profil n'est ni
proposé au modèle ni exécutable (`unknown_tool`), même si le modèle l'appelle.
`isolation.allowed_domains` restreint les hôtes des outils réseautés (sous-domaines
acceptés) ; les appels sont vérifiés et leurs décisions écrites dans
`data/network.jsonl` lorsque `isolation.log_network` est actif.

```bat
python app.py --task "resume mes notes" --profile read-only
```

C'est une isolation au niveau des outils : **un outil conservé garde les
droits du processus hôte**, ce n'est pas un bac à sable système. Concrètement,
`core/profile.py` définit les profils (`RunProfile`, `PROFILES`,
`apply_profile()`) et `core/factory.py` applique le profil avant de construire
le registre. `NetworkGuard`, dans le même module, contrôle les hôtes des outils
réseautés ; `core/tool_registry.py` applique ce garde réseau puis la porte
d'approbation (`core/approval.py`) avant tout appel à l'outil.

Les garde-fous de `tools/shell.py` sont réels mais restent des contrôles
d'application : le répertoire de travail est résolu dans les racines autorisées
par `tools/file_manager.py`, l'environnement enfant est expurgé des variables
qui ressemblent à des secrets (dont `llm.api_key_env`), certaines commandes
destructrices sont refusées et l'arbre de processus est tué au délai. En
revanche, il n'y a ni jeton de sécurité par outil, ni espace de noms, ni ACL
appliquée par le système : un outil conservé peut utiliser tous les droits du
compte qui exécute OmniAgent, accéder aux fichiers que ce compte peut lire et
lancer des processus avec la même identité. Le profil `full` conserve tous les
outils, y compris le shell. Pour retirer réellement ces droits, il faut un
runtime séparé (conteneur, AppContainer ou machine virtuelle) : c'est une
décision ouverte qui demande de choisir l'image, les montages et le réseau. Ce
n'est pas implémenté dans ce passage.

## Journal d'audit

Chaque trace scellée enregistre dans `data/audit/chain.jsonl` le nombre de ses
enregistrements et la racine SHA-256 de leurs empreintes **dans l'ordre**,
reliée au scellement précédent. Une ligne modifiée, supprimée ou réordonnée est
détectée ; les décisions de politique sont chaînées de la même façon.

```bat
python app.py --audit-seal      :: migre les traces existantes
python app.py --verify-audit
```

La chaîne détecte une altération accidentelle ou partielle, mais **elle n'est
pas une preuve d'intégrité auto-suffisante** : un attaquant qui détient la clé
peut réécrire la trace puis toutes les entrées du journal et recalculer les
empreintes. `--verify-audit` seul peut alors afficher une chaîne cohérente. La
parade est un ancrage externe : conserver une copie de `chain.jsonl` sur un
support que l'attaquant ne peut pas réécrire.

L'implémentation existe déjà dans `core/audit.py` : `export_anchor()` copie
le fichier source `data/audit/chain.jsonl` (défini par `audit_path()`) et
`verify_anchor()` compare le journal courant au préfixe de cette copie. Les
options correspondantes sont déclarées dans `app.py`.

### Procédure d'ancrage externe

1. **Fichier source** : `data/audit/chain.jsonl` sur la machine OmniAgent.
2. **Destination** : un autre support, hors de la machine et hors de portée du
   compte compromis, par exemple une clé USB montée en `E:` ou un partage
   réseau contrôlé : `E:\audit\omniagent-chain-2026-11-19.jsonl`. Ne pas
   réutiliser le même nom sans conserver l'ancienne copie : des copies datées
   permettent de comparer plusieurs points de l'historique.
3. **Fréquence** : après chaque session autonome, et au minimum une fois par
   jour. Copier également avant une maintenance ou une mise à jour importante.
4. **Copie** :

```bat
python app.py --audit-anchor E:\audit\omniagent-chain-2026-11-19.jsonl
```

   La commande écrit une copie exacte et atomique du journal et affiche le
   nombre d'entrées et la tête de chaîne. Elle ne signe pas la copie : sa
   valeur vient uniquement du fait qu'elle quitte la machine et reste sous un
   contrôle séparé.

5. **Contrôle ultérieur** : sur la machine, comparer le journal courant à la
   copie conservée :

```bat
python app.py --verify-audit --audit-anchor E:\audit\omniagent-chain-2026-11-19.jsonl
```

   Le code de sortie est `0` si le journal courant commence par exactement la
   même chaîne que l'ancre, `1` sinon. Les ajouts postérieurs à la copie sont
   acceptés ; une entrée réécrite est signalée par
   `entree N : differente de l'ancre externe`, et un journal tronqué par
   `journal plus court que l'ancre`. Ces messages viennent de
   `core/audit.verify_anchor()`. Une contradiction avec une copie externe doit
   être traitée comme un incident : conserver l'ancre et les traces, ne pas
   écraser la copie, puis examiner les deux historiques. Si l'attaquant peut
   aussi modifier la destination, l'ancrage ne protège plus : la copie doit
   rester hors de portée de l'attaquant.

## Notifications Windows

```bat
python app.py --notify-install  :: raccourci du menu Demarrer + protocole omniagent://
python app.py --notify-action omniagent://trace/<run_id>
python app.py --notify-action omniagent://stop
```

Le raccourci porte l'AppUserModelID `OmniAgent.Desktop` et l'icône
`assets/icon.ico` ; les toasts de fin de session proposent « Ouvrir la trace »
(bouton associé à `omniagent://trace/<run_id>`) et « Arrêter »
(`omniagent://stop`). Le protocole enregistré lance `pythonw.exe` **avec
`app.py`** puis `--notify-action "%1"`. Si Windows refuse la version avec
boutons, une toast simple est affichée ; si PowerShell est indisponible, la
session continue et l'échec est journalisé. Les deux boutons ont été cliqués
réellement le 2026-11-19 : `Arrêter` a écrit le fichier STOP, `Ouvrir la trace`
a ouvert le dossier des traces ; STOP et la trace jetable ont été nettoyés.

Les captures de la toast et du chemin `.lnk`/protocole sont versionnées :
`artifacts/visual/toast.png`, `artifacts/visual/notification_path.png` et
leurs entrées dans `artifacts/visual/manifest.json`. Elles sont régénérées par
`python tools/visual_check.py --capture` ; ce contrôle vérifie la géométrie,
les pixels et l'OCR, mais ne clique pas les boutons de la toast.

### DPI et multi-écran

La capture utilise `ImageGrab.grab(all_screens=True)` et recadre avec l'origine
virtuelle `SM_XVIRTUALSCREEN`/`SM_YVIRTUALSCREEN`, donc un écran placé à gauche
ou au-dessus de l'écran principal (coordonnées négatives) reste dans l'image.
La géométrie de la fenêtre et les rectangles des boutons sont convertis dans
ce repère avant le crop. Une capture pré-ouverture n'est jamais réutilisée.

Mesure réelle du 2026-11-19 : écran 1920×1080, **DPI 120 (125 %)**, un seul
écran physique ; la capture et l'OCR passent avec `per-monitor` awareness. Le
cas d'une origine négative est couvert par
`tests/test_visual_artifacts.py::test_button_crop_uses_the_captured_frame_and_virtual_origin`.
Un second écran physique n'était pas disponible : le branchement multi-écran
réel reste à confirmer sur une station à deux moniteurs.

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

**Preuve visuelle Windows** (images versionnées) :

- `artifacts/visual/approval_dialog.png` : boîte native réelle avec les trois
  boutons, géométrie et OCR ;
- `artifacts/visual/notification_path.png` : `.lnk` et commande du protocole
  lus dans Windows ;
- `artifacts/visual/toast.png` : toast réellement affichée.

`artifacts/visual/manifest.json` contient les rectangles des boutons, les
statistiques de pixels et les textes OCR ; `tests/test_visual_artifacts.py`
vérifie ces artefacts. Régénération : `python tools/visual_check.py --capture`
(Windows interactif uniquement).

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
ajoutées au contexte. Tout outil à effet de bord - écriture, shell, navigateur,
coffre en modification - est refusé et jamais exécuté ; un outil inconnu l'est
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
