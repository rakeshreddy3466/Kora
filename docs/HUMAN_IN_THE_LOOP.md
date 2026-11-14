# Human in the loop

**Rule: KORA automates only what it can prove. Everything that rests on
inference goes to a named person, with the evidence attached, and the
dependent step waits.**

## What is automatic and what needs a person

| Situation | Automatic? | Who decides | What waits for the decision |
|---|---|---|---|
| identical bytes, render-identical pages | detection automatic | **approver** (1) signs the deletion plan | quarantine of the copy |
| superseded revision (doc-control revisions, high confidence) | detection automatic | approver (1) | compaction |
| revision order inferred from file names or dates (low confidence) | no: `lineage_order` review | analyst | compaction of that lineage |
| two files claim the same revision | no: `lineage_conflict` review | analyst | compaction of that lineage |
| excerpt / same recording at lower quality / recording → archive | detection automatic | **two** approvers | execution |
| near-duplicate (same words, no shared slide) | never deleted: `near_duplicate` review | analyst | nothing (decision recorded for owners) |
| machine (ASR) transcript | no: `transcript_review` | analyst | distilling the recording |
| personal data in derived text | no: `pii_review` | **DPO** | AI export of that content |
| erasure request (GDPR Art. 17) | no: `erasure_request` | **DPO ≠ requester** | erasure |
| employee departure | assessment automatic | approver signs the plan; manager receives unique items | deletion of the departing person's copies |
| execute / purge / restore | n/a | **operator** | source changes |

Every decision is attributed (`decided_by`), audited (`review.decided`) and has a defined effect:

* confirming an order raises the lineage's confidence and enables compaction
* choosing the authoritative revision detaches the other file
* approving a transcript makes the capsule valid
* approving an erasure runs it

Reviews are created idempotently from the pipeline. Decided tasks are never re-opened, and fresh evidence updates only open ones.

## Interfaces

```
GET  /v1/reviews?status=open              tasks the caller's role may decide
POST /v1/reviews/{id}/decide              {"decision": "<one of options>", "comment": "..."}
kora reviews list
kora reviews decide <task> <decision> --as carol --roles analyst
```

## Why this matters for the user

A site owner at the storage limit today has two choices: delete by hand (and
lose information) or do nothing. With KORA:

* the safe part is pre-decided with evidence, so approving a 4-item plan takes one decision instead of opening 4 files
* the uncertain part arrives as a short, specific question

In the sample corpus, 23 files produced 3 questions for a person, all
near-duplicates (the samples contain no hard identifiers, so there was no PII
question). Every other relation was settled with proof.
