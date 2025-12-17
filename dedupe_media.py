#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
dedupe_media.py
- Détection de doublons image/vidéo par SHA-256 (contenu seul).
- Progression live sur 2 lignes :
    (N/Total) - % - <débit> (avg <débit>) - (Files matches : X)
    Parsing fichier : <chemin>
- Spinner "..." pour les phases silencieuses (scan, regroupement).
- Bilan avant suppression : nb de fichiers supprimables + espace récupérable.
- Suppression interactive [Y/n/all] (par défaut = Yes) ou automatique via --assume-yes / -y.
- --dry-run pour ne rien supprimer. Logs optionnels via --log-file.

Usage :
    python dedupe_media.py "C:\\chemin\\vers\\dossier" [--dry-run] [--log-file LOG] [-y]
"""

from __future__ import annotations
import argparse
import hashlib
import logging
import os
import sys
import time
import threading
import shutil
from pathlib import Path
from typing import Dict, List, Iterable, Tuple
from collections import deque

# --------- Extensions gérées (insensibles à la casse) ----------
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif", ".webm"
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".mpeg", ".mpg", ".m4v", ".3gp", ".flv", ".webm"
}
ALLOWED_EXTS = {e.lower() for e in (IMAGE_EXTS | VIDEO_EXTS)}

CHUNK_SIZE = 1024 * 1024  # 1 MiB


# -------------------- Utils d’affichage ------------------------
def term_width(fallback: int = 120) -> int:
    try:
        return max(40, shutil.get_terminal_size(fallback=(fallback, 20)).columns)
    except Exception:
        return fallback

def truncate_middle(s: str, maxlen: int) -> str:
    """Tronque au milieu : 'C:\\debut\\...\\fin.ext' pour tenir sur la ligne."""
    if len(s) <= maxlen:
        return s
    if maxlen <= 3:
        return s[:maxlen]
    head = maxlen // 2 - 2
    tail = maxlen - head - 3
    return f"{s[:head]}...{s[-tail:]}"

def write_line_overwrite(text: str) -> None:
    width = term_width()
    sys.stdout.write("\r" + text.ljust(width - 1)[:width - 1])
    sys.stdout.flush()

def write_two_lines_overwrite(line1: str, line2: str) -> None:
    """Écrit/rafraîchit 2 lignes, puis remonte le curseur de 2 lignes pour réécriture au même endroit."""
    width = term_width()
    out = (
        "\r"
        + line1.ljust(width - 1)[:width - 1]
        + "\n"
        + line2.ljust(width - 1)[:width - 1]
    )
    sys.stdout.write(out)
    # Remonte le curseur de 2 lignes (ANSI). Windows 10+ supporte l’ANSI dans le terminal moderne.
    sys.stdout.write("\x1b[2A")
    sys.stdout.flush()

def clear_two_status_lines() -> None:
    width = term_width()
    sys.stdout.write("\r" + " " * (width - 1) + "\n" + " " * (width - 1) + "\r")
    sys.stdout.flush()


# ---------------------- Spinner -------------------------------
class Spinner:
    """Petit spinner à points, nettoie proprement la ligne à l’arrêt."""
    def __init__(self, label: str = ""):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def stop(self):
        self._stop.set()
        self._thread.join()
        # Efface (2 lignes pour rester cohérent avec l’affichage 2 lignes)
        clear_two_status_lines()

    def _run(self):
        dots = ["", ".", "..", "..."]
        i = 0
        while not self._stop.is_set():
            msg = f"{self.label}{dots[i % len(dots)]}"
            write_line_overwrite(msg)
            i += 1
            time.sleep(0.3)


# -------------------- Formats lisibles ------------------------
def human_size(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f} {u}"
        size /= 1024.0

def human_rate(bps: float) -> str:
    return human_size(bps) + "/s"


# -------------------- I/O Stats pour le débit -----------------
class IOStats:
    """Débit moyen depuis le début + “instantané” lissé (fenêtre glissante)."""
    def __init__(self, window_sec: float = 1.0):
        self.start = time.perf_counter()
        self.total_bytes = 0
        self.window = deque()  # (timestamp, bytes_incr)
        self.window_sec = window_sec

    def add(self, n: int) -> None:
        t = time.perf_counter()
        self.total_bytes += n
        self.window.append((t, n))
        cutoff = t - self.window_sec
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()

    def avg_bps(self) -> float:
        elapsed = max(1e-6, time.perf_counter() - self.start)
        return self.total_bytes / elapsed

    def inst_bps(self) -> float:
        now = time.perf_counter()
        bytes_in_window = 0
        earliest = None
        for (t, n) in self.window:
            if now - t <= self.window_sec:
                bytes_in_window += n
                earliest = t if earliest is None else earliest
        if not bytes_in_window:
            return 0.0
        elapsed = min(self.window_sec, max(1e-6, now - earliest))
        return bytes_in_window / elapsed


# -------------------- Tracker “files matched” -----------------
class MatchTracker:
    """
    Compte en direct le nombre TOTAL de fichiers appartenant à des groupes de doublons
    (dès qu’un hash apparaît au moins 2 fois, les 2 fichiers sont comptés, puis +1 par fichier suivant).
    """
    def __init__(self):
        self.counts: Dict[str, int] = {}
        self.matched_files: int = 0

    def add(self, digest: str) -> None:
        c = self.counts.get(digest, 0)
        if c == 0:
            self.counts[digest] = 1
        elif c == 1:
            self.counts[digest] = 2
            self.matched_files += 2  # les deux premiers comptent
        else:
            self.counts[digest] = c + 1
            self.matched_files += 1


# ------------------------ Cœur logique ------------------------
def iter_media_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
            yield p

def sha256_file(path: Path, iostats: IOStats | None = None, progress_hook=None) -> str:
    """Calcule SHA-256 en lisant par chunks. Appelle progress_hook(bytes_lus) périodiquement."""
    h = hashlib.sha256()
    bytes_read = 0
    last_update = 0.0
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            n = len(chunk)
            bytes_read += n
            if iostats:
                iostats.add(n)
            now = time.perf_counter()
            if progress_hook and (now - last_update) >= 0.15:
                last_update = now
                try:
                    progress_hook(bytes_read)
                except Exception:
                    pass
    if progress_hook:
        try:
            progress_hook(bytes_read)
        except Exception:
            pass
    return h.hexdigest()

def group_by_size(paths: Iterable[Path]) -> Dict[int, List[Path]]:
    by_size: Dict[int, List[Path]] = {}
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError as e:
            logging.warning("Impossible de lire la taille de %s : %s", p, e)
            continue
        by_size.setdefault(size, []).append(p)
    return by_size

def print_progress_two_lines(current: int, total: int, path: Path, inst_bps: float,
                             avg_bps: float, files_matched: int) -> None:
    pct = int((current / total) * 100) if total else 0
    line1 = f"({current}/{total}) - {pct}% - {human_rate(inst_bps)} (avg {human_rate(avg_bps)}) - (Files matches : {files_matched})"
    width = term_width()
    prefix = "Parsing fichier : "
    max_path = max(10, width - len(prefix) - 2)
    shown = truncate_middle(str(path), max_path)
    line2 = prefix + shown
    write_two_lines_overwrite(line1, line2)

def group_by_hash(paths: Iterable[Path], iostats: IOStats, counter_offset: int,
                  total_to_hash: int, tracker: MatchTracker) -> Dict[str, List[Path]]:
    """Hash chaque fichier de 'paths' avec progression 2 lignes; renvoie {digest: [paths]}."""
    by_hash: Dict[str, List[Path]] = {}
    count = counter_offset
    for p in paths:
        count += 1

        def progress_hook(_bytes_read_file: int):
            print_progress_two_lines(
                count, total_to_hash, p,
                iostats.inst_bps(), iostats.avg_bps(),
                tracker.matched_files
            )

        try:
            digest = sha256_file(p, iostats=iostats, progress_hook=progress_hook).lower()
            tracker.add(digest)  # MAJ du compteur “Files matches”
            by_hash.setdefault(digest, []).append(p)
        except (OSError, IOError) as e:
            clear_two_status_lines()
            logging.warning("Impossible de lire %s : %s", p, e)
    return by_hash

def find_duplicate_groups(root: Path) -> Dict[str, List[Path]]:
    """Retourne {hash: [fichiers]} pour les doublons. Gère proprement Ctrl+C (retour partiel)."""
    logging.info("Scan du dossier : %s", root)

    with Spinner("Scan des fichiers médias"):
        all_media = list(iter_media_files(root))
    clear_two_status_lines()
    logging.info("Fichiers candidats trouvés : %d", len(all_media))

    with Spinner("Regroupement par taille"):
        by_size = group_by_size(all_media)
    clear_two_status_lines()
    logging.info("Groupes par taille : %d", len(by_size))

    files_to_hash: List[Path] = []
    for size, files in by_size.items():
        if len(files) >= 2:
            files_to_hash.extend(files)

    total_to_hash = len(files_to_hash)
    logging.info("Fichiers potentiellement en doublon (à hasher) : %d", total_to_hash)

    if total_to_hash == 0:
        return {}

    iostats = IOStats(window_sec=1.0)
    tracker = MatchTracker()
    duplicates_by_hash: Dict[str, List[Path]] = {}
    processed = 0

    try:
        for size, files in by_size.items():
            if len(files) < 2:
                continue
            by_hash = group_by_hash(
                files, iostats=iostats,
                counter_offset=processed, total_to_hash=total_to_hash,
                tracker=tracker
            )
            processed += len(files)
            for digest, paths in by_hash.items():
                if len(paths) > 1:
                    duplicates_by_hash.setdefault(digest, []).extend(paths)
    except KeyboardInterrupt:
        clear_two_status_lines()
        logging.warning("Interruption utilisateur : retour partiel des doublons trouvés jusque-là.")
    finally:
        clear_two_status_lines()
        logging.info("Fichiers effectivement hashés : %d", processed)
        logging.info("Groupes de doublons trouvés : %d", len(duplicates_by_hash))

    return duplicates_by_hash

def sort_paths_for_original(paths: List[Path]) -> List[Path]:
    return sorted(paths, key=lambda p: (len(str(p)), str(p).lower()))

def compute_stats(dupes: Dict[str, List[Path]]) -> Tuple[int, int]:
    total_files = 0
    total_bytes = 0
    for _digest, paths in dupes.items():
        if len(paths) <= 1:
            continue
        paths_sorted = sort_paths_for_original(paths)
        for c in paths_sorted[1:]:
            try:
                total_bytes += c.stat().st_size
                total_files += 1
            except OSError as e:
                logging.warning("Impossible de lire la taille de %s : %s", c, e)
    return total_files, total_bytes


# ------------------- Suppressions ------------------------------
def prompt_delete(path: Path, state: dict) -> bool:
    """
    Prompt [Y/n/all] :
      - Entrée / Y => supprime
      - n          => conserve
      - all        => active le mode 'tout supprimer' pour la suite
    """
    if state.get("all_mode", False):
        return True

    while True:
        resp = input("Supprimer ce fichier ? [Y/n/all] : ").strip().lower()
        if resp in ("", "y", "yes", "o", "oui"):
            return True
        if resp in ("n", "no", "non"):
            return False
        if resp == "all":
            state["all_mode"] = True
            return True
        print("Réponse non reconnue. Tapez 'Y' (ou Entrée), 'n' ou 'all'.")

def delete_file(path: Path) -> Tuple[bool, str]:
    try:
        os.remove(path)
        return True, f"Supprimé : {path}"
    except Exception as e:
        return False, f"Échec suppression {path} : {e}"

def process_deletions(dupes: Dict[str, List[Path]], dry_run: bool, assume_yes: bool) -> None:
    groups = 0
    total_candidates = 0
    total_deleted = 0
    state = {"all_mode": False}

    if assume_yes:
        state["all_mode"] = True  # -y équivaut à "all" dès le départ

    for digest, paths in dupes.items():
        groups += 1
        paths_sorted = sort_paths_for_original(paths)
        original = paths_sorted[0]
        candidates = paths_sorted[1:]
        total_candidates += len(candidates)

        try:
            size_bytes = original.stat().st_size
        except OSError:
            size_bytes = None

        print("\n" + "=" * 80)
        print(f"Doublons #{groups} — SHA256={digest}")
        if size_bytes is not None:
            print(f"Taille : {human_size(size_bytes)}")
        print("Original conservé :")
        print(f"  - {original}")

        if not candidates:
            continue

        print("Doublons potentiels :")
        for i, c in enumerate(candidates, 1):
            print(f"  {i:2d}. {c}")

        if dry_run:
            print("\n[DRY-RUN] Aucun fichier ne sera supprimé.")
            continue

        for c in candidates:
            do_delete = True if state.get("all_mode") else prompt_delete(c, state)
            if do_delete:
                ok, msg = delete_file(c)
                if ok:
                    total_deleted += 1
                    logging.info(msg)
                else:
                    logging.error(msg)
            else:
                logging.info("Conservé (choix utilisateur) : %s", c)

    print("\n" + "-" * 80)
    print(f"Groupes de doublons traités : {groups}")
    print(f"Fichiers doublons proposés/susceptibles de suppression : {total_candidates}")
    if not dry_run:
        print(f"Fichiers effectivement supprimés : {total_deleted}")
    else:
        print("[DRY-RUN] Aucune suppression effectuée.")


# ---------------------- Logging & main -------------------------
def configure_logging(log_file: str | None, verbose_console: bool = True) -> None:
    handlers = []
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    if verbose_console:
        ch = logging.StreamHandler()  # vers stderr
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        handlers.append(ch)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        handlers.append(fh)
    logging.basicConfig(level=logging.DEBUG if log_file else logging.INFO, handlers=handlers)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trouve et gère les doublons (contenu identique) d'images et de vidéos dans un dossier."
    )
    parser.add_argument("folder", type=Path, help="Dossier racine à analyser (ex: C:\\Users\\Moi\\Pictures)")
    parser.add_argument("--dry-run", action="store_true", help="N'affiche que les doublons, ne supprime rien.")
    parser.add_argument("--log-file", type=str, default=None, help="Chemin du fichier de logs (optionnel).")
    parser.add_argument("--assume-yes", "-y", action="store_true",
                        help="Supprime automatiquement tous les doublons sans demander (dangereux).")
    args = parser.parse_args()

    if not args.folder.exists() or not args.folder.is_dir():
        print(f"Erreur : le dossier spécifié n'existe pas ou n'est pas un dossier : {args.folder}")
        raise SystemExit(2)

    configure_logging(args.log_file)

    logging.info("Extensions prises en compte : %s", ", ".join(sorted(ALLOWED_EXTS)))
    logging.info("Mode dry-run : %s", args.dry_run)
    logging.info("Suppression auto (--assume-yes / -y) : %s", args.assume_yes)

    duplicates = find_duplicate_groups(args.folder)

    if not duplicates:
        print("Aucun doublon trouvé. 🎉")
        return

    # ---- Bilan avant suppressions ----
    deletable_count, reclaim_bytes = compute_stats(duplicates)
    print("\n" + "#" * 80)
    print("BILAN AVANT SUPPRESSION")
    print(f"Fichiers supprimables (si vous supprimez tous les doublons) : {deletable_count}")
    print(f"Espace potentiellement récupérable : {human_size(reclaim_bytes)}")
    if args.dry_run:
        print("[DRY-RUN] Rien ne sera supprimé, affichage des groupes seulement.")
    elif args.assume_yes:
        print("[MODE AUTO] Tous les doublons seront supprimés sans confirmation.")
    else:
        print("Des confirmations [Y/n/all] seront demandées pour chaque doublon.")
    print("#" * 80 + "\n")

    process_deletions(duplicates, dry_run=args.dry_run, assume_yes=args.assume_yes)
    print("\nTerminé.")

if __name__ == "__main__":
    main()