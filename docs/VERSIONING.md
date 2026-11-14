# Version handling

Presentations live through many revisions, each with small changes, copied
across platforms under different names. KORA models that explicitly instead of
treating every file as unrelated or every similar file as a duplicate.

## 1. Identity levels

| Level | Hash | Used for |
|---|---|---|
| file occurrence | `files.id` = (source, path) | where a copy lives, who owns it, ACL, holds |
| bytes | `sha256` | exact copies, one extraction per content |
| rendered document | `normalized_hash` = sha256(ordered unit hashes) | container variants |
| slide (exact) | `unit_hash` = sha256(raster ‖ text) | slide store, recovery proofs |
| slide (body) | `body_hash` (footer band excluded) | matching across revisions/decks, diffs |
| slide (fuzzy) | 64-bit DCT pHash, MinHash of body text | modified slides, video frame → slide |
| document family | `lineages.key` | revisions |

## 2. Grouping versions into lineages

Evidence, strongest first:

1. **Document number** from repository metadata, document properties
   (subject/keywords/identifier) or page text, e.g. `1/1551-LZA 701 0001 Uen`.
2. **File-name stem plus content similarity.** The stem is normalised by removing
   download suffixes `(1)`, `Copy of`, `- Copy`, version tokens (`_v2`, `_Final`, `rev B`,
   `PA1`), dates and resolution markers. A candidate lineage is joined only if the content
   also overlaps (≥ 30 % shared slide bodies or MinHash ≥ 0.45). A name alone is never
   an identity.

Content similarity alone never creates a lineage. Decks that share slides are
`PARTIAL_OVERLAP`, not revisions of each other.

## 3. Ordering, with confidence

| Scheme | Example order | Confidence |
|---|---|---|
| Ericsson document revisions | `PA1 < PA2 < PA10 < A < PB1 < B < … < AA` (P = preliminary) | high |
| R-states | `P1A < R1A < R1B < R2A` | high |
| numeric | `v1.9 < v1.10 < v2` | high |
| file-name tokens | `draft < (none) < Final < Final_v2` | medium if consistent with modification times, else low |
| modification time | earliest copy's mtime (download copies are newer) | low |

Policy (`revisions.min_ordering_confidence = medium`) forbids compaction on
low-confidence lineages.

## 4. Special cases

* **Variant**: render-identical content with different bytes (re-saved PDF) attaches
  as `variant_of` the canonical version. It is not a new version and not a conflict.
* **Conflict**: two different contents claim the same revision. Both are flagged,
  `lineage.has_conflict` is set, and the lineage is blocked from compaction until
  resolved.
* **Excerpt**: content carrying the document number but no revision whose pages are a
  strict subset of a version is not a version. It gets a `CONTAINED_SUBSET` edge.
  Processing order does not matter: an excerpt that arrived before its parent is
  reclassified when the parent joins (audited).
* **Concurrency**: the lineage row is locked (`SELECT … FOR UPDATE`) while versions
  are inserted and re-ordered, so two revisions processed in parallel cannot
  produce two heads.

## 5. Slide-level diff

`REVISION_OF` edges and `versions.diff_from_parent` hold a diff computed as:

1. sequence alignment on `body_hash`: `unchanged`, or `unchanged_body` when only the
   footer or page number changed
2. identical bodies out of order: `moved`
3. remaining slides paired greedily by text similarity (token Jaccard) with pHash as a
   tie-breaker: `modified`, with a line diff
4. leftovers: `removed` / `added`

Sample, `1/1551-LZA 701 0001`:

```
PA1                                  
PA2   6 unchanged, 2 modified (title, topology), 1 added (Risks)
A     7 unchanged, 2 modified (title, "Lattency" -> "Latency")       released
A     variant (re-saved in OneDrive)
B     6 unchanged (5 footer-only), 1 moved, 1 modified, 1 added, 1 removed   HEAD
```

## 6. Slide store and render-equivalent recovery

Every unique `unit_hash` is stored once as a single-page PDF in the hot tier.
Revisions, copies and decks sharing slides share those pages. Deleting a
superseded revision keeps only the pages it introduced.

Recoverability is **proven, not assumed**. Before a revision or subset can be
deleted, KORA rebuilds it from the slide store, re-renders every page in the
sandbox, and requires each page's unit hash to equal the one recorded at
extraction. Corrupt one page blob and the item becomes non-deletable (tested).
`kora reconstruct <sha> --out file.pdf` performs the same rebuild on demand.

Retention rule (policy `revisions`): keep the head, every released revision, and
the most recent `keep_last_n_unreleased` work-in-progress preliminaries. For
name-based lineages, keep the head plus the previous version. The sample result
is that PA1, PA2 and `Team_Update.pdf` are compacted; A, B and the latest Team
Update versions stay.

## 7. Version-aware search

Identical slides across copies and revisions collapse into one hit with
`also_in` references. A hit from an old revision says whether the slide is
"unchanged in B (page 4)" or "changed or removed in B". Recording frames link to
the slide of the presented revision, chosen by voting over all keyframes with
OCR as the tie-breaker, so "as you see here" resolves to `1/1551-LZA 701 0001
rev A, slide 3`.
