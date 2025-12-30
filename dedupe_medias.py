#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
dedupe_medias.py
- Détection de doublons image/vidéo par SHA-256 (contenu seul).
 - Progression live sur 3 lignes :
    (N/Total) - % - <débit> (avg <débit>) - (Files matches : X)
    Parsing fichier : <chemin>
    [=====>....] % (N/Total)  # barre de progression style wget
- Spinner "..." pour les phases silencieuses (scan, regroupement).
- Bilan avant suppression : nb de fichiers supprimables + espace récupérable.
- Suppression interactive [Y/n/all] (par défaut = Yes) ou automatique via --assume-yes / -y.
- Mode quarantaine optionnel pour déplacer les doublons plutôt que les supprimer.
- Rapport CSV/JSON optionnel listant les doublons détectés.
- --dry-run pour ne rien supprimer. Logs optionnels via --log-file.

Usage :
    python dedupe_medias.py "C:\\chemin\\vers\\dossier" [--dry-run] [--log-file LOG] [-y]
"""

from __future__ import annotations
import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import time
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Iterable, Tuple, Set, Optional, Callable
from collections import deque

# ---------- Support ANSI / Virtual Terminal (Windows) ----------
HAS_VT = False
def _enable_vt_mode() -> bool:
    """Essaie d'activer les séquences ANSI sur Windows; True si dispo."""
    import os, sys
    if os.name != 'nt':
        return True
    try:
        import msvcrt  # noqa: F401
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)) == 0:
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if kernel32.SetConsoleMode(h, new_mode) == 0:
            return False
        return True
    except Exception:
        return False

HAS_VT = _enable_vt_mode()

# --------- Extensions gérées (insensibles à la casse) ----------
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic", ".heif", ".webm"
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".wmv", ".mpeg", ".mpg", ".m4v", ".3gp", ".flv", ".webm"
}
DEFAULT_ALLOWED_EXTS = {e.lower() for e in (IMAGE_EXTS | VIDEO_EXTS)}

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def resolve_extensions(raw_exts: Optional[List[str]]) -> Set[str]:
    if not raw_exts:
        return set(DEFAULT_ALLOWED_EXTS)
    resolved: Set[str] = set()
    for ext in raw_exts:
        for part in ext.split(','):
            cleaned = part.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith('.'):
                cleaned = '.' + cleaned
            resolved.add(cleaned)
    return resolved if resolved else set(DEFAULT_ALLOWED_EXTS)


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

def write_status_lines(lines: List[str]) -> None:
    """
    Affiche un bloc de lignes en le réécrivant au même endroit.
    - Si les séquences ANSI sont dispo : on remonte le curseur.
    - Sinon : fallback sur une seule ligne pour éviter le flood console.
    """
    if not HAS_VT and len(lines) > 1:
        write_line_overwrite(" | ".join(lines))
        return

    width = term_width()
    padded = [ln.ljust(width - 1)[:width - 1] for ln in lines]
    out = "\r" + "\n".join(padded)
    sys.stdout.write(out)
    sys.stdout.write(f"\x1b[{len(lines)}A")
    sys.stdout.flush()

def clear_status_lines(nb_lines: int = 2) -> None:
    if not HAS_VT:
        # Sur les consoles sans ANSI, on efface uniquement la ligne courante.
        write_line_overwrite("")
        return
    width = term_width()
    blank = " " * (width - 1)
    out = "\r" + "\n".join(blank for _ in range(nb_lines))
    out += f"\x1b[{nb_lines}A"
    sys.stdout.write(out)
    sys.stdout.flush()

