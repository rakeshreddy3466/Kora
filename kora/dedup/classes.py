"""The duplicate taxonomy and what KORA is allowed to do about each class.

| class               | detected by                                   | default action            | recovery after deletion         |
|---------------------|-----------------------------------------------|---------------------------|---------------------------------|
| EXACT_COPY          | identical SHA-256                             | quarantine + stub         | byte-exact (survivor)           |
| CONTAINER_VARIANT   | identical ordered page/slide unit hashes      | quarantine + stub         | render-equivalent (survivor)    |
| SUPERSEDED_REVISION | lineage order + retention rule                | compact to slide store    | render-equivalent (slide store) |
| CONTAINED_SUBSET    | every slide present in a larger document      | compact to slide store    | render-equivalent (slide store) |
| MEDIA_VARIANT       | duration + temporal pHash signature           | quarantine + stub         | media-equivalent (survivor)     |
| RECORDING_DISTILLED | validated knowledge capsule                   | move original to archive  | byte-exact from archive tier    |
| COMPRESSED          | rendition within measured quality budget      | replace original in place | original in quarantine, then rendition |
| PARTIAL_OVERLAP     | shared slide bodies >= threshold              | slide-level dedup only    | - (never deleted)               |
| NEAR_DUPLICATE      | MinHash Jaccard >= threshold, no shared slide | human review              | - (never deleted)               |
| KNOWLEDGE_OVERLAP   | embedding cosine >= threshold                 | link for search           | - (never deleted)               |
"""
from __future__ import annotations

EXACT_COPY = "EXACT_COPY"
CONTAINER_VARIANT = "CONTAINER_VARIANT"
SUPERSEDED_REVISION = "SUPERSEDED_REVISION"
CONTAINED_SUBSET = "CONTAINED_SUBSET"
MEDIA_VARIANT = "MEDIA_VARIANT"
RECORDING_DISTILLED = "RECORDING_DISTILLED"
PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
NEAR_DUPLICATE = "NEAR_DUPLICATE"
KNOWLEDGE_OVERLAP = "KNOWLEDGE_OVERLAP"
COMPRESSED = "COMPRESSED"              # original replaced by a quality-budgeted rendition
MANUAL_DELETE = "MANUAL_DELETE"        # a person chose it; no copy or newer version explains it

ALL = [EXACT_COPY, CONTAINER_VARIANT, SUPERSEDED_REVISION, CONTAINED_SUBSET, MEDIA_VARIANT,
       RECORDING_DISTILLED, COMPRESSED, MANUAL_DELETE, PARTIAL_OVERLAP, NEAR_DUPLICATE, KNOWLEDGE_OVERLAP]

# relation kinds stored in the graph (subset of the classes + structural edges)
REL_CONTAINER = CONTAINER_VARIANT
REL_SUBSET = CONTAINED_SUBSET          # a is contained in b
REL_PARTIAL = PARTIAL_OVERLAP
REL_NEAR = NEAR_DUPLICATE
REL_MEDIA = MEDIA_VARIANT
REL_KNOWLEDGE = KNOWLEDGE_OVERLAP
REL_REVISION = "REVISION_OF"           # a is the previous revision of b
REL_PRESENTED = "PRESENTED_IN"         # document a is shown in recording b

SYMMETRIC = {REL_CONTAINER, REL_PARTIAL, REL_NEAR, REL_MEDIA, REL_KNOWLEDGE}
STRONG = {REL_CONTAINER, REL_SUBSET, REL_PARTIAL, REL_REVISION}

RECOVERY = {
    "byte_exact": "identical bytes remain at the survivor location",
    "byte_exact_archive": "identical bytes kept in the archive tier (hash-verified)",
    "render_equivalent": "every page re-renders to the same unit hash (proven by rebuild + re-render)",
    "media_equivalent": "same recording at equal or higher quality remains at the survivor",
    "knowledge_only": "transcript, keyframes, audio and slide links only; original media not recoverable",
    "original_in_quarantine": "byte-exact original kept in quarantine for the grace period; afterwards the "
                              "rendition, which passed the measured quality budget",
    "knowledge_capsule": "byte-exact original kept in quarantine for the grace period; afterwards its text, "
                         "slides, frames and transcript stay searchable and AI-ready in the signed capsule",
}
