# OpenViking Upstream Merge Policy

Use this checklist whenever Hermes upstream changes
`plugins/memory/openviking` or OpenViking releases update the official concepts
or APIs.

## Authority Order

1. OpenViking concepts and API contracts.
2. Hermes runtime integration and safety patterns.
3. Local fact-memory and multi-agent workflow requirements.

Hermes upstream code is a proposal to evaluate against this order. Do not merge
new OpenViking tools or lifecycle behavior solely because Hermes added them.

## Review Buckets

Classify every upstream OpenViking change into one bucket:

| Bucket | Merge decision |
| --- | --- |
| Concept-aligned API/lifecycle improvement | Absorb into the canonical local implementation. |
| Hermes runtime/safety improvement | Absorb if it does not distort OpenViking concepts. |
| UX simplification or minimal compatibility surface | Reference it, but keep local canonical tools if they are concept-aligned. |
| Duplicate capability | Pick one public owner; demote the other to alias/helper or reject it. |
| Concept conflict | Reject locally and document why. |

## Duplicate Capability Rule

The provider must not expose two first-class public tools for the same
capability. When a duplicate appears, record the decision in `DESIGN.md` or the
merge notes:

- canonical public tool
- compatibility alias, if any
- behavior kept from each side
- tests proving the boundary

Current canonical decisions:

- `viking_search` owns semantic retrieval.
- `viking_glob` owns filename/path glob matching.
- `viking_find` is a hidden compatibility alias for path matching, not a
  first-class public tool.
- `viking_grep` owns regex content search.
- `viking_read` owns L0/L1/L2 content reads.
- `viking_browse` owns read-only AGFS navigation.
- `viking_write` owns exact AGFS writes.
- `viking_remember` owns explicit OpenViking memory/extraction hints.

## Required Checks

Before switching the real Hermes checkout after an upstream sync:

- Compare upstream and local OpenViking tool schemas.
- Check whether any new Hermes tool overlaps an existing canonical tool.
- Verify session lifecycle still uses message parts, `used`, and `commit`.
- Verify tenant identity still relies on OpenViking request identity, not local
  URI prefix inventions.
- Verify fact-memory boundary text still exists in README/schema guidance.
- Run targeted OpenViking provider tests.

## Rejection Is Allowed

If Hermes upstream implements an OpenViking feature in a way that conflicts with
OpenViking concepts or the local fact-memory boundary, reject that shape locally
and treat it as an external design suggestion. The goal is not to chase Hermes'
OpenViking implementation line-by-line; the goal is a coherent OpenViking
adapter for Hermes.
