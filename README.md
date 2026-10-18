# photo-curator

Local photo cataloguer. **Step 1: indexing and provisional grouping.**
It never modifies, moves or deletes any file: it only reads. No network access.

## Install (macOS)

```bash
cd photo-curator
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip   # needed if pip is old ("editable mode" error)
pip install -e ".[dev]"
pytest            # uses synthetic photos only
```

## Usage

```bash
# 1. Index a folder (incremental: unchanged files are skipped)
curator scan ~/Pictures/sample          # add --force to re-scan everything

# 2. Group by time session + visual similarity (provisional)
curator group                           # --session-gap 30 --max-hamming 10

# 3. Summary
curator stats --errors
```

The database is `curator.db` (SQLite) in the current folder. It holds metadata only.
Inspect it read-only with: `sqlite3 -readonly -header -column curator.db`

## Important: the Apple Photos library

If your photos live in the Photos app, **do not point the tool at a `.photoslibrary`**: it is
refused on purpose. Export a copy first, for example:

```bash
pip install osxphotos
osxphotos export ~/Pictures/sample --directory "{created.year}/{created.month}"
```

For a first test use a sample of 300-500 photos, not the whole library.

## What step 1 does

- Capture date, in order of trust: EXIF capture date → EXIF date → date (and time) in the file
  name (WhatsApp `IMG-20210909-WA0026`, Android `IMG_20210909_143012`, screenshots…) →
  file modification time. Each photo records where its date came from (`taken_source`) and whether
  its time of day is trustworthy (`time_known`).
- Photos with no trustworthy time of day (typical for WhatsApp images, which carry no EXIF) are
  compared with every photo of the same calendar day instead of being placed in a time session.
- SHA-256 of the file (exact copies), perceptual hash (near-duplicates), sharpness (variance of
  the Laplacian, whole image), orientation from EXIF, HEIC supported (`pillow-heif`).
- Time sessions: a pause longer than 30 minutes starts a new session; inside a session, photos
  with a similar perceptual hash are merged.

## Upgrading from version 0.1.0

Just run `curator scan <folder>` again: the database is migrated and old rows are re-scanned
automatically. Then run `curator group` and `curator stats`.

## Known limits (to be addressed in the next steps)

- pHash is a weak grouper: fine for near-identical shots, not for the same scene with small
  changes. Step 2 adds embeddings (DINOv2) and a benchmark that decides which method works best
  on your own photos.
- Sharpness is measured on the whole image; we want it on the subject/face (step 3).
- The thresholds (30 minutes, distance 10) are starting values to be tuned on your sample.
- For files named like `IMG-YYYYMMDD-WA####`, the time of day comes from the file's modification
  time when that falls on the same day: it is when the file arrived, not when the photo was taken.
