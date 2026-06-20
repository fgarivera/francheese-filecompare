# Francheese FileCompare

A small, **safe, read-only** desktop tool to verify that a folder was copied
correctly — e.g. comparing the original on your laptop/HDD against the copy on
your SSD. It detects missing files, size mismatches, and **silently corrupted
files** (a photo that is the right size but has flipped bytes).

## Safety (top priority)

This tool **never modifies, moves, or deletes** any file in the folders you
compare. It only opens files for *reading* — computing a hash reads bytes the
same way viewing a photo does. The only file it ever writes is the optional
CSV report, saved wherever you choose. There is no fix/sync/copy button.

## How to run

- **Double-click** `Francheese FileCompare.exe` — no Python needed, fully
  portable (copy it anywhere). This is the easiest way.

For developers / tweaking the code instead:
- Run in a terminal: `python folder_compare.py`, **or** double-click
  `Run FolderCompare.bat` (these require Python 3).

## How to use

1. **Left folder** = the original (laptop / HDD).
2. **Right folder** = the copy (SSD).
3. Leave **"Verify file contents with SHA-256 hash"** checked — this is what
   catches corrupted photos. (Uncheck it for a quick name+size-only scan.)
4. Click **Compare**.
5. Read the colored results and the summary line at the bottom:
   - 🟢 **Identical** — file matches, byte for byte.
   - 🔴 **DIFFERENT - content** — same size but contents differ → **corrupted**.
   - 🔴 **DIFFERENT - size** — sizes don't match.
   - 🟠 **Missing on RIGHT / LEFT** — file exists on only one side (skipped).
   - 🔴 **ERROR** — a file could not be read.
6. Optional: **Export report (CSV)** to save a list of exactly which files to
   re-copy.

A full hash check reads every byte on **both** drives, so for ~46 GB expect a
few minutes. You can **Cancel** at any time; partial results stay on screen.

## Notes

- File modified-dates are intentionally ignored — they often differ harmlessly
  between drives. Size (quick) and hash (deep) are the reliable signals.
- Comparison is case-insensitive (standard Windows behavior).
