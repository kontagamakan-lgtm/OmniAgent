# Preuves visuelles Windows

Capturées sur `DESKTOP-D7O1SEG`, session console interactive 1, écran
1920×1080, avec `PIL.ImageGrab` (GDI, capture virtuelle multi-écran) et une
reconnaissance `tesserocr`.

Ces images documentent le rendu réel. La boîte d'approbation a été ouverte et
ses trois boutons cliqués lors de la vérification du 2026-11-19 ; les points
qui restent manuels sont tenus à jour dans `docs/OPERATIONS.md`, section
« Limites connues et vérifications manuelles ».

| Fichier | Ce qu'il montre | Vérification automatique |
| --- | --- | --- |
| `approval_dialog.png` | la vraie boîte Tk ouverte par `OmniAgentWindow._tool_approval` pour `fs_delete` | 795×499, trois boutons `Autoriser une fois` / `Autoriser pour la session` / `Refuser`, OCR des trois libellés, écart-type 62,5 |
| `toast.png` | la toast Windows réellement affichée par `core.notify.notify_windows` | 479×163, 62 323 pixels modifiés, OCR `OmniAgent` + « Les notifications fonctionnent. » |
| `notification_path.png` | le chemin réel du raccourci et la commande du protocole lus dans Windows | `.lnk`, cible `pythonw.exe`, `--notify-action`, AUMID présent, OCR du chemin |
| `manifest.json` | géométries, rectangles des boutons, OCR, statistiques de pixels et valeurs exactes du `.lnk`/protocole | lu par `tests/test_visual_artifacts.py` |

Aucune capture plein écran n'est conservée : elle montrerait le bureau et des
documents sans rapport. Le script ne garde que les fenêtres vérifiées.

## Reproduire

```bat
python tools/visual_check.py --capture
python -m pytest tests/test_visual_artifacts.py -q
```

Le script échoue avec la sortie concrète si la session n'est pas interactive, si
la fenêtre n'apparaît pas, si la capture est uniforme, si un bouton manque ou si
l'OCR ne retrouve pas le texte attendu. Il ne fabrique jamais d'image de
remplacement.
