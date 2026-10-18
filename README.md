# photo-curator

A local photo cataloguer and culling tool for people with **thousands of phone and camera photos**. It groups visually similar shots, learns which photos you reject, and organizes your library into a clean `_curator/` folder — without ever deleting anything.

**Everything runs on your Mac.** No cloud, no telemetry. The web UI and its interaction runtime are bundled locally rather than fetched from a CDN. Photos are only read; they are moved only after you explicitly confirm, and every move is reversible.

---

## Table of contents

1. [Requirements](#requirements)
2. [Install](#install)
3. [First run](#first-run)
4. [How the workflow works](#how-the-workflow-works)
5. [What gets moved where](#what-gets-moved-where)
6. [The web UI](#the-web-ui)
7. [Keyboard shortcuts](#keyboard-shortcuts)
8. [Learning from your tastes](#learning-from-your-tastes)
9. [CLI reference](#cli-reference)
10. [Data model](#data-model)
11. [How grouping works](#how-grouping-works)
12. [Benchmarking](#benchmarking)
13. [Customization](#customization)
14. [Undo and safety](#undo-and-safety)
15. [Troubleshooting](#troubleshooting)
16. [Known limits](#known-limits)
17. [Roadmap](#roadmap)

---

## Requirements

- **macOS** (tested on M1 with 8 GB RAM; also works on Linux with minor caveats)
- **Python 3.9 or newer**
- ~50 MB disk for dependencies, plus ~85 MB if you use embeddings (DINOv2)
- Optional but recommended: `torch` + `transformers` for visual embeddings

For a folder of **300–500 photos**, expect:
- ~2 minutes to index and group without embeddings
- ~4 minutes with DINOv2 on M1 (MPS accelerated)
- ~5 seconds to commit and organize

---

## Install

From zero, on a fresh machine:

```bash
# 1. Clone or copy the project folder
cd ~/Desktop
git clone <repo> photo-curator        # or copy the folder manually
cd photo-curator

# 2. Create an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Upgrade pip (needed for editable installs on older pips)
python -m pip install --upgrade pip

# 4. Install the package + dev + web extras
pip install -e ".[dev,web]"
Optional but highly recommended — install embeddings:

bash
pip install -e ".[dev,web,embeddings]"
This pulls in torch (~1.5 GB installed) and transformers (~600 MB). It unlocks:

Smart grouping (adaptive cosine similarity, much better than pHash alone)

Reject learning (learning which photos you tend to discard)

Benchmark emb_centroid (to measure if DINOv2 beats sharpness on your photos)

Without these, the tool still works — it falls back to pHash-based grouping.

Verify the install:

bash
pytest -q
# expected: ~60+ tests pass in a few seconds
First run
Start the local web server:

bash
curator web
You'll see:

text
Starting local review UI on http://127.0.0.1:5001
  db: curator.db  ·  fixtures: False
Open http://127.0.0.1:5001 in your browser.

The first visit redirects to /intake because the database is empty.

Click Choose folder → macOS native folder picker opens.

Select a folder of photos (start with 100–500 photos, not your entire library).

Click Analyze folder.

Watch the analysis checklist:

✓ Reading metadata and detecting duplicates

✓ Computing visual embeddings (if torch installed)

✓ Grouping similar shots

When done, you land on / (the review screen) with your groups ready.

That's it — from here, press Enter to confirm a group, press 1–9 to promote a different photo, press ⌫ to delete a group.

How the workflow works
The tool follows a strict decision queue: you never commit anything irreversible until the very end.

text
┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐
│   INTAKE    │ → │   ANALYSIS   │ → │   REVIEW (main UI)   │
│ pick folder │   │ scan + group │   │ confirm best of each │
└─────────────┘   └──────────────┘   └──────────────────────┘
                                              │
                                              ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐
│    DONE     │ ← │  CONFIRM &   │ ← │  REJECTED + SUGGESTED│
│  undo/open  │   │  ORGANIZE    │   │  recover mistakes    │
└─────────────┘   └──────────────┘   └──────────────────────┘
Phase 1 — Intake (/intake)
Pick a folder. If a review is already in progress, the page warns you ("You have N groups in progress") so you don't accidentally start over. If a previous batch was committed, a green banner tells you it's safe to proceed.

Phase 2 — Analysis (/analysis)
A live checklist with per-task ETA. Cancellable at any moment.

Reads EXIF dates, file names, modification times

Computes SHA-256 (exact duplicates), pHash (near-duplicates), sharpness (variance of Laplacian)

Detects screenshots by filename (e.g. Screenshot*.png)

Marks byte-identical copies as duplicate_of (still kept in groups for review)

Computes visual embeddings (if torch is installed) — DINOv2 ViT-S/14

Flags photos similar to past rejects — before grouping, so they don't clutter your review

Groups similar photos — time-adaptive cosine similarity (see How grouping works)

Phase 3 — Review (/)
On desktop, the review uses three panels:

Left sidebar: all groups, with progress ring and counter

Center: alternative photos, which you can select or open full size

Right: the current "chosen" photo, with metadata and confirmation actions

On smaller screens, groups become a horizontal strip, alternatives become a swipeable photo deck, and the chosen photo and decision buttons stay within reach. Swiping browses; choosing a photo changes the winner. Nothing is rejected or organized by a swipe.

For each group, you pick the best photo. It becomes the group's winner. The rest stay as "rejected within the group" (they will be moved to _curator/rejected/ at commit time).

Keyboard-first: with focus outside controls, Enter confirms and advances. 1–9 promotes a different photo. ⌫ deletes the entire group. Focused buttons keep their normal Enter behavior.

Phase 4 — Rejected (/rejected)
Photos the system flagged as screenshots or documents (filename heuristic). They never entered groups. You can:

K Keep → promote to a regular photo, joins groups next analysis

X Reject → confirmed reject, moves to _curator/rejected/ at commit

Enter Accept all → confirms all visible rejects in one shot

Phase 5 — Suggested (/suggested)
Only appears once you've built up reject history (2–3 sessions). These are photos the system thinks you'll reject, based on visual similarity to previously-rejected groups.

K Keep → "actually I like this one", photo rejoins groups next analysis

X Reject → confirmed reject, becomes a new reject example

Enter Accept all → all visible suggestions confirmed

Optional to review — if you trust the system, hit Enter and move on.

Phase 6 — Confirm & organize (/confirm)
The dry-run: shows a preview of every file that will be moved, the destination, the new name.

Kept: winner photos — move to _curator/winner/

Rejected: losers + confirmed rejects — move to _curator/rejected/

Live Photos: .HEIC + .MOV pairs — move together, same name stem

Total size, sample of the first 20 renames

Nothing is moved until you click "Confirm & organize". Every move is:

Preceded by SHA-256 verification

Followed by SHA-256 verification

All-or-nothing: if any move fails, the whole batch rolls back

Phase 7 — Done (/done)
Summary of the commit:

Total files moved, split by winner/rejected

Open folder → opens _curator/winner/ in Finder

Undo everything → moves all files back, clears state

Start over (or N) → returns to /intake for the next batch

The manifest (in SQLite) remains as an audit trail indefinitely. Undo stays available until you manually delete curator.db.

What gets moved where
Given a source folder /Users/you/Pictures/sample/:

text
sample/
├── IMG_0001.HEIC          ← untouched source files remain here
├── IMG_0002.HEIC          (only files chosen as winners/rejected move)
├── IMG_0003.JPG
├── Screenshot 2024.png    ← if utility, moves to _curator/rejected/
└── _curator/              ← created at commit
    ├── winner/
    │   ├── 20261015_143200_001.HEIC
    │   ├── 20261015_143200_001.MOV   ← Live Photo companion
    │   └── 20261016_090000_002.JPG
    └── rejected/
        ├── 20261015_143500_001.HEIC
        └── 20261016_091500_002.PNG
Filename format
YYYYMMDD_HHMMSS_NNN.ext

YYYYMMDD — the photo's taken_at date (EXIF capture date, not file mtime)

HHMMSS — time of day (00:00:00 if only the date is known, e.g. WhatsApp images)

NNN — a global per-day counter (001, 002, …), shared between winner/ and rejected/

.ext — the original extension, preserved exactly (.HEIC stays .HEIC, .JPG stays .JPG)

Why this format:

Sortable alphabetically = chronologically in Finder

No redundant folders (no YYYY/MM/) — the date is in the name

Counter per day guarantees no collisions when importing photos from multiple batches on the same day

Extension preserved avoids breaking HEIC containers and Live Photo pairs

Live Photos
If IMG_0001.HEIC has a sibling IMG_0001.MOV in the same source folder, they are treated as a pair:

Same NNN

Same destination folder (winner/ or rejected/)

Same name stem, different extensions

Linked in the manifest via companion_of

Undo restores both. If one is missing, the other is still moved and the manifest records the missing partner.

The web UI
Layout is a three-column view on / (the main review screen):

text
┌─────────────┬─────────────────────────┬───────────────┐
│  SIDEBAR    │     MAIN                │  DETAIL       │
│             │                         │               │
│  Groups     │  Rejected thumbnails    │  Chosen       │
│  (with      │  of this group          │  photo        │
│  progress   │                         │               │
│  ring)      │  Click to promote       │  Metadata     │
│             │  1–9 keyboard           │  Confirm ⌘    │
│  5 to       │                         │  Delete ⌫     │
│  review     │                         │               │
└─────────────┴─────────────────────────┴───────────────┘
All three columns are resizable — drag the vertical divider between panels. Widths are persisted in localStorage. Double-click a divider to reset to default.

Click any photo to open it full-screen. Use ← → to compare photos in the group and press Enter to select the one on screen. The app confirms the photo name, closes the preview, and advances to the next group. Z zooms in; Esc closes the preview.

Keyboard shortcuts
On / (review groups)
Key	Action
J / ↓	Next group
K / ↑	Previous group
Enter	Confirm current winner, advance to next pending
1–9	Promote the Nth alternate photo in the comparison grid
⌫ / Delete	Delete the entire group
⌘Z	Undo last action (30 actions deep)
?	Show shortcuts overlay
Esc	Deselect group
On /rejected and /suggested
Key	Action
K	Keep first photo
X	Reject first photo
Enter	Accept all visible
⌘Z	Undo last action
On /done
Key	Action
N	Start over (go to /intake)
In the lightbox
Key	Action
Z	Toggle zoom (2.5×)
← / →	Previous / next photo
Enter	Select the photo shown, close preview, and advance to the next group
Esc	Close
Learning from your tastes
After every commit, the tool learns what you rejected:

For each group you deleted, compute the centroid of its embeddings (average of member photos)

Store the centroid in reject_examples

On the next analysis, before grouping:

For every new photo, compute its cosine similarity to every stored reject centroid

If similarity ≥ 0.82, flag it as suggested_reject

Flagged photos are excluded from groups and appear in /suggested

The threshold (0.82) is conservative by design:

False positives (flagging a good photo) are annoying

False negatives (missing a bad photo) are harmless — it just goes into a group like before

You can tune it: DEFAULT_THRESHOLD in src/curator/learning.py.

After 2–3 sessions, expect 10–25% of new photos to appear in /suggested. Review them in 30 seconds with Enter, K for exceptions.

CLI reference
The web UI is the primary interface, but every step is also available via CLI.

curator scan <path>
Index a folder. Incremental by default (unchanged files skipped).

bash
curator scan ~/Pictures/sample
curator scan ~/Pictures/sample --force     # re-scan everything
Read-only. Skips .photoslibrary (Apple Photos must be exported first — see Troubleshooting).

curator group
Group indexed photos.

bash
curator group                                        # pHash only (step 1)
curator group --method embeddings --model dinov2_vits14
curator group --method combined --model dinov2_vits14
curator group --method smart --model dinov2_vits14   # recommended (time-adaptive)
--session-gap (minutes, default 30): gap that starts a new session

--max-hamming (default 10): max pHash distance for pHash methods

--cosine-threshold (default 0.20): cutoff on 1 - cosine for embeddings/combined

Note: running curator group wipes prior decisions (group_states). Use only if you're ready to re-review from scratch.

curator embed
Compute visual embeddings (requires pip install -e ".[embeddings]").

bash
curator embed --model dinov2_vits14
curator embed --model clip_vit_b32
curator embed --batch-size 8 --device mps    # M1
Incremental: photos already embedded with that model are skipped.

curator groups
List groups in the terminal (for debugging).

bash
curator groups --min-size 2
curator label
Manually label a photo within a group (for benchmark, if you don't use the web UI).

bash
curator label --group 5 --photo path/to/best.jpg --label winner
curator label --group 5 --photo path/to/bad.jpg --label rejected --reason "out of focus"
curator benchmark
Measure which scorer picks your winners most often.

bash
curator benchmark                              # sharpness, phash_centroid, random
curator benchmark --model dinov2_vits14        # adds emb_centroid
Output:

text
scorer               hits  groups  accuracy
emb_centroid            9      15     60.0%
sharpness               7      15     46.7%
phash_centroid          6      15     40.0%
random                  2      15     13.3%
With fewer than 20 labeled groups, treat the numbers as noise. At 40–60 groups, they become actionable.

curator stats
Summary of the database.

bash
curator stats --errors
curator web
Start the local web UI.

bash
curator web
curator web --port 5002
curator web --fixtures             # fake data (for UI development)
curator web --debug                # Flask debug mode
Binds to 127.0.0.1 only. Never exposes on the network.

Data model
Everything lives in curator.db (SQLite, in the current directory). The schema:

Table	Purpose
photos	One row per file: path, dates, size, SHA-256, pHash, sharpness, utility flag, duplicate_of, committed_at
groups	One row per (photo, group) — photo belongs to group_id
group_states	One row per group: status (pending / completed / deleted), current_winner_photo_id
embeddings	One row per (photo, model): L2-normalized float32 blob
labels	User labels for benchmark: group_id, photo_id, label, reason
benchmark_runs	Persisted benchmark results
settings	Key/value: last_folder, last_commit_id, grouping_fingerprint, review_completed
manifest	One row per moved file: commit_id, original_path, new_path, sha256, undone_at
reject_examples	Centroids of rejected groups (DINOv2 vectors)
photo_suggestions	Per-photo suggestions from reject learning, with dismissed_at
Inspect read-only:

bash
sqlite3 -readonly -header -column curator.db "SELECT * FROM photos LIMIT 10"
How grouping works
Four methods, selectable via --method:

phash (default, step 1)
Hamming distance on 64-bit perceptual hash

Fast, no ML

Good for: exact copies, near-identical bursts

Bad for: "one step back" (same scene, small movement)

embeddings (step 2)
Cosine similarity on DINOv2 (384-dim) or CLIP (512-dim) vectors

Static threshold (default: cosine ≥ 0.80)

Good for: same scene with small changes

Bad for: bursts (identical cosine threshold applies to photos 1 second or 5 minutes apart)

combined
Group if pHash matches OR embeddings match

Union of the two

smart (recommended)
Time-adaptive cosine threshold: burst photos (≤ 2s apart) use a permissive threshold (0.65), distant photos (5+ min apart) require very high similarity (0.85+)

Plus an exact-duplicate pHash override: if pHash distance ≤ 4, always merge

Good for: everything. Catches "one step back" without over-merging distant shots

Requires embeddings

Threshold curve (group.py:_time_adaptive_cosine_threshold):

Time gap	Cosine threshold
≤ 2 seconds	0.65
≤ 5 seconds	0.68
≤ 15 seconds	0.72
≤ 60 seconds	0.78
≤ 5 minutes	0.85
> 5 minutes	0.90
The web UI's /analysis uses smart automatically when embeddings are available, and falls back to phash otherwise.

Benchmarking
The curator benchmark command measures which scorer correctly picks your labeled winners. It's agnostic: it works with any function that returns a photo_id given a group's photos.

Built-in scorers:

Scorer	What it does
sharpness	Picks the photo with highest variance of Laplacian
phash_centroid	Picks the photo closest to the group's pHash centroid
emb_centroid	Picks the photo closest to the group's embedding centroid
random	Baseline (should be ~1/N for N-photo groups)
The benchmark is not about the embedding model itself — it's about whether a scorer matches your taste on your photos. If sharpness outperforms emb_centroid, the tool doesn't need ML for your photos. If the opposite, embeddings are earning their complexity.

Future steps (from the project PDF) will add:

exposure — from histogram

eyeBlink — MediaPipe blendshapes

aesthetic — Apple Vision, Aesthetic Predictor v2.5, NIMA

ranker — Bradley-Terry pairwise model with active learning

The current benchmark harness supports plugging in any new scorer with no framework changes.

Customization
Thresholds
GROUPING_VERSION in views.py — bump when grouping logic changes; forces re-analysis on next intake

_time_adaptive_cosine_threshold in group.py — the smart grouping curve

DEFAULT_THRESHOLD in learning.py — reject learning sensitivity (default 0.82)

THUMB_MAX in thumbs.py — thumbnail size (default 800px)

ANALYSIS_MAX_SIDE in scan.py — max side for sharpness/pHash analysis (default 1024px)

Column widths
The three-panel layout is resizable. Drag the hairline dividers. Widths persist in localStorage. Double-click to reset.

Database location
Pass --db:

bash
curator web --db ~/Pictures/curator.db
Or copy curator.db to a new location and specify it explicitly.

Undo and safety
Every commit is reversible. The manifest table records every move:

bash
sqlite3 -readonly -header -column curator.db \
  "SELECT role, original_path, new_path FROM manifest WHERE commit_id='abc123def'"
To undo, from the /done page click Undo everything. From CLI:

bash
curl -X POST http://127.0.0.1:5001/api/commit/undo
The undo:

Reads all manifest rows for the last commit_id where undone_at IS NULL

Moves each file back to original_path

Verifies SHA-256 against sha256_before

Clears committed_at on the photos table

Stamps undone_at on the manifest

Idempotent: calling undo twice is safe (second call returns "already undone").

Partial failures: if a file is missing or its hash doesn't match, it's reported in missing[] but the rest is still restored.

Permanent: undo works as long as the manifest exists. Delete curator.db and you lose the ability to undo.

Troubleshooting
"Photos Library.photoslibrary is refused"
The tool refuses to scan Apple Photos libraries directly — modifying them risks corruption. Export first:

bash
pip install osxphotos
osxphotos export ~/Pictures/sample --directory "{created.year}/{created.month}"
curator scan ~/Pictures/sample
"No embeddings for model 'dinov2_vits14'"
The analysis fell back to pHash grouping because embeddings are missing. Fix:

bash
pip install -e ".[embeddings]"
curator embed --model dinov2_vits14
Then re-run analysis from /intake.

"torch not installed — pHash only"
Same as above. The tool works without torch, but grouping will be less accurate and /suggested won't populate.

Analysis stalls or is very slow
On M1 with 8 GB RAM and 500+ photos, embedding can take 2–5 minutes. Check the per-task ETA on /analysis. If it stalls:

Reduce batch size: edit views.py:_real_analysis → batch_size=4 instead of 8

Or cancel and use phash only

Filenames look wrong after commit
The format is YYYYMMDD_HHMMSS_NNN.ext. If HHMMSS is 000000, the photo had no trustworthy time (common for WhatsApp exports). This is honest: only the date is known.

Live Photos have orphaned .MOV files
If you moved .HEIC files manually (outside the tool), their .MOV companions may remain in source. The tool only pairs them when the .MOV is a sibling in the same folder at scan time. For orphaned files, rename manually or rerun analysis.

Photos seem to duplicate across sessions
Check that _curator/ is being skipped on re-scan. It should be (see scan.py:iter_photos). If not, you may have an old version — verify:

bash
python -c "from curator.scan import iter_photos; print([p.name for p in iter_photos(__import__('pathlib').Path('.'))][:5])"
The three-column layout collapsed after resize
Double-click the divider between panels to reset widths. If that doesn't work, clear localStorage in DevTools → Application → Local Storage → curator-widths.

"Start over" doesn't show new photos
After commit, ensure:

You added new photos to the source folder (not _curator/)

You analyzed the same folder (or a different one) via /intake

The new photos are in the source root, not in _curator/ — _curator/ is skipped deliberately

Known limits
What this tool does NOT do yet (from the project PDF, prioritized):

No face-based analysis — sharpness is measured on the whole image, not on subjects/faces (step 3 of PDF)

No eye-blink detection — MediaPipe integration not yet implemented (step 3)

No aesthetic scoring — Apple Vision, Aesthetic Predictor, NIMA not yet integrated (step 4)

No personal ranker — the benchmark harness exists, but no trained Bradley-Terry model yet (step 5)

No active learning — no UI to prioritize uncertain groups (step 5)

No .MOV-first detection — Live Photos are detected as .HEIC + .MOV sibling, but standalone videos are ignored

No sidecar XMP — metadata is read from EXIF only, not from XMP sidecars

pHash fallback is weak — without embeddings, "one step back" photos may end up separate

What the tool does well:

Read-only until commit — nothing moves without confirmation

All-or-nothing commit — every move verified by SHA-256

Full undo — reversible via manifest, indefinitely

Live Photo pairing — .HEIC + .MOV stay together

Learning from your taste — accumulates reject examples over time

Deterministic grouping — same input → same output

Incremental — unchanged files are skipped

Fast — minutes for hundreds of photos, not hours

Roadmap
The current state covers steps 1, 2, and the workflow/commit portion of steps 3–6 from the project PDF. What remains:

### Personalized recommendations and clearer comparisons

Build this in safety-first stages. Existing scan features, review choices, embeddings, and reject examples are a starting point, but durable feedback, reliable undo, and evaluation are needed before learned recommendations can safely preselect photos.

1. **Make feedback and actions reliable.** Persist explicit keep/reject and confirmed-winner choices against stable photo/group identities. Preserve completed review decisions when analysis is rebuilt. Make suggestion actions and undo server-backed, and ensure every accepted reject appears in the dry-run organize plan.
2. **Measure useful photo signals.** Store real sharpness, brightness/exposure and clipping, image dimensions, and exact/near-duplicate cues. Represent unavailable measurements as unknown, not fabricated values. Keep screenshot/document clues as weak type signals; a type label alone must never mean a photo is unwanted.
3. **Learn two distinct preferences locally.** Use confirmed keeper-versus-alternative choices to train a regularized pairwise ranker that recommends the likely best frame in each group. Train a separate, regularized keep/reject classifier only from explicit keep/reject feedback. Do not treat every non-winning burst photo or an untouched prediction as a general reject label. Reuse frozen visual embeddings as optional similarity features, not as a model to fine-tune from sparse feedback.
4. **Explain comparisons on the main review page.** During selection, show concise, truthful signals that help compare the current photo with its alternatives, such as relative sharpness, measured exposure/clipping, dimensions, duplicate/similarity cues, and the learned keeper recommendation. Label recommendations as suggestions, show only evidence that is actually available, and keep the photos and manual choice central.
5. **Unify the proposed-discard review.** Put system-flagged utility photos and high-confidence learned reject candidates in one Rejected review queue, clearly identifying why each is there. Preselect only candidates that pass a conservative, later-batch precision check. Allow individual keep/override and batch acceptance; organizing always remains behind the explicit confirmation page.
6. **Validate before widening automation.** Add tests for feedback semantics, migrations, state preservation, commit preview, undo, missing features, and conservative cold-start behavior. Benchmark keeper ranking and reject precision on later batches, avoiding data leakage between photos from the same group. Leave candidates unselected when evidence or validation is insufficient.

Related ideas to evaluate after this foundation:

- Face-based sharpness, measured on the subject rather than the whole image.
- Eye-blink detection as a review cue, never an automatic reject.
- Optional local OCR/document detection; evaluate accuracy, runtime, privacy, and dependency cost before adopting it.
- Active learning that prioritizes uncertain groups only if it reduces review effort without adding required questions.
- Batch benchmarking UI and additional local aesthetic predictors only if they demonstrate value beyond the personal ranker and measured signals.
- Native folder picker everywhere (osascript integration is already used in `/intake`).

Keep cross-batch learning local to the user's own history; no cloud aggregation or telemetry.

A note on design principles
This tool was built with four rules in mind:

Never modify photos without explicit confirmation. Read-only until /confirm. All-or-nothing commit. Full undo.

Local-first, no telemetry. No network calls. No cloud. Ever.

Learning from the user's actual taste. Not a generic aesthetic model — your explicit keep/reject choices and confirmed winners.

Keyboard-first, mouse-optional. Every action has a shortcut. The mouse accelerates, it doesn't gate.

If you use this on 10,000 photos over a year, it should still feel like the same tool, because the design accommodates discovery without requiring rewrites. That's the goal.