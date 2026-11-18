# OmniAgent Windows

Agent personnel autonome pour Windows : boucle **ReAct** (Reasoning + Acting),
outils fichiers illimités dans les racines autorisées, automatisation navigateur
Playwright, coffre-fort d'identifiants AES-256-GCM, recherche web multi-moteurs
et index sémantique local. Interface CustomTkinter, backend LLM interchangeable
(DeepSeek, endpoint compatible OpenAI, Ollama local), compilation PyInstaller.

---

## Décisions d'architecture

| Sujet | Choix retenu | Raison |
|---|---|---|
| Interface | **CustomTkinter** (pas PySide6) | Aucune dépendance Qt à collecter dans PyInstaller, API `Text` suffisante pour le streaming token par token et le journal d'outils coloré. Build mesurée : 126,91 Mio au total, dont ~26 Mio de NumPy/OpenBLAS ; le reste vient des outils (pilote Playwright, bibliothèques), pas de l'interface (voir « Variantes de l'exécutable »). |
| Index vectoriel | **SQLite FTS5 + cosinus NumPy**, `sqlite-vec` en accélérateur optionnel | Aucune extension native obligatoire : l'index fonctionne dès l'installation. Si `sqlite_vec` est présent (≥ 20 000 chunks), il prend le relais sans changement d'API. ChromaDB écarté (serveur + ~200 Mo pour un besoin mono-utilisateur). |
| Recherche | DuckDuckGo HTML/Lite, SearxNG, Brave API optionnelle | Zéro clé API par défaut, plusieurs moteurs en secours automatique, extraction de contenu par `trafilatura`. |
| Modèle local | Sélecteur dans l'UI, transport HTTP direct vers `/api/chat` d'Ollama | Pas de dépendance au SDK `ollama` ; streaming et annulation gérés par `httpx`. |
| Coffre-fort | Argon2id (64 Mio, t=3, p=4) + AES-256-GCM, en-tête authentifié | Paramètres conformes aux recommandations OWASP ; le tag GCM authentifie aussi l'en-tête (anti-altération du sel). |
| Répertoires | Racines blanches + `denied_globs` en seconde ceinture | Les outils fichiers refusent toute sortie hors racines, y compris via `..` ou lien symbolique (`follow_symlinks=False`). |

> Note technique : un modèle hébergé (DeepSeek, tout endpoint compatible OpenAI)
> applique ses propres filtres **côté serveur**, qu'aucun system prompt ne peut
> neutraliser. Le mode réellement sans filtre plateforme est le backend Ollama
> local, sélectionnable dans l'UI.

---

## Arborescence

```
OmniAgent/
├── app.py                     # ✔ LIVRABLE 5 — bootstrap, --task sans interface, --check
├── config.py                  # ✔ LIVRABLE 1 — configuration typée + validation
├── requirements.txt           # ✔ LIVRABLE 1
├── requirements-dev.txt
├── requirements-vector.txt    # sqlite-vec optionnel
├── build.bat                  # ✔ LIVRABLE 5 — compilation en un clic
├── OmniAgent.spec             # ✔ LIVRABLE 5 — recette PyInstaller (paquet complet)
├── OmniAgent-lite.spec        #    variante --lite (sans NumPy, Pillow conservé)
├── OmniAgent-slim.spec        #    alias de --lite : même paquet, dossier « slim »
├── .env.example / .gitignore / README.md
├── assets/                    # icon.ico, thème JSON
├── core/
│   ├── agent_core.py          # LIVRABLE 4 — boucle ReAct, budget, reprises
│   ├── llm_client.py          #    client OpenAI-compatible + Ollama (streaming)
│   ├── prompts.py             #    system prompt action-first + templates
│   ├── react_parser.py        #    parseur tolérant des appels d'outils
│   ├── tool_registry.py       #    schémas JSON, dispatch, garde-fous
│   ├── events.py              #    bus d'événements thread-safe vers l'UI
│   └── errors.py              #    hiérarchie d'exceptions et politique de reprise
├── tools/
│   ├── base.py                # ✔ LIVRABLE 2 — Tool ABC, ToolResult, validation JSON Schema
│   ├── file_manager.py        # ✔ LIVRABLE 2 — FS confiné, recherche, renommage de masse
│   ├── vault.py               # ✔ LIVRABLE 2 — coffre-fort chiffré + TOTP
│   ├── browser_agent.py       # LIVRABLE 3 — Playwright, login, formulaires
│   ├── web_search.py          # LIVRABLE 3 — multi-moteurs + extraction
│   ├── doc_index.py           #    RAG : découpage, FTS5, vecteurs
│   ├── ocr.py                 #    OCR images / PDF scannés (Tesseract)
│   └── shell.py               #    exécution PowerShell/cmd contrôlée
├── security/
│   ├── crypto.py              # ✔ LIVRABLE 2 — Argon2id/scrypt + AES-256-GCM
│   └── redaction.py           # ✔ LIVRABLE 2 — masquage des secrets (logs + UI)
├── storage/
│   ├── db.py                  #    SQLite WAL, migrations, FTS5
│   └── schema.sql
├── ui/
│   ├── controller.py          # ✔ LIVRABLE 5 — AgentCore dans un thread, file d'événements
│   └── app_gui.py             # ✔ LIVRABLE 5 — fenêtre CustomTkinter (chat + journal d'outils)
├── tests/                     # 181 tests, tous verts
└── data/                      # généré à l'exécution, ignoré par git
    ├── config.json            #    configuration persistée
    ├── vault.enc              #    coffre chiffré
    ├── omniagent.db           #    historique, index FTS5, audit
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

- **Tesseract OCR** (optionnel) : installer `tesseract-ocr-w64` puis renseigner
  `files.tesseract_cmd` ou `OMNIAGENT_FILES_TESSERACT_CMD`. Sans lui, les images
  sont indexées par métadonnées uniquement.
- **Ollama** (optionnel) : `ollama pull qwen2.5:14b` + `ollama pull nomic-embed-text`
  pour un fonctionnement 100 % local sans filtre plateforme.

---

## Variantes de l'exécutable

`build.bat` produit un paquet PyInstaller « onedir » : un dossier
`dist\<variante>\` contenant `OmniAgent.exe` et `_internal\`, vérifié à la fin
par `smoke_exe.py` (sauf `--no-smoke`). Tailles et nombres de fichiers mesurés
sur les paquets de `dist\` de ce poste :

| Variante | Commande | Taille | Fichiers | Contenu |
|---|---|---|---|---|
| Complète | `build.bat` | 126,91 Mio | 1505 | NumPy/OpenBLAS (~26 Mio), Pillow, `pytesseract`, `pypdf`/`docx`/`openpyxl` ; navigateur pris dans le cache Playwright, sinon Edge/Chrome de la machine |
| Lite | `build.bat --lite` | 98,67 Mio | 1466 | Sans NumPy (index vectoriel dégradé) ; Pillow, `pytesseract`, `pypdf`/`docx`/`openpyxl` conservés, donc OCR, indexation PDF/DOCX/XLSX et navigateurs fonctionnent à l'identique |
| Slim | `build.bat --slim` | 98,67 Mio | 1466 | **Alias de `--lite`** : même recette, produite dans `dist\OmniAgent-slim` pour les scripts qui utilisent ce dossier |

Valeurs tronquées à deux décimales (somme réelle des tailles de fichiers) :
133 076 042 octets pour le complet, 103 466 039 pour le lite. Aucun des deux
paquets ne contient de `node.exe`. Le surcoût de ~2,6 Mio par rapport aux
mesures précédentes vient de `pypdf`, `docx` et `openpyxl`, désormais déclarés
en `hiddenimports` : `tools.file_manager._require` passe par `__import__`, donc
PyInstaller ne les voyait pas et l'indexation PDF/DOCX/XLSX échouait dans le
paquet.

Aucune variante n'embarque `playwright/driver/node.exe` (~95 Mio) : les outils
`browser_*` pilotent Chromium avec le **Node.js 18+ du système** (voir
« Dépendances de la cible pour les outils `browser_*` »).

`--slim` reste disponible comme **alias de `--lite`** : la commande et le
dossier `dist\OmniAgent-slim` continuent de fonctionner pour les scripts
existants, avec exactement le contenu de la variante allégée (mêmes exclusions,
même poids). La seule différence historique — l'absence de Node embarqué — ne
distingue plus les paquets puisqu'aucun ne l'embarque.

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

**Part des familles `doc_index_*`, `ocr_*` et `shell_*`** : environ 21,8 Kio de
code source (`tools/doc_index.py` 10,1 Kio, `tools/ocr.py` 6,0 Kio,
`tools/shell.py` 5,7 Kio), embarqués en bytecode dans l'archive PyInstaller.
Le module `pytesseract` (pur Python) et Pillow sont embarqués dans les trois
variantes : c'est ce qui permet à `ocr_status` de répondre dans l'exe. **Aucun
moteur Tesseract n'est livré** : `tesseract.exe` doit être installé sur la
machine cible (ou désigné par `files.tesseract_cmd`), sinon `ocr_status` répond
`available: false` avec la raison exacte. Leur poids reste négligeable devant
NumPy/OpenBLAS (~26 Mio).

Autres options :

- `build.bat --check` : vérifie seulement l'environnement (Python, PyInstaller,
  customtkinter) et lance `python app.py --check`, sans rien compiler.
- `build.bat --no-smoke` : compile sans démarrer l'exécutable ; combinable avec
  `--lite` et `--slim`.
- `build.bat --all` : construit le paquet **complet puis l'allégé** et lance le
  contrôle de fumée sur les deux exécutables — une seule commande valide les
  deux variantes. Comme le paquet complet nettoie tout `dist\`, l'état final
  contient `dist\OmniAgent` et `dist\OmniAgent-lite` ; ajouter `--no-smoke`
  saute les deux lancements.

> `build.bat` sans option conserve le comportement historique : il supprime
> tout `dist\`, donc les variantes déjà compilées. `build.bat --lite` (ou
> `--slim`, son alias) ne supprime que son propre dossier
> (`dist\OmniAgent-lite`, `dist\OmniAgent-slim`) : les paquets déjà présents
> restent en place et peuvent coexister. En fin de compilation,
> le script affiche le poids du paquet produit (Mio et nombre de fichiers),
> mesure faite par Python — inutile de la calculer à la main.

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
  variable est déjà définie) — sans quoi le paquet ne trouverait aucun
  navigateur alors que la machine en a un.
- **Microsoft Edge ou Google Chrome** installé : aucun navigateur n'est
  fourni dans le paquet. Quand `browser.channel` vaut `""` ou `"bundled"` et
  qu'aucun Chromium Playwright n'est présent dans son cache
  (`%LOCALAPPDATA%\ms-playwright`), `resolved_channel` choisit automatiquement
  le navigateur système (`msedge` puis `chrome`) — aucune édition manuelle de
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

---

## Livrable 2 — coffre-fort et gestion de fichiers

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
| Secrets jamais renvoyés au modèle | `vault_get_entry` masque par défaut ; `reveal=true`, `vault_totp` et `vault_generate_password` marquent le résultat `sensitive`, que `ToolResult.to_text()` / `to_dict()` refusent alors de sérialiser (un `safe_summary` — service, durée de validité, longueur — remplace la valeur). `extract_cookies` n'expose qu'un résumé (nombre, domaines, noms), valeurs retenues en local. Les notes d'une entrée (`entry.public()`) sont masquées elles aussi : du texte libre peut contenir un secret collé |
| Journal d'audit | JSONL (création, déverrouillage, ajout, accès, suppression) — aucun secret, lignes passées au `Redactor` |
| 2FA | TOTP RFC 6238 implémenté en stdlib (HMAC-SHA1), validé contre les vecteurs de test officiels |
| Remplacement forcé | `create(force=True)` copie le conteneur existant en `vault.enc.<horodatage>.bak` avant écrasement — aucune perte si la phrase secrète est oubliée |

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
structuré — jamais une exception non gérée — pour que la boucle ReAct puisse
analyser l'échec et changer de stratégie.

### Vérification

```bat
python tests/test_crypto.py        :: 16/16
python tests/test_redaction.py     :: 12/12
python tests/test_vault.py         :: 16/16
python tests/test_file_manager.py  :: 28/28
python tests/test_integration.py   :: 3/3
python -m ruff check --select F,B,E9 security tools
```