def clear_two_status_lines() -> None:
    clear_status_lines(2)


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
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            t = time.perf_counter()
            self.total_bytes += n
            self.window.append((t, n))
            cutoff = t - self.window_sec
            while self.window and self.window[0][0] < cutoff:
                self.window.popleft()

    def avg_bps(self) -> float:
        with self._lock:
            elapsed = max(1e-6, time.perf_counter() - self.start)
            return self.total_bytes / elapsed

    def inst_bps(self) -> float:
        with self._lock:
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
def iter_media_files(root: Path, allowed_exts: Set[str], counters: Optional[Dict[str, int]] = None) -> Iterable[Path]:
    for p in root.rglob("*"):
        try:
            if p.is_symlink():
                if counters is not None:
                    counters["symlinks"] = counters.get("symlinks", 0) + 1
                logging.debug("Lien symbolique ignoré : %s", p)
                continue
            if not p.is_file():
                continue
            if p.suffix.lower() not in allowed_exts:
                continue
            try:
                p.stat()
            except OSError as e:
                logging.warning("Impossible d'accéder à %s : %s", p, e)
                if counters is not None:
                    counters["inaccessible"] = counters.get("inaccessible", 0) + 1
                continue
            yield p
        except OSError as e:
            logging.warning("Erreur lors du parcours de %s : %s", p, e)
            if counters is not None:
                counters["inaccessible"] = counters.get("inaccessible", 0) + 1

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

    bar_width = max(10, min(60, width - 30))
    filled = int(bar_width * pct / 100)
    rest = bar_width - filled
    if rest > 0 and filled < bar_width:
        bar_body = "=" * filled + ">" + "." * (rest - 1)
    else:
        bar_body = "=" * bar_width
    bar_line = f"[{bar_body}] {pct:3d}% ({current}/{total})"

    write_status_lines([line1, line2, bar_line])

def group_by_hash(paths: Iterable[Path], iostats: IOStats, counter_offset: int,
                  total_to_hash: int, tracker: MatchTracker, max_workers: int = 1,
                  progress_lock: Optional[threading.Lock] = None) -> Dict[str, List[Path]]:
    """Hash chaque fichier de 'paths' avec progression 2 lignes; renvoie {digest: [paths]}.

    Si max_workers > 1, le hachage est parallélisé (ThreadPoolExecutor) en conservant
    l'affichage de progression via un verrou partagé.
    """
    by_hash: Dict[str, List[Path]] = {}
    tracker_lock = threading.Lock()
    progress_lock = progress_lock or threading.Lock()

    def build_progress_hook(current_index: int, path: Path) -> Callable[[int], None]:
        def _progress(_bytes_read_file: int) -> None:
            with progress_lock:
                print_progress_two_lines(
                    current_index, total_to_hash, path,
                    iostats.inst_bps(), iostats.avg_bps(),
                    tracker.matched_files
                )
        return _progress

    def hash_one(path: Path, current_index: int) -> Tuple[Optional[str], Path, Optional[Exception]]:
        try:
            digest = sha256_file(path, iostats=iostats,
                                 progress_hook=build_progress_hook(current_index, path)).lower()
            with tracker_lock:
                tracker.add(digest)
            return digest, path, None
        except (OSError, IOError) as e:
            with progress_lock:
                clear_status_lines(3)
            logging.warning("Impossible de lire %s : %s", path, e)
            return None, path, e

    if max_workers <= 1:
        for idx, p in enumerate(paths, start=1):
            digest, path, _err = hash_one(p, counter_offset + idx)
            if digest:
                by_hash.setdefault(digest, []).append(path)
        return by_hash

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {}
        for idx, p in enumerate(paths, start=1):
            future = executor.submit(hash_one, p, counter_offset + idx)
            future_to_path[future] = p

        for future in as_completed(future_to_path):
            digest, path, _err = future.result()
            if digest:
                by_hash.setdefault(digest, []).append(path)

    return by_hash

