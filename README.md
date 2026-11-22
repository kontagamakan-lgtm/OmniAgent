# OmniAgent Windows

Agent personnel autonome pour Windows : boucle **ReAct** (Reasoning + Acting),
outils fichiers illimités dans les racines autorisées, automatisation navigateur
Playwright, coffre-fort d'identifiants AES-256-GCM, recherche web multi-moteurs
et index sémantique local. Interface CustomTkinter, backend LLM interchangeable
(DeepSeek, tout endpoint compatible OpenAI, API Messages d'Anthropic, Ollama
local), compilation PyInstaller.

> **Limites de sûreté et vérifications manuelles restantes** : voir
> `docs/OPERATIONS.md`, section « Limites connues et vérifications manuelles ».
>
> **Données et confidentialité** : ce qui reste sur la machine, ce qui en sort
> et comment tout effacer - `docs/PRIVACY.md`.
> **Conditions d'utilisation** : responsabilités, usage acceptable, garanties -
> `docs/TERMS.md`.

---

## Décisions d'architecture

| Sujet | Choix retenu | Raison |
|---|---|---|
| Interface | **CustomTkinter** (pas PySide6) | Aucune dépendance Qt à collecter dans PyInstaller, API `Text` suffisante pour le streaming token par token et le journal d'outils coloré. Build mesurée : 187,40 Mio au total, dont ~26 Mio de NumPy/OpenBLAS ; le reste vient des outils (pilote Playwright, bibliothèques), pas de l'interface (voir « Variantes de l'exécutable »). |
| Index vectoriel | **SQLite FTS5 + cosinus NumPy**, `sqlite-vec` en accélérateur optionnel | Aucune extension native obligatoire : l'index fonctionne dès l'installation. Si `sqlite_vec` est présent (≥ 20 000 chunks), il prend le relais sans changement d'API. ChromaDB écarté (serveur + ~200 Mo pour un besoin mono-utilisateur). |
| Recherche | DuckDuckGo HTML/Lite, SearxNG, Brave API optionnelle | Zéro clé API par défaut, plusieurs moteurs en secours automatique, extraction de contenu par `trafilatura`. |
| Modèle local | Sélecteur dans l'UI, transport HTTP direct vers `/api/chat` d'Ollama | Pas de dépendance au SDK `ollama` ; streaming et annulation gérés par `httpx`. |
| Coffre-fort | Argon2id (64 Mio, t=3, p=4) + AES-256-GCM, en-tête authentifié | Paramètres conformes aux recommandations OWASP ; le tag GCM authentifie aussi l'en-tête (anti-altération du sel). |
| Répertoires | Racines blanches + `denied_globs` en seconde ceinture | Les outils fichiers refusent toute sortie hors racines, y compris via `..` ou lien symbolique (`follow_symlinks=False`). |

> Note technique : un modèle hébergé (DeepSeek, Anthropic, tout endpoint
> compatible OpenAI) applique ses propres filtres **côté serveur**, qu'aucun
> system prompt ne peut neutraliser. Le mode réellement sans filtre plateforme est le backend Ollama
> local, sélectionnable dans l'UI.

---

## Arborescence

```
OmniAgent/
├── app.py                     # ✔ LIVRABLE 5 - bootstrap, --task sans interface, --check
├── config.py                  # ✔ LIVRABLE 1 - configuration typée + validation
├── requirements.txt           # ✔ LIVRABLE 1
├── requirements-dev.txt
├── requirements-vector.txt    # sqlite-vec optionnel
├── build.bat                  # ✔ LIVRABLE 5 - compilation en un clic
├── OmniAgent.spec             # ✔ LIVRABLE 5 - recette PyInstaller (paquet complet)
├── OmniAgent-lite.spec        #    variante --lite (sans NumPy, Pillow conservé)
├── OmniAgent-slim.spec        #    alias local de --lite ; dossier « slim » non publié
├── .env.example / .gitignore / README.md
├── build_smoke/               # smoke tests des exécutables et de la boucle ReAct
├── assets/                    # icon.ico, theme.json, tessdata/
├── core/
│   ├── agent_core.py          # LIVRABLE 4 - boucle ReAct, budget, approches, plan/critique
│   ├── llm_client.py          #    client OpenAI / Anthropic / Ollama (flux SSE)
│   ├── prompts.py             #    system prompt action-first + templates
│   ├── react_parser.py        #    parseur tolérant des appels d'outils
│   ├── tool_registry.py       #    schémas JSON, dispatch, garde-fous
│   ├── events.py              #    bus d'événements thread-safe vers l'UI
│   └── errors.py              #    hiérarchie d'exceptions et politique de reprise
├── tools/
│   ├── base.py                # ✔ LIVRABLE 2 - Tool ABC, ToolResult, validation JSON Schema
│   ├── file_manager.py        # ✔ LIVRABLE 2 - FS confiné, recherche, renommage de masse
│   ├── vault.py               # ✔ LIVRABLE 2 - coffre-fort chiffré + TOTP
│   ├── browser_agent.py       # LIVRABLE 3 - Playwright, login, formulaires
│   ├── web_search.py          # LIVRABLE 3 - multi-moteurs + extraction
│   ├── doc_index.py           #    RAG : découpage, FTS5, vecteurs
│   ├── obsidian.py            #    vault Markdown, liens, mémoire automatique
│   ├── ocr.py                 #    OCR images / PDF scannés (Tesseract)
│   └── shell.py               #    exécution PowerShell/cmd contrôlée
├── security/
│   ├── crypto.py              # ✔ LIVRABLE 2 - Argon2id/scrypt + AES-256-GCM
│   └── redaction.py           # ✔ LIVRABLE 2 - masquage des secrets (logs + UI)
├── ui/
│   ├── approval_dialog.py     # boîte Autoriser une fois / session / Refuser
│   ├── controller.py          # ✔ LIVRABLE 5 - AgentCore dans un thread, file d'événements
│   └── app_gui.py             # ✔ LIVRABLE 5 - fenêtre CustomTkinter (chat + journal d'outils)
├── tests/                     # suite pytest; tests opt-in ignorés par défaut
└── data/                      # généré à l'exécution, ignoré par git
    ├── config.json            #    configuration persistée
    ├── vault.enc              #    coffre chiffré
    ├── Obsidian/OmniAgent_Brain/ # cerveau Markdown (notes, index, journal)
    ├── index/  logs/  downloads/  cache/  reports/  browser_profile/
```

---

## Installation

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
```

Prérequis externes :

- **Tesseract OCR** - **plus nécessaire** : les paquets embarquent le moteur
  `tesserocr` (Tesseract 5.5 + Leptonica en DLL) et les données `eng`/`fra`, donc
  `ocr_image` fonctionne sans rien installer. Le binaire système reste utile
  pour d'autres langues, ou pour profiter des données déjà présentes :
  installeur Windows officiel -
  <https://github.com/UB-Mannheim/tesseract/wiki> (builds UB Mannheim). Après
  installation, soit `tesseract` est dans le `PATH`, soit on renseigne la clé
  `files.tesseract_cmd` dans `data\config.json` :

  ```json
  {
    "files": {
      "tesseract_cmd": "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    }
  }
  ```

  ou la variable `OMNIAGENT_FILES_TESSERACT_CMD`. Vérification exacte :

  ```bat
  "%PROGRAMFILES%\Tesseract-OCR\tesseract.exe" --version
  python app.py --check
  ```

  S'il est absent, le moteur embarqué prend le relais : `ocr_status` renvoie
  `backend: "tesserocr"`. Le binaire n'est obligatoire que pour les langues qui
  ne sont pas livrées dans `assets/tessdata`.
- **Ollama** (optionnel) : `ollama pull qwen2.5:14b` + `ollama pull nomic-embed-text`
  pour un fonctionnement 100 % local sans filtre plateforme.

---

## Cerveau Obsidian

Au premier lancement graphique, OmniAgent vérifie la présence d'Obsidian et
lance si nécessaire `winget install --id Obsidian.Obsidian --exact --silent`.
Si winget est absent ou échoue, l'agent télécharge l'installateur officiel de
la dernière version (`Obsidian-<version>.exe`, release GitHub
`obsidianmd/obsidian-releases`), vérifie que le fichier est bien un exécutable
Windows et contrôle sa signature Authenticode (statut `Valid`, CN **et** O
exactement `Dynalist Inc` ou `Obsidian`, casse ignorée) ; un installateur non
signé, modifié ou signé par un
autre éditeur est refusé. Il le lance ensuite en silencieux (`/S /currentuser`)
puis confirme l'installation en retrouvant `Obsidian.exe`. En cas d'échec, le vault Markdown reste
utilisable, et `python app.py --install-obsidian` permet de relancer le
bootstrap explicitement. `OBSIDIAN_INSTALLER_URL` impose une URL https
d'installateur à la place de l'API GitHub. Le vault est créé sous
`data/Obsidian/OmniAgent_Brain/` (ou `obsidian.vault_dir`).

Le vault est la mémoire longue durée :

- `00_CORTEX_INDEX.md` est la note maîtresse, régénérée après chaque écriture ;
- les lectures web, fichiers, OCR et recherches documentaires deviennent des
  notes dans `01_Sources/` ;
- les faits, préférences et connaissances durables sont écrits par l'outil
  `obsidian_remember` dans `02_Knowledge/`, avec des `[[Wikilinks]]` et des
  `#tags` ; `folder` accepte un sous-dossier relatif personnalisé
  (`02_Knowledge/Projets/Atlas`), créé au besoin et refusé s'il sort du vault
  (chemin absolu, `..`, dossier masqué ou nom réservé) ;
- une note de journal est ajoutée dans `04_Journal/` à la fin d'un run.

L'agent reçoit l'instruction de chercher dans `obsidian_search` avant de
redécouvrir un fait, et de mémoriser ce qu'il apprend. Les résultats sensibles
(coffre, cookies) ne sont jamais transformés en notes ; titre, corps et source
sont passés par le `Redactor` avant écriture. Les dossiers cachés du vault
(`.obsidian`, `.trash`) ne sont ni indexés ni cherchés, et le vault ne peut pas
être une racine de disque.

```bat
python app.py --install-obsidian      :: bootstrap winget explicite
python app.py --check                 :: vault et présence d'Obsidian
```

Réglages `config.json` : `obsidian.enabled`, `auto_install`, `vault_name`,
`vault_dir`, `auto_memory`, `auto_journal`, `max_note_chars` et
`max_index_links`. Les variables correspondantes suivent
`OMNIAGENT_OBSIDIAN_*`.

## Auto-évolution bornée

Le noyau reste immuable ; tout ce que l'agent améliore vit dans
`evolution_core/` : `skills/*.py` (compétences rechargées à chaud) et
`prompts/adaptations.md` (prompt injecté dans le prompt système). Le moteur
`core/self_evolver.py` est **inactif par défaut** : il ne démarre qu'avec
`evolution.enabled = true`, après `evolution.idle_minutes` d'inactivité.

Chaque cycle :

1. diagnostique les outils lents ou en échec et les cycles ratés ;
2. fait une veille via `web_search` (garde réseau et profil respectés) ;
3. ne demande une compétence au modèle que si le diagnostic signale un outil
   lent, en échec ou une erreur récente (sinon la forge est ignorée) ;
4. crée un instantané dans `data/evolution/backups/`, exécute le test dans un
   sous-processus borné, et restaure l'instantané si le test échoue ; la version
   `v1.0.N` ne monte **que** si le test passe et que la compétence se charge ;
5. consigne des leçons dans `prompts/adaptations.md` et dans Obsidian
   (`[[Erreurs_Corrigées]]`, `[[Stratégies_Optimales]]`).

Garde-fous : aucune écriture hors de `evolution_core/`, imports et appels
dangereux (`subprocess`, `socket`, `ctypes`, `eval`, `exec`…) refusés avant
chargement, `pip install` impossible sans `evolution.allow_pip` (noms de
paquets validés), plafonds `max_cycles_per_day`,
`max_forced_cycles_per_day`, `interval_minutes`, `test_timeout_s` et
`backup_limit`. Chaque cycle, forcé compris, vérifie d'abord le budget global
(`budget.*`) et est refusé si les plafonds de coût ou de tokens sont déjà
atteints : un clic répété ne peut pas consommer sans limite. Les compétences sont exposées par deux
outils classés `dangerous` (`evolution_run_skill`, `evolution_force_cycle`) ;
seul `evolution_status` est une lecture.

Le bouton **Évolution** affiche la version auto-générée, les compétences
forgées, le journal des cycles et permet de forcer un cycle. Réglages
`config.json` : `enabled`, `core_dir`, `idle_minutes`, `interval_minutes`,
`max_cycles_per_day`, `max_forced_cycles_per_day`, `max_skill_chars`,
`max_prompt_chars`, `allow_network`, `allow_pip`, `pip_timeout_s`,
`test_timeout_s`, `backup_limit`, `journal_max_entries`,
`journal_max_bytes`.

## Routeur multi-LLM et multi-clés

`llm_router.enabled = true` remplace le client unique par
`core/llm_router.py` : un pool illimité de clés par fournisseur (DeepSeek,
OpenAI, Anthropic, Gemini, Groq, OpenRouter, Mistral) et un secours local
Ollama. Les clés sont ajoutées depuis le panneau **Clés & LLM** et leur secret
reste dans le coffre chiffré ; `data/llm_router/keys.json` ne contient qu'un
indice masqué, les états et les compteurs.

Chaque clé est `ACTIVE`, `COOLDOWN` (HTTP 429, réactivation automatique),
`EXHAUSTED` (HTTP 402 / `insufficient_quota`, réessai après
`exhausted_hours`) ou `DEAD` (HTTP 401/403, jusqu'à réenregistrement). À chaque
appel, le routeur classe les modèles par score de puissance puis choisit le
premier qui possède une clé active ; si cette clé échoue, il passe à la clé
suivante du même modèle, puis au modèle suivant, et enfin à Ollama. L'historique
ReAct et les appels d'outils sont convertis au format du fournisseur retenu
(OpenAI-compatible, API Messages d'Anthropic, API Ollama), sans perdre le
contexte de la tâche.

Les scores de base (matrice `CATALOGUE`) sont ajustés de ±8 points par les
mesures réelles de réussite, de latence et de tokens/seconde. Le moteur
d'auto-évolution recalcule ce classement à chaque cycle et le consigne dans
Obsidian (`Classement LLM - date`). Le streaming est désactivé quand le routeur
est actif : un flux déjà affiché ne peut pas être rejoué sur un autre modèle
sans dupliquer le texte ; l'agent utilise donc `complete` et conserve la bascule.

Réglages `config.json` : `llm_router.enabled`, `cooldown_s`,
`exhausted_hours`, `max_attempts`, `ollama_model`. Coffre verrouillé = clés
cloud indisponibles (seul Ollama reste possible) ; aucune clé n'apparaît dans
les journaux, les erreurs ou le fichier d'état.

## Assistant de premier démarrage

Au lancement, si aucune clé n'est disponible pour le fournisseur, la fenêtre
ouvre **Configuration initiale - OmniAgent** en modal au-dessus d'elle. Le
bouton **Plus tard** referme l'assistant : l'interface reste utilisable, mais
chaque envoi échouera tant qu'aucune clé n'est renseignée.

| Réglage | Où il est enregistré | Quand il prend effet |
|---|---|---|
| **Fournisseur** | `llm.backend`, `llm.base_url`, `llm.api_key_env` et le trio de modèles dans `config.json` : DeepSeek, OpenAI, Anthropic, Gemini, Groq, OpenRouter, Mistral, un autre service compatible OpenAI (adresse et modèle saisis à la main), ou « automatique » d'après le préfixe de la clé. | immédiatement |
| **Clé API** | variable du fournisseur choisi (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`) de la session, et fichier `.env` du dossier d'installation : à côté de `OmniAgent.exe`, racine du projet en source. Jamais dans `config.json`. | immédiatement |
| **Dossier de travail** | ajouté à `files.allowed_roots` dans `config.json` (`%LOCALAPPDATA%\OmniAgent\config.json` pour l'exe, `data\config.json` en source) | au prochain démarrage (il borne les outils fichiers) |
| **Canal navigateur** | `browser.channel` dans le même `config.json` : vide = Chromium embarqué sinon Edge/Chrome du système, ou `msedge` / `chrome` | au prochain démarrage |
| **Phrase du coffre** | crée `vault.enc` (Argon2id + AES-256-GCM) ; les phrases trop courtes ou non concordantes sont refusées, un coffre existant est conservé | immédiatement, coffre laissé déverrouillé |

Le bouton **Assistant** de l'en-tête rouvre la même fenêtre avec les valeurs
actuelles, pour changer d'avis sans éditer de fichier : la clé saisie est
active tout de suite, le dossier et le canal s'appliquent au redémarrage
suivant.

---

## Variantes de l'exécutable

`build.bat` produit un paquet PyInstaller « onedir » : un dossier
`dist\<variante>\` contenant `OmniAgent.exe` et `_internal\`, vérifié à la fin
par `smoke_exe.py` (sauf `--no-smoke`). Tailles et nombres de fichiers mesurés
sur les paquets de `dist\` de ce poste :

| Variante | Commande | Taille | Fichiers | Contenu |
|---|---|---|---|---|
| Complète | `build.bat` | 187,40 Mio | 1536 | NumPy/OpenBLAS (~26 Mio), Pillow, `pytesseract`, moteur OCR `tesserocr` (DLL Tesseract/Leptonica/ICU, ~58 Mio) et données `eng`/`fra`, `pypdf`/`docx`/`openpyxl` ; navigateur pris dans le cache Playwright, sinon Edge/Chrome de la machine |
| Lite | `build.bat --lite` | 159,16 Mio | 1497 | Sans NumPy (index vectoriel dégradé) ; Pillow, `pytesseract`, moteur OCR `tesserocr` et données de langue conservés, donc OCR, indexation PDF/DOCX/XLSX et navigateurs fonctionnent à l'identique |
| Slim | `build.bat --slim` | 159,16 Mio | 1497 | **Alias local de `--lite`, non publié** : même recette, produite dans `dist\OmniAgent-slim` pour les scripts qui utilisent ce dossier |

Valeurs tronquées à deux décimales (somme réelle des tailles de fichiers) :
196 501 824 octets pour le complet, 166 891 821 pour le lite. Aucun des deux
paquets ne contient de `node.exe`.

L'installateur Windows est construit avec `installer/OmniAgent.iss` (Inno Setup)
et publié sous `OmniAgent-<version>-Setup.exe`. Il s'installe par utilisateur dans
`%LOCALAPPDATA%\Programs\OmniAgent`, crée les raccourcis et le désinstalleur,
sans demander de droits administrateur. Le surcoût de ~60 Mio par rapport aux mesures
précédentes vient du moteur OCR en processus : DLL `tesserocr` (Tesseract 5.5,
Leptonica, ICU, ~53 Mio après sélection de la fermeture des imports) et
5,1 Mio de données `eng`/`fra`. `pypdf`, `docx`, `openpyxl` et `tesserocr` sont
déclarés en `hiddenimports` : les imports dynamiques (`__import__`,
imports en méthode) échappent à l'analyse statique de PyInstaller.

Aucune variante n'embarque `playwright/driver/node.exe` (~95 Mio) : les outils
`browser_*` pilotent Chromium avec le **Node.js 18+ du système** (voir
« Dépendances de la cible pour les outils `browser_*` »).

`--slim` reste disponible comme **alias local de `--lite`** : la commande et le
dossier `dist\OmniAgent-slim` continuent de fonctionner pour les scripts
existants, avec exactement le contenu de la variante allégée (mêmes exclusions,
même poids). Il n'est en revanche **plus publié** : `OmniAgent-slim.zip` a
quitté les assets de release, puisque c'était le paquet de `OmniAgent-lite.zip`
sous un autre nom - environ 80 Mio de doublon par version. Qui téléchargeait le
zip « slim » prend `OmniAgent-lite.zip`. Les releases déjà publiées gardent
leur asset : rien n'est retiré rétroactivement.

### Contrôle de fumée du navigateur

`build.bat` lance ce contrôle après le test de fenêtre (sauf `--no-smoke`) :

```bat
python smoke_exe.py --browser --exe dist\OmniAgent\OmniAgent.exe
python smoke_exe.py --browser --exe dist\OmniAgent-lite\OmniAgent.exe
```

Il exécute `OmniAgent.exe --browser-smoke`, qui démarre Chromium en headless
avec le Node.js du système, sert une page en `127.0.0.1` et exige le marqueur
`BROWSER_SMOKE_OK` lu dans le DOM. Sortie attendue :

```
Contrôle navigateur de dist\OmniAgent\OmniAgent.exe
  Node.js : C:\Program Files\nodejs\node.EXE
  Navigateurs Playwright : C:\Users\<vous>\AppData\Local\ms-playwright (chromium présent : True)
  ok   navigateur lancé (canal bundled)
  ok   page locale chargée : http://127.0.0.1:63439/
  ok   marqueur BROWSER_SMOKE_OK lu dans le DOM
  ok   BROWSER_SMOKE_OK confirmé par l'exécutable
Contrôle navigateur réussi.
```

Code 0 = le binaire pilote réellement le navigateur ; 1 = Node.js ou le
navigateur manque (le message nomme la cause), 2 = exécutable absent.

### Contrôle de fumée de l'OCR

`build.bat` lance ce contrôle après celui du navigateur (sauf `--no-smoke`) :

```bat
python smoke_exe.py --ocr --exe dist\OmniAgent\OmniAgent.exe
python smoke_exe.py --ocr --exe dist\OmniAgent-lite\OmniAgent.exe
```

L'exe rend une image avec Pillow, la fait lire au moteur retenu et exige
`OCR_SMOKE_OK` :

```
  moteur : tesserocr
  langue : fra+eng
  texte  : 'ATLAS BUDGET 42'
Contrôle OCR réussi (OCR_SMOKE_OK).
```

Aucun `tesseract.exe` n'est présent sur la machine de mesure : ce résultat
prouve que le moteur embarqué et ses données de langue fonctionnent dans le
paquet livré. `--check` l'affiche aussi :

```
ok  OCR : disponible (Tesseract 5.5.4 en processus (tesserocr), langues : eng, fra)
```

**Part des familles `doc_index_*`, `ocr_*` et `shell_*`** : environ 21,8 Kio de
code source (`tools/doc_index.py` 10,1 Kio, `tools/ocr.py` 6,0 Kio,
`tools/shell.py` 5,7 Kio), embarqués en bytecode dans l'archive PyInstaller.
Pillow, `pytesseract` **et le moteur en processus `tesserocr`** sont embarqués
dans les deux variantes. `Ocr` essaye d'abord un `tesseract.exe` (installé ou
`files.tesseract_cmd`), puis bascule sur `tesserocr` avec les données de
`assets/tessdata` : `ocr_status` renvoie `backend: "tesseract-cli"` ou
`"tesserocr"`, et si les deux échouent, `detail` cite **les deux causes**
(`--check` ajoute alors le lien officiel
<https://github.com/UB-Mannheim/tesseract/wiki>, la clé `files.tesseract_cmd` et
la commande de vérification). Le paquet ne dépend donc plus d'une installation
système ; le binaire reste prioritaire pour les langues supplémentaires. Leur
poids reste négligeable devant
NumPy/OpenBLAS (~26 Mio).

Autres options :

- `build.bat --check` : vérifie seulement l'environnement (Python, PyInstaller,
  customtkinter) et lance `python app.py --check`, sans rien compiler.
- `build.bat --no-smoke` : compile sans démarrer l'exécutable ; combinable avec
  `--lite` et `--slim`.
- `build.bat --all` : construit le paquet **complet puis l'allégé** et lance le
  contrôle de fumée sur les deux exécutables - une seule commande valide les
  deux variantes. Comme le paquet complet nettoie tout `dist\`, l'état final
  contient `dist\OmniAgent` et `dist\OmniAgent-lite` ; ajouter `--no-smoke`
  saute les deux lancements.

> `build.bat` sans option conserve le comportement historique : il supprime
> tout `dist\`, donc les variantes déjà compilées. `build.bat --lite` (ou
> `--slim`, son alias) ne supprime que son propre dossier
> (`dist\OmniAgent-lite`, `dist\OmniAgent-slim`) : les paquets déjà présents
> restent en place et peuvent coexister. En fin de compilation,
> le script affiche le poids du paquet produit (Mio et nombre de fichiers),
> mesure faite par Python - inutile de la calculer à la main.

### Dépendances de la cible pour les outils `browser_*`

Aucun paquet ne contient le runtime Node.js de Playwright : c'est le **Node.js
du système** qui pilote Chromium.

- **Node.js ≥ 18** accessible dans le `PATH` (ou `PLAYWRIGHT_NODEJS_PATH`
  pointant vers `node.exe`). Playwright est un client : son pilote JavaScript a
  besoin d'un runtime Node, et celui livré d'habitude par `pip` représente
  ~95 Mio de binaire. Au démarrage, `tools/browser_agent.py`
  (`ensure_system_node`) renseigne automatiquement `PLAYWRIGHT_NODEJS_PATH`
  avec le `node` du `PATH`, et ne touche jamais à une valeur déjà définie.
- **Cache de navigateurs Playwright** : dans l'exe, le pilote vit sous
  `_internal/playwright/driver`, d'où Playwright déduirait un cache local vide
  (`driver/package/.local-browsers`). `ensure_browsers_path` force donc
  `PLAYWRIGHT_BROWSERS_PATH` vers `%LOCALAPPDATA%\ms-playwright` (jamais si la
  variable est déjà définie) - sans quoi le paquet ne trouverait aucun
  navigateur alors que la machine en a un.
- **Microsoft Edge ou Google Chrome** installé : aucun navigateur n'est
  fourni dans le paquet. Quand `browser.channel` vaut `""` ou `"bundled"` et
  qu'aucun Chromium Playwright n'est présent dans son cache
  (`%LOCALAPPDATA%\ms-playwright`), `resolved_channel` choisit automatiquement
  le navigateur système (`msedge` puis `chrome`) - aucune édition manuelle de
  `data/config.json` n'est nécessaire. Un canal explicite (`msedge`, `chrome`)
  reste prioritaire et désactive la détection.
- Si **aucun** navigateur n'est trouvé (ni cache Playwright, ni Edge, ni
  Chrome), le lancement échoue avec un message actionnable : « aucun navigateur
  disponible : installez Microsoft Edge ou Google Chrome, ou exécutez
  « python -m playwright install chromium » ». Playwright n'est alors même pas
  importé.
- Si Node.js manque, `browser_open` échoue avec un message explicite plutôt
  que de planter.

---

## Configuration

Ordre de priorité : `config.json` < variables `OMNIAGENT_*` < `.env`.

```bat
python config.py --init       :: crée data/, config.json, .env.example
python config.py --validate   :: 0 = valide, 1 = erreurs bloquantes
python config.py --show       :: configuration effective, secrets masqués
python config.py --defaults   :: valeurs par défaut au format JSON
python config.py --check-keyring :: signale une phrase maîtresse résiduelle dans le Gestionnaire d'identifiants
```

Toute variable suit le motif `OMNIAGENT_<SECTION>_<CHAMP>` :

```bat
set OMNIAGENT_LLM_BACKEND=ollama
set OMNIAGENT_LLM_MODEL=qwen2.5:14b
set OMNIAGENT_BROWSER_HEADLESS=1
set OMNIAGENT_FILES_ALLOWED_ROOTS=C:\Docs;D:\Projets
```

---

## Feuille de route

| Livrable | Contenu | État |
|---|---|---|
| 1 | Arborescence, `requirements.txt`, `config.py` | livré |
| 2 | `security/crypto.py`, `security/redaction.py`, `tools/base.py`, `tools/vault.py`, `tools/file_manager.py` | livré |
| 3 | `tools/web_search.py`, `tools/browser_agent.py` | livré |
| 4 | `core/agent_core.py`, `llm_client.py`, `prompts.py`, `react_parser.py`, `tool_registry.py`, `events.py`, `errors.py` | livré |
| 5 | `ui/app_gui.py`, `ui/controller.py`, `app.py`, `build.bat`, `OmniAgent.spec` | livré |

### Livrable 4 - vérification de la boucle

En plus de la suite pytest, un smoke test autonome exerce la boucle complète
sans réseau ni données utilisateur : planification facultative, appel d'outil,
réponse finale et auto-critique.

```bat
python build_smoke\agent_loop_smoke.py
```

Il est exécuté par la CI et par le workflow de publication avant toute
construction d'installateur. Les options `agent.plan_before_act` et
`agent.self_critique_passes` restent désactivées par défaut : le plan ajoute
un appel LLM et chaque passe de critique en ajoute un.

---

## Livrable 2 - coffre-fort et gestion de fichiers

### Format du conteneur `vault.enc`

```
offset  taille  contenu
0       8       magie b"OMNIVLT1"
8       1       version (1)
9       1       KDF (1 = argon2id, 2 = scrypt)
10      2       longueur N des paramètres
12      N       paramètres JSON  {"kdf":"argon2id","m":65536,"p":4,"t":3}
12+N    16      sel aléatoire
28+N    12      nonce AES-GCM
40+N    ...     chiffré || tag (16 o)
```

L'AAD couvre **tout l'en-tête** : modifier le sel, les paramètres KDF ou le nonce
invalide le tag GCM. Un conteneur forgé qui tenterait d'imposer un Argon2id à
8 Mio est donc rejeté avant déchiffrement.

### Garanties

| Propriété | Mise en œuvre |
|---|---|
| Phrase secrète jamais persistée **par défaut** | Seule la clé dérivée reste en mémoire, dans un `bytearray` effacé au verrouillage. Exception opt-in : `vault.keyring_fallback` (désactivé par défaut) délègue explicitement la phrase au Gestionnaire d'identifiants Windows |
| Pas de re-dérivation à chaque écriture | La clé est réutilisée avec un nonce neuf (`seal_with_key`) |
| Anti-bruteforce | Argon2id 64 Mio / t=3 / p=4 ; compteur d'échecs persisté + verrouillage à backoff exponentiel (15 s → 300 s) |
| Secrets jamais renvoyés au modèle | `vault_get_entry` masque par défaut ; `reveal=true`, `vault_totp` et `vault_generate_password` marquent le résultat `sensitive`, que `ToolResult.to_text()` / `to_dict()` refusent alors de sérialiser (un `safe_summary` - service, durée de validité, longueur - remplace la valeur). `extract_cookies` n'expose qu'un résumé (nombre, domaines, noms), valeurs retenues en local. Les notes d'une entrée (`entry.public()`) sont masquées elles aussi : du texte libre peut contenir un secret collé |
| Journal d'audit | JSONL (création, déverrouillage, ajout, accès, suppression) - aucun secret, lignes passées au `Redactor` |
| 2FA | TOTP RFC 6238 implémenté en stdlib (HMAC-SHA1), validé contre les vecteurs de test officiels |
| Remplacement forcé | `create(force=True)` copie le conteneur existant en `vault.enc.<horodatage>.bak` avant écrasement - aucune perte si la phrase secrète est oubliée |

### Confinement des outils fichiers

`FileManager._resolve` développe l'environnement, résout les liens symboliques,
vérifie l'appartenance réelle aux `allowed_roots`, applique les `denied_globs`
et rejette les noms réservés (`CON`, `NUL`, `COM1`…) ainsi que les flux NTFS
(`fichier:flux`). Aucun joker n'est accepté dans un chemin. L'extraction ZIP
est protégée contre la traversée de répertoire, et un renommage de masse qui
échoue à mi-parcours est intégralement annulé.

### Catalogue d'outils exposé à l'agent

`vault_status` · `vault_services` · `vault_list_entries` · `vault_get_entry` ·
`vault_create_credential` · `vault_add_entry` · `vault_update_entry` ·
`vault_delete_entry` · `vault_generate_password` · `vault_totp` · `vault_lock` ·
`vault_backup`

`fs_list` · `fs_info` · `fs_read_text` · `fs_write_text` · `fs_mkdir` ·
`fs_copy` · `fs_move` · `fs_delete` · `fs_rename_bulk` · `fs_search` ·
`fs_find_duplicates` · `fs_organize` · `fs_archive` · `fs_extract` ·
`fs_convert` · `fs_disk_usage` · `fs_hash`

Chaque outil déclare un JSON Schema strict (`additionalProperties: false`) ;
`tools/base.py` valide les arguments et renvoie toujours un `ToolResult`
structuré - jamais une exception non gérée - pour que la boucle ReAct puisse
analyser l'échec et changer de stratégie.

Les capacités de chaque outil - lecture ou écriture, danger, secret - sont
déclarées une seule fois dans `tools/capabilities.py`. `build_agent` compare
cette table aux outils construits et refuse de démarrer si un outil n'y figure
pas ou si ses drapeaux divergent du contrat.

### Vérification

```bat
python tests/test_crypto.py        :: 16/16
python tests/test_redaction.py     :: 17/17
python tests/test_vault.py         :: 16/16
python tests/test_file_manager.py  :: 28/28
python tests/test_integration.py   :: 3/3
python tests/golden_traces.py      :: 9 scénarios hors ligne (golden)
python -m ruff check --select F,B,E9 security tools
```
