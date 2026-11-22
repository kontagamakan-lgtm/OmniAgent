# Confidentialité et données

OmniAgent est une application de bureau : il n'y a **ni compte, ni site web, ni
cookie, ni mesure d'audience**. Ce document décrit exactement ce qui est écrit
sur la machine, ce qui en sort, et comment tout supprimer.

## En bref

- Rien n'est envoyé à l'éditeur d'OmniAgent : aucun télémétrie, aucun rapport de
  plantage automatique, aucune publicité, aucun traceur.
- Les seuls flux sortants sont ceux que **vous** configurez ou déclenchez : le
  fournisseur de modèle, la recherche web, et les pages que l'agent ouvre.
- Toutes les données de travail (traces, coffre, mémoire, notes) restent dans le
  dossier de données local.

## Données écrites sur la machine

Emplacement par défaut : `%LOCALAPPDATA%\OmniAgent` (ou `OMNIAGENT_DATA_DIR`,
ou le dossier de l'application en mode portable).

| Donnée | Fichier | Contenu | Réglage |
|---|---|---|---|
| Configuration | `config.json` | réglages, chemins autorisés, canaux du navigateur - **jamais de clé d'API** | toujours |
| Clé d'API | `.env` (répertoire d'installation) ou variable d'environnement | une ligne `CLE=valeur`, permissions restreintes quand le système le permet | assistant de démarrage |
| Traces d'exécution | `traces/*.jsonl` | étapes, outils appelés, compteurs ; le contenu des messages seulement si `trace.content = "full"` | `trace.enabled` (activé par défaut) |
| Coffre d'identifiants | `vault.enc` | identifiants chiffrés AES-256-GCM, clé dérivée par Argon2id (scrypt en repli) | créé à la demande |
| Mémoire projet | notes datées dans le dossier de données | résumés bornés (`memory.max_notes`, `memory.max_chars`) | `memory.enabled` |
| Notes / journal | vault Obsidian local (`data/Obsidian/…`) | notes Markdown issues des lectures | `obsidian.enabled` |
| Index documentaire | base SQLite locale | texte extrait des documents indexés | à la demande |

Aucune de ces données n'est lue par un service distant. Les traces peuvent être
chiffrées au repos (AES-256-GCM, une enveloppe par ligne) et leur rétention est
bornée par `trace.max_runs`, `trace.max_age_days` et `trace.max_bytes`.

## Ce qui quitte la machine

| Destination | Déclencheur | Contenu envoyé |
|---|---|---|
| Fournisseur de modèle configuré (`llm.base_url` : DeepSeek, endpoint compatible OpenAI, API Messages d'Anthropic, ou Ollama local) | chaque génération | le prompt système, votre demande, les résultats d'outils nécessaires, l'historique de contexte |
| Moteurs de recherche (`search.engines`) | outils de recherche web | la requête, éventuellement les filtres de région et de sûreté |
| Sites visités | outils navigateur et lecture de page | la requête HTTP habituelle d'un navigateur (adresse, en-têtes, cookies du site) |

Points à connaître :

- **HTTPS est exigé** pour le fournisseur de modèle et pour l'index sémantique :
  `http://` n'est accepté que vers la boucle locale (`localhost`, `127.0.0.1`,
  `::1`), où il n'y a pas de réseau à écouter. Une URL distante en clair est
  refusée par la validation de configuration.
- **Ollama local** ne fait sortir aucun prompt de la machine : c'est le mode à
  choisir si le contenu ne doit pas quitter le poste.
- Le fournisseur de modèle applique ses propres conditions et sa propre
  politique de conservation ; ce choix vous appartient.
- Le titre de la fenêtre et le contenu des fichiers ne sont jamais transmis en
  dehors des cas ci-dessus.

## Ce que l'application ne fait pas

- Pas de mesure d'audience, pas de cookies, pas de bandeau de consentement : il
  n'y a rien à consentir, aucune page web n'est servie.
- Pas de newsletter ni d'e-mail : aucun lien de désinscription n'est nécessaire.
- Pas de compte utilisateur, donc pas de profil ni de données de compte.
- Pas de paiement dans l'application : il n'y a ni frais cachés ni politique de
  remboursement à publier.
- Pas de fonctionnalité réservée aux adultes ni de contenu soumis à un âge
  minimum ; l'application est un outil de développement sans inscription.

## Réduire ce qui est écrit

- `trace.content = "metadata"` : les traces gardent les étapes et les compteurs
  mais **pas** le contenu des messages.
- `trace.enabled = false` : aucune trace.
- `trace.archive = true` : compression des anciennes traces au lieu de les
  garder en clair ; le chiffrement au repos est disponible séparément.
- Ne pas créer de coffre si aucun identifiant n'est stocké ; le supprimer
  (`vault.enc`) efface les secrets qu'il contient.

## Supprimer ses données

1. Fermer l'application (le coffre et les traces ne sont ouverts que par elle).
2. Supprimer le dossier de données : `%LOCALAPPDATA%\OmniAgent` (ou la valeur de
   `OMNIAGENT_DATA_DIR`). Cela efface configuration, traces, index, mémoire,
   notes et coffre.
3. Supprimer le fichier `.env` du répertoire d'installation pour effacer la clé
   d'API écrite par l'assistant.
4. Désinstaller l'application (Paramètres → Applications, ou `unins000.exe`) :
   le programme et ses raccourcis partent, le dossier de données doit être
   supprimé à l'étape 2 s'il doit disparaître aussi.

Aucune demande à adresser à un tiers n'est nécessaire : personne d'autre ne
détient ces données. Pour un fournisseur de modèle, la suppression se fait
depuis le compte chez ce fournisseur.

## Composants tiers et licences

| Composant | Rôle | Licence |
|---|---|---|
| `tessdata_fast` (`assets/tessdata/*.traineddata`) | reconnaissance de texte hors ligne | Apache License 2.0 (voir `assets/tessdata/README.txt`) |
| Tesseract / `tesserocr` / Leptonica | moteur OCR embarqué | Apache License 2.0 / BSD 2-clauses |
| Playwright | automatisation du navigateur système | Apache License 2.0 |
| PyInstaller | fabrication de l'exécutable | GPL 2.0 avec exception de distribution |
| Inno Setup | installateur Windows | licence libre autorisant la redistribution |
| Python et bibliothèques de `requirements.txt` | exécution | licences respectives, versions figées |

Polices : l'interface utilise **Segoe UI**, fournie par Windows et non
redistribuée dans les paquets. `assets/icon.ico` et `assets/theme.json` sont des
éléments du projet.

## Contact

Les questions et les signalements passent par les tickets du dépôt :
<https://github.com/kontagamakan-lgtm/OmniAgent/issues>.
