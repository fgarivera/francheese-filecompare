"""
FolderCompare - a safe, read-only folder comparison tool.

PURPOSE
    Compare two folders (e.g. a laptop original vs. an SSD copy) to verify a
    file transfer completed correctly, including detecting silently corrupted
    files (a photo that is the "right" size but has flipped bytes).

SAFETY GUARANTEE (the most important thing about this program)
    This tool is STRICTLY READ-ONLY with respect to the folders you compare.
      * It only ever OPENS files for reading ('rb'). Computing a hash reads
        bytes exactly the way viewing a photo does - it never changes a file.
      * There is NO code here that writes, moves, renames, deletes, or in any
        way modifies files inside the folders being compared.
      * The ONLY file this program ever writes is the optional report, and it
        is written to a location YOU choose (e.g. your Desktop), never inside
        the compared folders.
      * There is no "fix", "sync", or "copy" button. The app looks and reports.
        If it finds a problem, YOU decide what to do, manually.

USAGE
    python folder_compare.py
"""

import csv
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Pillow is used (read-only) to decode images for the integrity check.
# Guarded so the comparison features still work even if Pillow is missing.
try:
    from PIL import Image, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS = None  # don't false-flag legitimately huge photos
    _PIL_OK = True
except Exception:
    _PIL_OK = False


def resource_path(name):
    """Path to a bundled asset, whether running as a script or a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

# ---------------------------------------------------------------------------
# Comparison status constants
# ---------------------------------------------------------------------------
STATUS_IDENTICAL = "Identical"                 # in both, hash matched
STATUS_DIFF_CONTENT = "DIFFERENT - content"    # in both, same size, hash differs (corruption!)
STATUS_DIFF_SIZE = "DIFFERENT - size"          # in both, sizes differ
STATUS_MATCH_QUICK = "Match (size only)"       # in both, same size, not yet hashed
STATUS_MISSING_RIGHT = "Missing on RIGHT"      # only in left folder
STATUS_MISSING_LEFT = "Missing on LEFT"        # only in right folder
STATUS_ERROR = "ERROR"                         # could not read a file
STATUS_PRESENT = "Checked"                     # single-folder health check: file was examined

# Treeview row colour tags
TAG_OK = "ok"
TAG_BAD = "bad"
TAG_WARN = "warn"
TAG_NEUTRAL = "neutral"

HASH_CHUNK = 1024 * 1024  # 1 MB read chunks for hashing

# ---------------------------------------------------------------------------
# Integrity check - actually OPEN/DECODE a file to see if it is broken
# (catches a photo/video that is damaged on its own, even if the copy matched).
# ---------------------------------------------------------------------------
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp", ".webm", ".flv"}
# Formats Pillow can't read without extra plugins - we can't judge these, so skip
# rather than false-flag. (HEIC/HEIF from iPhones fall here.)
UNSUPPORTED_BY_PIL = {".heic", ".heif"}

INTEGRITY_OK = "OK"
INTEGRITY_CORRUPT = "CORRUPT"
INTEGRITY_SKIP = "-"                 # not a photo/video, or format we can't assess
INTEGRITY_NO_FFMPEG = "needs ffmpeg" # a video, but ffmpeg isn't available to check it

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keep console from flashing


def ffmpeg_path():
    """Locate ffmpeg: first on PATH, then in the WinGet install location
    (so a fresh `winget install` is found even before PATH refreshes)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import glob
    pattern = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "Microsoft", "WinGet",
        "Packages", "Gyan.FFmpeg*", "**", "ffmpeg.exe",
    )
    hits = glob.glob(pattern, recursive=True)
    return hits[0] if hits else None


