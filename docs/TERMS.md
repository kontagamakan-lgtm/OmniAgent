# Conditions générales d'utilisation

Ce document décrit ce que l'utilisateur accepte en installant et en utilisant
OmniAgent, et ce que l'éditeur du logiciel garantit. Il s'applique à
l'application de bureau ; il ne crée aucune obligation pour un service en ligne,
puisque l'application n'en dépend pas.

## Objet

OmniAgent est une application de bureau qui pilote un modèle de langage et des
outils locaux (fichiers, navigateur, OCR, index de documents) pour exécuter des
tâches demandées par l'utilisateur. Le logiciel est fourni tel quel, sans
engagement de résultat : les réponses du modèle peuvent être incomplètes ou
fausses, et les actions qu'il propose doivent être vérifiées.

## Responsabilité des actions

Les outils qui modifient la machine (écriture de fichiers, commandes,
navigation) passent par une demande d'approbation, dont le mode est configurable
dans les réglages. L'utilisateur reste responsable de ce qu'il approuve :

- les commandes lancées s'exécutent avec les droits du compte qui a ouvert
  l'application ;
- les fichiers écrits le sont sous le dossier de travail configuré, sauf
  autorisation explicite ;
- l'annulation d'un run (`Annuler ce run`) couvre les écritures journalisées par
  la transaction, pas les effets externes (envoi d'un message, achat, dépôt
  distant).

## Clés et comptes tiers

L'application n'héberge aucune clé : les clés d'API sont fournies par
l'utilisateur, stockées localement (voir `docs/PRIVACY.md`), et consommées
directement auprès du fournisseur choisi. Les conditions et la facturation de ce
fournisseur s'appliquent aux appels effectués avec ces clés. Les plafonds de
dépense de l'application limitent ce que le logiciel envoie ; ils ne remplacent
pas les limites fixées chez le fournisseur.

## Usage acceptable

L'utilisateur s'engage à ne pas utiliser l'application pour porter atteinte aux
droits de tiers, contourner des protections techniques, ou automatiser des
actions interdites par les conditions d'un site visité. Les outils de navigation
respectent `robots.txt` et les limites de débit configurées ; les désactiver
reste un choix de l'utilisateur, qui en assume les conséquences.

## Données et confidentialité

Le traitement des données est décrit dans `docs/PRIVACY.md`, qui fait partie de
ces conditions : ce qui reste sur la machine, ce qui sort, comment tout
supprimer.

## Mises à jour

Le logiciel peut être mis à jour en remplaçant l'exécutable ou en rebâtissant
depuis les sources. Aucune mise à jour n'est imposée à distance : l'application
ne se connecte pas à un serveur de l'éditeur pour se mettre à jour.

## Garanties et limitation

Le logiciel est fourni sans garantie de disponibilité, d'absence d'erreur ou
d'adéquation à un usage particulier. Dans la limite permise par la loi,
l'éditeur ne répond pas des pertes de données, pertes d'exploitation ou
dommages indirects résultant de l'utilisation du logiciel. Cette limitation ne
s'applique pas en cas de faute lourde ou de dommage corporel, ni aux droits que
la loi accorde impérativement au consommateur.

## Droit applicable

Les présentes conditions sont régies par le droit français. En cas de litige,
une solution amiable sera recherchée avant toute action judiciaire ; à défaut,
les tribunaux compétents sont ceux du ressort du domicile de l'utilisateur
consommateur.

## Contact

Les remarques et demandes relatives à ces conditions passent par les tickets du
dépôt du projet.