def find_duplicate_groups(root: Path, allowed_exts: Set[str], max_workers: int,
                          counters: Dict[str, int]) -> Dict[str, List[Path]]:
    """Retourne {hash: [fichiers]} pour les doublons. Gère proprement Ctrl+C (retour partiel)."""
    logging.info("Scan du dossier : %s", root)

    with Spinner("Scan des fichiers médias"):
        all_media = list(iter_media_files(root, allowed_exts=allowed_exts, counters=counters))
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

    progress_lock = threading.Lock()

    try:
        for size, files in by_size.items():
            if len(files) < 2:
                continue
            by_hash = group_by_hash(
                files, iostats=iostats,
                counter_offset=processed, total_to_hash=total_to_hash,
                tracker=tracker, max_workers=max_workers,
                progress_lock=progress_lock
            )
            processed += len(files)
            for digest, paths in by_hash.items():
                if len(paths) > 1:
                    duplicates_by_hash.setdefault(digest, []).extend(paths)
    except KeyboardInterrupt:
        clear_status_lines(3)
        logging.warning("Interruption utilisateur : retour partiel des doublons trouvés jusque-là.")
    finally:
        clear_status_lines(3)
        logging.info("Fichiers effectivement hashés : %d", processed)
        logging.info("Groupes de doublons trouvés : %d", len(duplicates_by_hash))
        counters["hashed_files"] = counters.get("hashed_files", 0) + processed

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


def write_reports(dupes: Dict[str, List[Path]], report_json: Optional[Path],
                  report_csv: Optional[Path]) -> None:
    if not report_json and not report_csv:
        return

    entries = []
    for digest, paths in dupes.items():
        if len(paths) <= 1:
            continue
        paths_sorted = sort_paths_for_original(paths)
        keeper = paths_sorted[0]
        duplicates = paths_sorted[1:]
        try:
            size_bytes = keeper.stat().st_size
        except OSError:
            size_bytes = None
        entries.append({
            "hash": digest,
            "size_bytes": size_bytes,
            "keeper": str(keeper),
            "duplicates": [str(p) for p in duplicates],
        })

    if report_json:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        with report_json.open("w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        logging.info("Rapport JSON écrit dans %s", report_json)

    if report_csv:
        report_csv.parent.mkdir(parents=True, exist_ok=True)
        with report_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["hash", "size_bytes", "keeper", "duplicate"])
            writer.writeheader()
            for entry in entries:
                for dup in entry["duplicates"]:
                    writer.writerow({
                        "hash": entry["hash"],
                        "size_bytes": entry["size_bytes"],
                        "keeper": entry["keeper"],
                        "duplicate": dup,
                    })
        logging.info("Rapport CSV écrit dans %s", report_csv)


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


def quarantine_file(path: Path, quarantine_dir: Path, base_folder: Path) -> Tuple[bool, str]:
    try:
        try:
            rel = path.relative_to(base_folder)
        except ValueError:
            rel = Path(path.name)
        target = quarantine_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        candidate = target
        counter = 1
        while candidate.exists():
            candidate = candidate.with_name(f"{target.stem}_{counter}{target.suffix}")
            counter += 1
        shutil.move(str(path), str(candidate))
        return True, f"Déplacé en quarantaine : {candidate}"
    except Exception as e:
        return False, f"Échec déplacement quarantaine {path} : {e}"