def check_image_integrity(path):
    """Try to fully decode an image. Only flag CORRUPT when the file is clearly
    unreadable - never for formats Pillow simply doesn't support."""
    if not _PIL_OK:
        return INTEGRITY_SKIP
    ext = os.path.splitext(path)[1].lower()
    if ext in UNSUPPORTED_BY_PIL:
        return INTEGRITY_SKIP  # e.g. HEIC/HEIF - Pillow can't read it, so we can't judge
    try:
        with Image.open(path) as im:
            im.load()  # force a full decode of the pixel data
        return INTEGRITY_OK
    except Exception:
        # Pillow couldn't decode it. This is only a reliable "corrupt" signal for
        # the standard formats Pillow fully supports; anything else we skip rather
        # than risk a false alarm on a file your photo viewer opens fine.
        return INTEGRITY_CORRUPT


def check_video_integrity(path, ffmpeg):
    """Decode a video with ffmpeg. Only flag CORRUPT when ffmpeg actually FAILS
    (non-zero exit). ffmpeg prints harmless warnings to stderr for many perfectly
    good files, so stderr content alone is NOT a reliable corruption signal."""
    if not ffmpeg:
        return INTEGRITY_NO_FFMPEG
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-xerror", "-i", path, "-f", "null", "-"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        return INTEGRITY_CORRUPT if proc.returncode != 0 else INTEGRITY_OK
    except Exception:
        return INTEGRITY_SKIP  # couldn't run the check - don't false-flag the file


def check_integrity(path, ffmpeg):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return check_image_integrity(path)
    if ext in VIDEO_EXTS:
        return check_video_integrity(path, ffmpeg)
    return INTEGRITY_SKIP  # documents, archives, etc. - nothing to decode


# ---------------------------------------------------------------------------
# Core comparison logic (pure, no GUI) - runs on a background thread
# ---------------------------------------------------------------------------

def scan_folder(root, stop_event, progress):
    """Walk a folder tree and return {normalized_relpath: FileInfo}.

    Opens nothing - only reads directory listings and file metadata.
    """
    files = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if stop_event.is_set():
            return files
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            key = os.path.normcase(rel)  # Windows is case-insensitive
            try:
                st = os.stat(full)
                files[key] = {
                    "rel": rel,
                    "full": full,
                    "size": st.st_size,
                }
            except OSError as exc:
                files[key] = {
                    "rel": rel,
                    "full": full,
                    "size": None,
                    "error": str(exc),
                }
            progress(f"Scanning: {rel}")
    return files


