Données de langue Tesseract livrées avec OmniAgent
==================================================

Fichiers : eng.traineddata, fra.traineddata (modèles « fast »).

Source : https://github.com/tesseract-ocr/tessdata_fast
Licence : Apache License 2.0 (voir le dépôt ci-dessus).

Ces fichiers permettent au moteur embarqué (`tesserocr`, voir tools/ocr.py)
de reconnaître du texte sans installer Tesseract sur la machine. Pour ajouter
une langue, déposez son `.traineddata` ici puis reconstruisez le paquet : la
recette PyInstaller copie tous les `*.traineddata` du dossier dans l'exe.
