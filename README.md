# dedup-medias-scripts

Ce dépôt contient deux variantes quasi identiques du script de déduplication de médias :
- `dedupe_media.py`
- `dedupe_medias.py`

## Différences fonctionnelles entre les deux scripts
- **Support ANSI/VT pour Windows** : `dedupe_medias.py` active le *Virtual Terminal* afin que les séquences ANSI (utilisées pour réécrire les lignes de statut) fonctionnent correctement sur Windows. Sans cette section, certains terminaux Windows n'affichent pas le spinner et les mises à jour sur deux lignes. 【F:dedupe_medias.py†L32-L55】
- **Fonctionnalités communes** : en dehors de cette initialisation spécifique à Windows, les deux fichiers partagent le même comportement : détection des doublons via SHA-256, affichage en temps réel du débit et du chemin parcouru, puis suppression interactive ou automatique des fichiers en doublon. 【F:dedupe_media.py†L5-L120】

## Suggestions d'amélioration
- **Robustesse d'E/S** : ignorer explicitement les liens symboliques ou les fichiers inaccessibles dès l'itération pour éviter les erreurs inattendues (actuellement seules les erreurs de lecture/`stat` sont journalisées plus tard).【F:dedupe_media.py†L116-L160】
- **Sécurité des suppressions** : proposer un mode de mise en quarantaine (déplacement vers un dossier dédié) plutôt qu'une suppression directe, afin de limiter les risques d'effacement irréversible.
- **Tests automatisés** : ajouter une suite de tests unitaires couvrant l'itération des fichiers, le calcul de hash par chunk et la logique de regroupement par taille/hash pour sécuriser les évolutions.
- **Performance** : autoriser un réglage du nombre de fichiers hashed en parallèle (via `concurrent.futures`) sur les disques NVMe tout en gardant un mode séquentiel par défaut pour les HDD, en veillant à limiter l'empreinte mémoire.
- **Ergonomie** :
  - ajouter une option `--extensions` pour réduire le périmètre aux types souhaités.
  - autoriser la génération d'un rapport JSON/CSV listant les doublons (hash, fichier conservé, fichiers supprimables) pour audit.
  - afficher un résumé final plus détaillé (par exemple espace gagné par groupe, nombre de groupes ignorés faute de droits).
- **Portabilité Windows** : dans `dedupe_media.py`, reprendre la séquence d'activation ANSI déjà présente dans `dedupe_medias.py` afin d'harmoniser l'expérience multi-plateforme.【F:dedupe_medias.py†L32-L55】

## Exemples d'usage
- **Scan simple sans suppression (prévisualisation)**
  ```bash
  python dedupe_media.py "D:\\Photos" --dry-run
  ```
- **Scan avec journal détaillé**
  ```bash
  python dedupe_media.py "/mnt/donnees/photos" --log-file dedupe.log
  ```
- **Suppression automatique de tous les doublons (à utiliser avec prudence)**
  ```bash
  python dedupe_media.py ~/Images --assume-yes
  ```
- **Utilisation de la variante avec support ANSI amélioré (Windows)**
  ```bash
  python dedupe_medias.py "C:\\Users\\Moi\\Pictures"
  ```

Les options communes :
- `--dry-run` : affiche les groupes de doublons sans supprimer.
- `--log-file <chemin>` : enregistre toutes les opérations dans un fichier.
- `--assume-yes` / `-y` : supprime automatiquement tous les doublons sans demander confirmation.

> Conseil : lancez toujours un premier passage avec `--dry-run` et sauvegardez vos médias avant toute suppression automatique.
