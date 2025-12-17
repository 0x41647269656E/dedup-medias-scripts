# dedup-medias-scripts

Script unique `dedupe_medias.py` pour détecter et traiter les doublons image/vidéo via SHA-256 (lecture par chunks).
Il affiche une progression en temps réel, propose la suppression ou la mise en quarantaine des doublons et peut produire
un rapport CSV/JSON pour audit.

## Fonctionnalités principales
- Ignorer explicitement les liens symboliques et fichiers inaccessibles lors du scan.
- Calcul de hash par chunks (1 MiB) avec compteur de débit instantané/moyen.
- Option `--workers` pour paralléliser le hachage sur SSD/NVMe (séquentiel par défaut).
- Sélection des extensions à traiter via `--extensions` (par défaut photos/vidéos usuelles).
- Mode quarantaine (`--quarantine-dir`) pour déplacer les doublons au lieu de les supprimer.
- Suppression interactive ou automatique (`--assume-yes`), support du mode `--dry-run`.
- Rapports CSV/JSON listant hash, fichier conservé et doublons déplaçables/supprimables.
- Résumé final détaillé (espace récupéré, groupes en erreur, fichiers ignorés, etc.).

## Installation
Python 3.9+ suffit ; le script n'a pas de dépendance runtime hors standard library.

### Installer les dépendances (tests inclus)
```bash
python -m venv .venv
source .venv/bin/activate  # sous Windows : .venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

### Exécuter le script
```bash
python dedupe_medias.py /chemin/vers/mes/medias --dry-run
```

## Options CLI
- `folder` : dossier racine à analyser.
- `--dry-run` : n’exécute aucune suppression/déplacement.
- `--assume-yes` / `-y` : supprime ou déplace tous les doublons sans confirmation.
- `--log-file` : journal détaillé des opérations.
- `--extensions` : liste (séparée par virgules) des extensions à considérer, ex : `--extensions .jpg,.png`.
- `--workers` : nombre de fichiers hashés en parallèle (>=1).
- `--quarantine-dir` : dossier de destination pour déplacer les doublons (sécurise les suppressions).
- `--report-json` / `--report-csv` : chemins des rapports listant les groupes de doublons.

## Exemples
- Prévisualisation sans action destructive :
  ```bash
  python dedupe_medias.py "~/Photos" --dry-run
  ```
- Déplacement des doublons en quarantaine avec rapport JSON :
  ```bash
  python dedupe_medias.py "~/Photos" --quarantine-dir ./quarantaine --report-json rapports/doublons.json
  ```
- Suppression automatique et rapport CSV ciblé sur quelques extensions :
  ```bash
  python dedupe_medias.py "~/Videos" --assume-yes --extensions .mp4,.mkv --report-csv rapports/doublons.csv
  ```
- Optimisation sur SSD/NVMe avec plusieurs workers :
  ```bash
  python dedupe_medias.py "D:/Media" --workers 4 --log-file dedupe.log
  ```

> Astuce : lancer d’abord un `--dry-run`, puis utiliser `--quarantine-dir` pour sécuriser avant toute suppression définitive.

## Tests
Lancer la suite de tests Pytest :

```bash
python -m pytest
```