def hash_file(path, stop_event):
    """Return SHA-256 hex digest of a file, reading it in chunks.

    READ-ONLY: opens the file in binary read mode only.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if stop_event.is_set():
                return None
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compare_folders(left_root, right_root, do_hash, do_integrity, stop_event, emit):
    """Compare two folders and emit result rows.

    `emit` is a callback the worker uses to push progress/result messages back
    to the GUI thread. This function never modifies any compared file.
    """
    emit(("status", "Scanning left folder..."))
    left = scan_folder(left_root, stop_event, lambda m: emit(("status", m)))
    if stop_event.is_set():
        emit(("done", "cancelled"))
        return

    emit(("status", "Scanning right folder..."))
    right = scan_folder(right_root, stop_event, lambda m: emit(("status", m)))
    if stop_event.is_set():
        emit(("done", "cancelled"))
        return

    all_keys = sorted(set(left) | set(right), key=lambda k: k.lower())

    # First pass: classify by presence + size, collect hash candidates.
    rows = []  # each: dict(rel, status, left_size, right_size, left_full, right_full)
    hash_candidates = []
    for key in all_keys:
        l = left.get(key)
        r = right.get(key)
        if l and not r:
            rows.append(_row(l["rel"], STATUS_MISSING_RIGHT, l.get("size"), None, l, None))
        elif r and not l:
            rows.append(_row(r["rel"], STATUS_MISSING_LEFT, None, r.get("size"), None, r))
        else:
            if l.get("size") is None or r.get("size") is None:
                rows.append(_row(l["rel"], STATUS_ERROR, l.get("size"), r.get("size"), l, r))
            elif l["size"] != r["size"]:
                rows.append(_row(l["rel"], STATUS_DIFF_SIZE, l["size"], r["size"], l, r))
            else:
                row = _row(l["rel"], STATUS_MATCH_QUICK, l["size"], r["size"], l, r)
                rows.append(row)
                if do_hash:
                    hash_candidates.append(row)

    # Emit quick-scan rows immediately so the user sees results right away.
    for row in rows:
        emit(("row", row))
    emit(("status", f"Quick scan done: {len(rows)} entries."))

    # Second pass: hash the same-size files to detect content corruption.
    if do_hash and hash_candidates:
        total = sum(row["left_size"] or 0 for row in hash_candidates)
        done_bytes = 0
        emit(("hash_start", total))
        for row in hash_candidates:
            if stop_event.is_set():
                emit(("done", "cancelled"))
                return
            emit(("status", f"Hashing: {row['rel']}"))
            try:
                lh = hash_file(row["left_full"], stop_event)
                rh = hash_file(row["right_full"], stop_event)
            except OSError as exc:
                row["status"] = STATUS_ERROR
                row["note"] = str(exc)
                emit(("update", row))
                done_bytes += row["left_size"] or 0
                emit(("hash_progress", done_bytes))
                continue
            if lh is None or rh is None:  # cancelled mid-hash
                emit(("done", "cancelled"))
                return
            row["status"] = STATUS_IDENTICAL if lh == rh else STATUS_DIFF_CONTENT
            emit(("update", row))
            done_bytes += row["left_size"] or 0
            emit(("hash_progress", done_bytes))

    # Third pass: integrity check - open/decode each photo & video to confirm
    # it isn't broken on its own (independent of whether the copy matched).
    if do_integrity:
        ffmpeg = ffmpeg_path()
        targets = [row for row in rows
                   if (row.get("right_full") or row.get("left_full")) and row["status"] != STATUS_ERROR]
        emit(("integrity_start", len(targets)))
        for i, row in enumerate(targets, 1):
            if stop_event.is_set():
                emit(("done", "cancelled"))
                return
            target = row.get("right_full") or row.get("left_full")  # check the kept copy
            emit(("status", f"Checking integrity: {row['rel']}"))
            row["integrity"] = check_integrity(target, ffmpeg)
            emit(("update", row))
            emit(("integrity_progress", i))

    emit(("done", "complete"))


def _row(rel, status, left_size, right_size, l, r):
    return {
        "rel": rel,
        "status": status,
        "left_size": left_size,
        "right_size": right_size,
        "left_full": l["full"] if l else None,
        "right_full": r["full"] if r else None,
        "note": "",
        "integrity": "",  # filled by the integrity pass: OK / CORRUPT / etc.
    }


def health_check_folder(root_path, stop_event, emit):
    """Single-folder health check: open/decode every photo & video in ONE folder
    to find files that are broken on their own. Strictly READ-ONLY - it only
    reads/decodes files, exactly like viewing them; it never modifies anything.
    """
    emit(("status", "Scanning folder..."))
    files = scan_folder(root_path, stop_event, lambda m: emit(("status", m)))
    if stop_event.is_set():
        emit(("done", "cancelled"))
        return

    rows = []
    for key in sorted(files, key=lambda k: k.lower()):
        f = files[key]
        status = STATUS_ERROR if f.get("size") is None else STATUS_PRESENT
        row = _row(f["rel"], status, f.get("size"), None, f, None)
        rows.append(row)
        emit(("row", row))
    emit(("status", f"Found {len(rows)} files. Checking integrity..."))

    ffmpeg = ffmpeg_path()
    targets = [r for r in rows if r["status"] != STATUS_ERROR]
    emit(("integrity_start", len(targets)))
    for i, row in enumerate(targets, 1):
        if stop_event.is_set():
            emit(("done", "cancelled"))
            return
        emit(("status", f"Checking: {row['rel']}"))
        row["integrity"] = check_integrity(row["left_full"], ffmpeg)
        emit(("update", row))
        emit(("integrity_progress", i))

    emit(("done", "complete"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_duration(secs):
    """Human-friendly duration: '45s', '3m 20s', '1h 05m'."""
    secs = int(max(0, secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def human_size(n):
    if n is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.0f} {u}" if u == "B" else f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class FolderCompareApp:
    def __init__(self, root):
        self.root = root
        root.title("Francheese FileCompare - safe read-only folder verification")
        root.geometry("1000x680")
        try:
            root.iconbitmap(resource_path("cheese.ico"))
        except Exception:
            pass  # icon is cosmetic; never block startup over it

        self.left_var = tk.StringVar()
        self.right_var = tk.StringVar()
        self.hash_var = tk.BooleanVar(value=True)
        self.integrity_var = tk.BooleanVar(value=False)

        self.queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.rows_by_rel = {}  # rel -> treeview item id
        self.results = []       # list of row dicts (for export)
        self.mode = "compare"  # "compare" (two folders) or "health" (one folder)
        self.running = False
        self._run_start = 0.0
        self._phase_total = None   # total for current phase (files or bytes)
        self._phase_done = 0
        self._phase_unit = "files"
        self._phase_start = 0.0

        self._build_ui()
        self.root.after(100, self._poll_queue)

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Left folder (original):").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.left_var, width=90).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_left).grid(row=0, column=2)

        ttk.Label(top, text="Right folder (copy):").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.right_var, width=90).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_right).grid(row=1, column=2)

        top.columnconfigure(1, weight=1)

        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(
            opts,
            text="Verify file contents with SHA-256 hash (reads every byte - slower, but catches transfer corruption)",
            variable=self.hash_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            opts,
            text="Integrity check: open each photo/video to detect files that are broken on their own (slowest; videos need ffmpeg)",
            variable=self.integrity_var,
        ).pack(anchor="w")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.compare_btn = ttk.Button(btns, text="Compare two folders", command=self._start_compare)
        self.compare_btn.pack(side="left")
        self.health_btn = ttk.Button(btns, text="Health Check (Left folder only)", command=self._start_health)
        self.health_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.export_btn = ttk.Button(btns, text="Export report (CSV)...", command=self._export, state="disabled")
        self.export_btn.pack(side="left", padx=4)

        # Status + progress area - placed HIGH, right under the buttons, so it
        # is visible and updates live while a run is in progress.
        statusbar = ttk.Frame(self.root)
        statusbar.pack(fill="x", side="top", **pad)
        self.progress = ttk.Progressbar(statusbar, mode="determinate")
        self.progress.pack(fill="x", side="top", pady=(0, 2))
        # ETA + files-done + live issue count, directly under the green bar
        self.eta_var = tk.StringVar(value="")
        tk.Label(statusbar, textvariable=self.eta_var, anchor="w",
                 font=("Segoe UI", 9, "bold"), fg="#444444").pack(fill="x", side="top")
        self.status_var = tk.StringVar(value="Pick folder(s), then Compare or Health Check. This tool only reads your files - it never changes them.")
        ttk.Label(statusbar, textvariable=self.status_var, anchor="w").pack(fill="x", side="top")

        # Verdict line + file-count line + summary table
        self.verdict_label = tk.Label(statusbar, text="", anchor="w", font=("Segoe UI", 11, "bold"))
        self.verdict_label.pack(fill="x", side="top", pady=(4, 0))
        self.count_label = tk.Label(statusbar, text="", anchor="w", font=("Segoe UI", 10))
        self.count_label.pack(fill="x", side="top", pady=(0, 2))
        self.summary_frame = ttk.Frame(statusbar)
        self.summary_frame.pack(fill="x", side="top", anchor="w")
        self.summary_frame.columnconfigure(0, weight=1)
        self.summary_frame.columnconfigure(1, weight=1)
        self.summary_frame.columnconfigure(2, weight=1)

        # Results table - fills the remaining space below the status area
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)
        cols = ("status", "integrity", "left_size", "right_size", "rel")
        self.tree = ttk.Treeview(body, columns=cols, show="headings")
        self.tree.heading("status", text="Status")
        self.tree.heading("integrity", text="Integrity")
        self.tree.heading("left_size", text="Left size")
        self.tree.heading("right_size", text="Right size")
        self.tree.heading("rel", text="File (relative path)")
        self.tree.column("status", width=150, anchor="w")
        self.tree.column("integrity", width=100, anchor="w")
        self.tree.column("left_size", width=85, anchor="e")
        self.tree.column("right_size", width=85, anchor="e")
        self.tree.column("rel", width=480, anchor="w")

        self.tree.tag_configure(TAG_OK, foreground="#1a7f37")       # green
        self.tree.tag_configure(TAG_BAD, foreground="#c1121f")      # red
        self.tree.tag_configure(TAG_WARN, foreground="#b54708")     # orange
        self.tree.tag_configure(TAG_NEUTRAL, foreground="#555555")  # grey

        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

    # -- Folder pickers -----------------------------------------------------
    def _pick_left(self):
        d = filedialog.askdirectory(title="Select the LEFT (original) folder")
        if d:
            self.left_var.set(d)

    def _pick_right(self):
        d = filedialog.askdirectory(title="Select the RIGHT (copy) folder")
        if d:
            self.right_var.set(d)

    # -- Run lifecycle ------------------------------------------------------
    def _begin_run(self):
        """Shared setup: clear results and switch buttons into running state."""
        self.tree.delete(*self.tree.get_children())
        self.rows_by_rel.clear()
        self.results.clear()
        self.verdict_label.config(text="")
        self.count_label.config(text="")
        self.eta_var.set("Starting...")
        for w in self.summary_frame.winfo_children():
            w.destroy()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.stop_event.clear()
        self.running = True
        self._run_start = time.time()
        self._phase_total = None
        self._phase_done = 0
        self.compare_btn.configure(state="disabled")
        self.health_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

    def _start_compare(self):
        left = self.left_var.get().strip()
        right = self.right_var.get().strip()
        if not left or not right:
            messagebox.showwarning("Missing folder", "Please choose both folders.")
            return
        if not os.path.isdir(left) or not os.path.isdir(right):
            messagebox.showerror("Invalid folder", "One or both paths are not valid folders.")
            return
        if os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right)):
            messagebox.showwarning("Same folder", "Left and right are the same folder.")
            return

        self.mode = "compare"
        self._begin_run()
        do_hash = self.hash_var.get()
        do_integrity = self.integrity_var.get()
        self.worker = threading.Thread(
            target=compare_folders,
            args=(left, right, do_hash, do_integrity, self.stop_event, self.queue.put),
            daemon=True,
        )
        self.worker.start()

    def _start_health(self):
        folder = self.left_var.get().strip()
        if not folder:
            messagebox.showwarning("Missing folder", "Put the folder to check in the Left folder box.")
            return
        if not os.path.isdir(folder):
            messagebox.showerror("Invalid folder", "The Left folder path is not a valid folder.")
            return

        proceed = messagebox.askokcancel(
            "Health Check - what it does",
            "Health Check will OPEN and read every photo and video in:\n\n"
            f"{folder}\n\n"
            "It decodes each file to confirm it is not corrupted - exactly like "
            "opening it to view. It is strictly READ-ONLY:\n\n"
            "  • It does NOT modify, move, rename, or delete anything.\n"
            "  • It only reads your files - they cannot be harmed.\n\n"
            "Large folders and videos can take a while. You can press Cancel at "
            "any time; partial results stay on screen.\n\n"
            "Proceed?",
            icon="info",
        )
        if not proceed:
            return

        self.mode = "health"
        self._begin_run()
        self.worker = threading.Thread(
            target=health_check_folder,
            args=(folder, self.stop_event, self.queue.put),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self):
        self.stop_event.set()
        self.status_var.set("Cancelling...")

    # -- Queue polling (runs on GUI thread) ---------------------------------
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.running:
            self._refresh_live()
        self.root.after(100, self._poll_queue)

    _BAD_STATUSES = {STATUS_DIFF_CONTENT, STATUS_DIFF_SIZE,
                     STATUS_MISSING_RIGHT, STATUS_MISSING_LEFT, STATUS_ERROR}

    def _is_problem(self, row):
        if row.get("integrity") in (INTEGRITY_CORRUPT, INTEGRITY_NO_FFMPEG):
            return True
        return row["status"] in self._BAD_STATUSES

    def _refresh_live(self):
        """Update the live ETA / files-done / issues-so-far line under the bar."""
        parts = []
        if self._phase_total:
            done, total = self._phase_done, self._phase_total
            elapsed = time.time() - self._phase_start
            frac = (done / total) if total else 0
            if done > 0 and frac > 0:
                parts.append(f"Est. time left: {fmt_duration(elapsed * (1 - frac) / frac)}")
            else:
                parts.append("Est. time left: estimating...")
            if self._phase_unit == "files":
                parts.append(f"{int(done):,} / {int(total):,} files checked")
            else:
                parts.append(f"{human_size(done)} / {human_size(total)} read")
        else:
            parts.append("Scanning folders...")

        probs = sum(1 for r in self.results if self._is_problem(r))
        parts.append("no issues yet" if probs == 0 else f"⚠ {probs} issue(s) found so far")
        self.eta_var.set("      |      ".join(parts))

    def _handle(self, kind, payload):
        if kind == "status":
            self.status_var.set(payload)
        elif kind == "row":
            self._add_row(payload)
        elif kind == "update":
            self._update_row(payload)
        elif kind in ("hash_start", "integrity_start"):
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(payload, 1), value=0)
            self._phase_total = payload
            self._phase_done = 0
            self._phase_unit = "bytes" if kind == "hash_start" else "files"
            self._phase_start = time.time()
        elif kind in ("hash_progress", "integrity_progress"):
            self.progress.configure(value=payload)
            self._phase_done = payload
        elif kind == "done":
            self._finish(payload)

    def _tag_for(self, status):
        if status == STATUS_IDENTICAL:
            return TAG_OK
        if status in (STATUS_DIFF_CONTENT, STATUS_DIFF_SIZE, STATUS_ERROR):
            return TAG_BAD
        if status in (STATUS_MISSING_LEFT, STATUS_MISSING_RIGHT):
            return TAG_WARN
        return TAG_NEUTRAL

    def _row_tag(self, row):
        """Row colour considers BOTH the comparison status and the integrity result."""
        integ = row.get("integrity", "")
        if integ == INTEGRITY_CORRUPT:
            return TAG_BAD
        if integ == INTEGRITY_NO_FFMPEG:
            return TAG_WARN
        return self._tag_for(row["status"])

    @staticmethod
    def _integrity_text(row):
        v = row.get("integrity", "")
        return "" if v in ("", INTEGRITY_SKIP) else v

    def _row_values(self, row):
        status_text = row["status"] + (f"  ({row['note']})" if row.get("note") else "")
        return (status_text, self._integrity_text(row),
                human_size(row["left_size"]), human_size(row["right_size"]), row["rel"])

    def _add_row(self, row):
        self.results.append(row)
        item = self.tree.insert("", "end", values=self._row_values(row), tags=(self._row_tag(row),))
        self.rows_by_rel[row["rel"]] = item

    def _update_row(self, row):
        item = self.rows_by_rel.get(row["rel"])
        if item:
            self.tree.item(item, values=self._row_values(row), tags=(self._row_tag(row),))

    _TAG_COLOR = {TAG_OK: "#1a7f37", TAG_BAD: "#c1121f", TAG_WARN: "#b54708", TAG_NEUTRAL: "#555555"}

    def _render_summary(self, stats):
        """Render the result counts as a compact color-coded table of cells."""
        for w in self.summary_frame.winfo_children():
            w.destroy()
        cols = 3  # three metric cells per row
        for i, (label, count, tag) in enumerate(stats):
            r, c = divmod(i, cols)
            cell = tk.Frame(self.summary_frame, bd=1, relief="solid")
            cell.grid(row=r, column=c, sticky="we", padx=4, pady=3, ipadx=8, ipady=4)
            tk.Label(cell, text=label, anchor="w", font=("Segoe UI", 9)).pack(side="left")
            tk.Label(cell, text=str(count), fg=self._TAG_COLOR[tag],
                     font=("Segoe UI", 12, "bold")).pack(side="right", padx=(10, 2))

    def _finish(self, outcome):
        self.running = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=self.progress["maximum"] if outcome == "complete" else 0)
        self.compare_btn.configure(state="normal")
        self.health_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if self.results:
            self.export_btn.configure(state="normal")

        if self.mode == "health":
            self._finish_health(outcome)
        else:
            self._finish_compare(outcome)

        elapsed = fmt_duration(time.time() - self._run_start)
        self.eta_var.set(f"Completed in {elapsed}  ({len(self.results):,} files)" if outcome == "complete"
                         else f"Stopped after {elapsed}  ({len(self.results):,} files so far)")
        self.status_var.set("Done." if outcome == "complete" else "Stopped.")

    def _finish_compare(self, outcome):
        counts = {}
        for row in self.results:
            counts[row["status"]] = counts.get(row["status"], 0) + 1

        identical = counts.get(STATUS_IDENTICAL, 0)
        diff = counts.get(STATUS_DIFF_CONTENT, 0) + counts.get(STATUS_DIFF_SIZE, 0)
        quick = counts.get(STATUS_MATCH_QUICK, 0)
        miss_r = counts.get(STATUS_MISSING_RIGHT, 0)
        miss_l = counts.get(STATUS_MISSING_LEFT, 0)
        errs = counts.get(STATUS_ERROR, 0)
        corrupt = sum(1 for row in self.results if row.get("integrity") == INTEGRITY_CORRUPT)
        unchecked = sum(1 for row in self.results if row.get("integrity") == INTEGRITY_NO_FFMPEG)

        problems = diff + miss_r + miss_l + errs + corrupt
        total = len(self.results)

        # Explicit file-count check: are the two folders' totals equal?
        left_total = sum(1 for r in self.results if r.get("left_full"))
        right_total = sum(1 for r in self.results if r.get("right_full"))
        if left_total == right_total:
            self.count_label.config(
                text=f"File counts — Left (original): {left_total}   |   Right (copy): {right_total}   ✓ counts match",
                fg="#1a7f37")
        else:
            self.count_label.config(
                text=f"File counts — Left (original): {left_total}   |   Right (copy): {right_total}   "
                     f"⚠ MISMATCH: {abs(left_total - right_total)} file(s) difference",
                fg="#c1121f")

        self._render_summary([
            ("Identical", identical, TAG_OK),
            ("Size / content differ", diff, TAG_BAD if diff else TAG_NEUTRAL),
            ("Corrupt (won't open)", corrupt, TAG_BAD if corrupt else TAG_NEUTRAL),
            ("Missing on RIGHT", miss_r, TAG_WARN if miss_r else TAG_NEUTRAL),
            ("Missing on LEFT", miss_l, TAG_WARN if miss_l else TAG_NEUTRAL),
            ("Errors (unreadable)", errs, TAG_BAD if errs else TAG_NEUTRAL),
            ("Size-only (not hashed)", quick, TAG_NEUTRAL),
            ("Videos not checked", unchecked, TAG_WARN if unchecked else TAG_NEUTRAL),
        ])

        if outcome == "cancelled":
            self.verdict_label.config(
                text=f"Cancelled - partial results. {problems} problem(s) so far (of {total} checked).", fg="#b54708")
        elif problems == 0:
            self.verdict_label.config(text=f"✓  No problems found - all {total} files match.", fg="#1a7f37")
        else:
            self.verdict_label.config(
                text=f"⚠  {problems} problem(s) found of {total} files - review the red/orange rows.", fg="#c1121f")

    def _finish_health(self, outcome):
        ok = sum(1 for r in self.results if r.get("integrity") == INTEGRITY_OK)
        corrupt = sum(1 for r in self.results if r.get("integrity") == INTEGRITY_CORRUPT)
        unchecked = sum(1 for r in self.results if r.get("integrity") == INTEGRITY_NO_FFMPEG)
        skipped = sum(1 for r in self.results if r.get("integrity") in ("", INTEGRITY_SKIP)
                      and r["status"] != STATUS_ERROR)
        errs = sum(1 for r in self.results if r["status"] == STATUS_ERROR)

        problems = corrupt + errs
        total = len(self.results)

        self.count_label.config(text=f"Folder scanned: {total} files total", fg="#555555")

        self._render_summary([
            ("Healthy (opens fine)", ok, TAG_OK),
            ("Corrupt (won't open)", corrupt, TAG_BAD if corrupt else TAG_NEUTRAL),
            ("Unreadable (errors)", errs, TAG_BAD if errs else TAG_NEUTRAL),
            ("Videos not checked", unchecked, TAG_WARN if unchecked else TAG_NEUTRAL),
            ("Skipped (not photo/video)", skipped, TAG_NEUTRAL),
        ])

        if outcome == "cancelled":
            self.verdict_label.config(
                text=f"Cancelled - partial results. {problems} bad file(s) so far (of {total} scanned).", fg="#b54708")
        elif problems == 0:
            self.verdict_label.config(
                text=f"✓  Health check passed - all {total} files open fine.", fg="#1a7f37")
        else:
            self.verdict_label.config(
                text=f"⚠  {problems} bad file(s) found of {total} - review the red rows.", fg="#c1121f")

    # -- Export -------------------------------------------------------------
    def _export(self):
        if not self.results:
            return
        path = filedialog.asksaveasfilename(
            title="Save report",
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
            initialfile="folder_compare_report.csv",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Status", "Integrity", "Note", "Left size (bytes)", "Right size (bytes)", "Relative path", "Left path", "Right path"])
                for row in self.results:
                    w.writerow([
                        row["status"], row.get("integrity", ""), row.get("note", ""),
                        row["left_size"] if row["left_size"] is not None else "",
                        row["right_size"] if row["right_size"] is not None else "",
                        row["rel"], row["left_full"] or "", row["right_full"] or "",
                    ])
            messagebox.showinfo("Saved", f"Report saved to:\n{path}")
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))


def show_splash(root, duration_ms=2800):
    """Show the cheesy intro splash, then reveal the main window."""
    intro = resource_path("intro.png")
    if not os.path.exists(intro):
        root.deiconify()
        return
    try:
        photo = tk.PhotoImage(file=intro)  # Tk 8.6 reads PNG natively
    except tk.TclError:
        root.deiconify()
        return

    splash = tk.Toplevel(root)
    splash.overrideredirect(True)  # borderless
    w, h = photo.width(), photo.height()
    sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
    splash.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    label = tk.Label(splash, image=photo, borderwidth=0)
    label.image = photo  # keep a reference so it isn't garbage-collected
    label.pack()
    splash.bind("<Button-1>", lambda e: _close_splash(root, splash))  # click to skip
    splash.after(duration_ms, lambda: _close_splash(root, splash))


def _close_splash(root, splash):
    if splash.winfo_exists():
        splash.destroy()
    root.deiconify()
    root.lift()
    root.focus_force()


def main():
    root = tk.Tk()
    root.withdraw()  # hide main window until the splash finishes
    FolderCompareApp(root)
    show_splash(root)
    root.mainloop()


if __name__ == "__main__":
    main()
