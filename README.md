## Step 2: embeddings and near-duplicate grouping

pHash alone is a weak grouper: it catches exact copies and bursts well, but misses the same
scene with small changes. Step 2 adds visual embeddings (DINOv2, optionally CLIP) and a
benchmark harness that tells you which method works best **on your own photos**.

### Install the optional deps

```bash
pip install -e ".[dev,embeddings]"    # pulls torch + transformers
```

On an M1 with 8 GB, DINOv2 ViT-S/14 is the safe default: ~85 MB on disk, MPS-accelerated,
~1 GB peak RAM at batch size 8.

### Workflow

```bash
# 1. Compute embeddings once per photo (saved to SQLite, never recomputed for unchanged files)
curator embed --model dinov2_vits14

# 2. Re-group using embeddings instead of, or in addition to, pHash
curator group --method combined --model dinov2_vits14 --cosine-threshold 0.20
curator groups --min-size 2                    # lists groups and their files

# 3. Label the best photo in each group you care about
curator label --group 5 --photo path/to/best.jpg --label winner
curator label --group 5 --photo path/to/blurry.jpg --label rejected --reason "out of focus"

# 4. See which scorer picks your winners most often
curator benchmark --model dinov2_vits14
```

### Grouping methods

- `phash` (default, step-1 behaviour): Hamming distance on the 64-bit perceptual hash.
  Fast, no ML. Good for exact copies and near-identical bursts.
- `embeddings`: cosine similarity of DINOv2 (384-dim) or CLIP (512-dim) vectors.
  Better on the same scene with small changes. `--cosine-threshold` is the cutoff on
  `1 − similarity` (0.20 ≈ similarity 0.80; tune on your own photos).
- `combined`: group if pHash says "near-identical" **or** embeddings say "same scene".
  Best of both; same cost as `embeddings` only.

### Reading the benchmark

`curator benchmark` evaluates **scorers**, not the embedding model itself: each scorer
returns the photo it would pick, and we check whether it matches your `winner` label.
With 20–60 labeled groups you get a first signal:

- `sharpness` — variance of the Laplacian (from step 1)
- `phash_centroid` — photo closest to the others by pHash
- `emb_centroid` — photo closest to the group's embedding centroid
- `random` — baseline (should be ~1/N)

Step 3 adds real technical signals (exposure, eyes open); step 4 adds aesthetics.
The harness is deliberately scorer-agnostic so those plug in unchanged.

### Important: re-grouping invalidates `winner` labels

Group IDs are regenerated on every `curator group`. Choose the method you want first
(guided by `curator groups` and your own eyes), **then** label, **then** benchmark.
A future step adds stable group IDs and a review UI.