def process_deletions(dupes: Dict[str, List[Path]], dry_run: bool, assume_yes: bool,
                      quarantine_dir: Optional[Path], base_folder: Path) -> Dict[str, int]:
    groups = 0
    total_candidates = 0
    total_deleted = 0
    total_quarantined = 0
    total_failed = 0
    reclaimed_bytes = 0
    groups_with_errors = 0
    state = {"all_mode": False}

    if assume_yes:
        state["all_mode"] = True  # -y équivaut à "all" dès le départ

    for digest, paths in dupes.items():
        groups += 1
        paths_sorted = sort_paths_for_original(paths)
        original = paths_sorted[0]
        candidates = paths_sorted[1:]
        total_candidates += len(candidates)
        group_error = False

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
            if not do_delete:
                logging.info("Conservé (choix utilisateur) : %s", c)
                continue

            try:
                size = c.stat().st_size
            except OSError:
                size = 0

            if quarantine_dir:
                ok, msg = quarantine_file(c, quarantine_dir=quarantine_dir, base_folder=base_folder)
                if ok:
                    total_quarantined += 1
                    reclaimed_bytes += size
                    logging.info(msg)
                else:
                    group_error = True
                    total_failed += 1
                    logging.error(msg)
            else:
                ok, msg = delete_file(c)
                if ok:
                    total_deleted += 1
                    reclaimed_bytes += size
                    logging.info(msg)
                else:
                    group_error = True
                    total_failed += 1
                    logging.error(msg)

        if group_error:
            groups_with_errors += 1

    print("\n" + "-" * 80)
    print(f"Groupes de doublons traités : {groups}")
    print(f"Fichiers doublons proposés/susceptibles de suppression : {total_candidates}")
    if dry_run:
        print("[DRY-RUN] Aucune suppression effectuée.")
    elif quarantine_dir:
        print(f"Fichiers déplacés en quarantaine : {total_quarantined}")
    else:
        print(f"Fichiers effectivement supprimés : {total_deleted}")
    print(f"Espace récupéré : {human_size(reclaimed_bytes)}")
    if groups_with_errors:
        print(f"Groupes ayant rencontré des erreurs : {groups_with_errors}")

    return {
        "groups": groups,
        "candidates": total_candidates,
        "deleted": total_deleted,
        "quarantined": total_quarantined,
        "failed": total_failed,
        "reclaimed_bytes": reclaimed_bytes,
        "groups_with_errors": groups_with_errors,
    }


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
    parser.add_argument("--extensions", action="append", help="Limiter aux extensions voulues (ex: --extensions .jpg,.png)")
    parser.add_argument("--report-json", type=Path, help="Chemin du rapport JSON des doublons détectés.")
    parser.add_argument("--report-csv", type=Path, help="Chemin du rapport CSV des doublons détectés.")
    parser.add_argument("--quarantine-dir", type=Path,
                        help="Dossier de quarantaine (déplacement des doublons au lieu de les supprimer).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Nombre de fichiers à hasher en parallèle (par défaut 1, augmenter sur SSD/NVMe).")
    args = parser.parse_args()

    if not args.folder.exists() or not args.folder.is_dir():
        print(f"Erreur : le dossier spécifié n'existe pas ou n'est pas un dossier : {args.folder}")
        raise SystemExit(2)

    configure_logging(args.log_file)

    allowed_exts = resolve_extensions(args.extensions)
    logging.info("Extensions prises en compte : %s", ", ".join(sorted(allowed_exts)))
    logging.info("Mode dry-run : %s", args.dry_run)
    logging.info("Suppression auto (--assume-yes / -y) : %s", args.assume_yes)
    logging.info("Travailleurs parallèles (hash) : %d", args.workers)

    counters: Dict[str, int] = {}

    duplicates = find_duplicate_groups(args.folder, allowed_exts=allowed_exts,
                                       max_workers=max(1, args.workers), counters=counters)

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

    if args.quarantine_dir and not args.dry_run:
        args.quarantine_dir.mkdir(parents=True, exist_ok=True)

    report_json_path = args.report_json if args.report_json else None
    report_csv_path = args.report_csv if args.report_csv else None
    write_reports(duplicates, report_json=report_json_path, report_csv=report_csv_path)

    summary = process_deletions(
        duplicates, dry_run=args.dry_run, assume_yes=args.assume_yes,
        quarantine_dir=args.quarantine_dir, base_folder=args.folder,
    )

    print("\nRÉCAPITULATIF")
    print(f"  Groupes analysés : {summary['groups']}")
    print(f"  Fichiers doublons candidats : {summary['candidates']}")
    print(f"  Espace réellement récupéré : {human_size(summary['reclaimed_bytes'])}")
    print(f"  Fichiers déplacés en quarantaine : {summary['quarantined']}")
    print(f"  Fichiers supprimés : {summary['deleted']}")
    if summary['groups_with_errors']:
        print(f"  Groupes ignorés/partiels suite à erreurs : {summary['groups_with_errors']}")
    if counters:
        print(f"  Liens symboliques ignorés : {counters.get('symlinks', 0)}")
        print(f"  Fichiers inaccessibles ignorés : {counters.get('inaccessible', 0)}")
        print(f"  Fichiers hashés : {counters.get('hashed_files', 0)}")
    print("\nTerminé.")

if __name__ == "__main__":
    main()
