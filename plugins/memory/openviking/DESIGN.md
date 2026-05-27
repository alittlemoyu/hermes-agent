# OpenViking Provider Design Charter

This provider adapts OpenViking's official concepts to Hermes. It is not a
collection of unrelated local tool experiments, and it is not downstream of
Hermes' current OpenViking provider shape when that shape is only a minimal
compatibility implementation.

## Design Authority

When upstreams disagree, use this order:

1. OpenViking official concepts and API contracts.
2. Hermes runtime integration patterns and safety mechanisms.
3. Local fact-memory and multi-agent workflow requirements.

Hermes upstream OpenViking changes are treated as compatibility guidance. They
should be absorbed when they align with OpenViking concepts or improve Hermes
runtime fit, but they do not define the provider's architecture by themselves.

## Module Boundary

- `provider.py` is the Hermes adapter boundary: configuration,
  initialization, session identity, and shutdown.
- `client.py` is the OpenViking REST boundary: HTTP methods, identity headers,
  upload/delete behavior, and structured server errors.
- `lifecycle.py` owns automatic recall, turn capture, `used`, commit,
  pre-compression archive, and memory-write mirroring.
- `tools.py` owns tool dispatch and behavior; `schemas.py` owns schema text;
  `tool_policy.py` owns default exposure decisions.
- `__init__.py` is export compatibility only. It must not regain provider,
  lifecycle, client, schema, or tool handler implementation.

## Concept Mapping

- Retrieval: `viking_search` is the single semantic retrieval entry point.
  `strategy="find"` maps to OpenViking `find()` for simple low-latency recall;
  `strategy="search"` maps to OpenViking `search()` for session-aware intent
  analysis and rerank.
- Progressive loading: `viking_read` owns L0/L1/L2 content loading
  (`abstract`, `overview`, `full`). Callers should read summaries before full
  content unless exact text is required.
- AGFS navigation: `viking_browse`, `viking_glob`, and `viking_grep` expose
  filesystem-style navigation and lexical search. They are not semantic
  retrieval tools.
- Session lifecycle: Hermes turns are captured as OpenViking message parts;
  actual context use is recorded through `used`; compression and session-end
  paths use OpenViking `commit`.
- Storage authority: AGFS is OpenViking's content source of truth. Vector
  records are indexes and must not be treated as canonical content.
- Multi-tenant identity: account, user, and agent boundaries come from
  OpenViking request identity and auth context. Do not invent tenant-specific
  URI prefixes in this provider.

## Fact-Memory Boundary

Fact-memory is the local structured source of truth for Moyu's `project`,
`task`, `fact`, and `problem` entities. OpenViking strengthens that workflow by
providing recall, semantic candidate discovery, archive context, and searchable
mirrors. It must not replace fact-memory writes.

Rules:

- Use OpenViking recall to discover candidate entities, related logs, and
  keywords.
- Confirm structured entity changes through fact-memory duplicate checks,
  dry-run, and ingest.
- Mirror fact-memory entities into OpenViking only as a search/read layer.
- Do not let OpenViking content overwrite `.memory/entities/`.

## Tool Ownership

Each public capability should have one canonical tool:

| Capability | Canonical tool | Notes |
| --- | --- | --- |
| Semantic recall | `viking_search` | `find` and `search` are OpenViking retrieval strategies. |
| Content read | `viking_read` | L0/L1/L2 progressive loading. |
| Directory browse | `viking_browse` | Read-only AGFS navigation. |
| Path/glob match | `viking_glob` | Filename/path matching; `viking_find` is compatibility sugar hidden by default. |
| Regex content search | `viking_grep` | Lexical content search, not semantic retrieval. |
| Session archive lookup | `viking_archive` | Archive search/expand over committed session history. |
| Direct AGFS write | `viking_write` | Exact URI writes; supports `content_path`. |
| Memory extraction hint | `viking_remember` | Candidate memory/extraction path, not fact-memory ingest. |
| Resource/skill import | `viking_add_resource`, `viking_add_skill` | Follow OpenViking APIs and temp upload contracts. |
| Maintenance | `viking_fs`, `viking_system`, `viking_admin`, `viking_consistency`, `viking_reindex` | Maintenance layer; mutations require explicit safety rules. |

Do not add a new public tool if an existing canonical tool can own the
capability with a small schema extension.

Compatibility tools may remain callable for old transcripts or automations, but
they should not appear in `get_tool_schemas()` unless explicitly enabled by an
environment flag.

## Safety Rules

- Admin writes must request Hermes approval before the HTTP mutation.
- Maintenance tools should remain clearly labeled as maintenance/high-risk.
- Large free text should use `content_path` to avoid oversized model-emitted
  tool-call JSON.
- Auto-recall is candidate context only. `sync_turn(..., contexts=...)` or
  structured message capture is responsible for recording actual use.

## Upstream References

- OpenViking concepts: https://github.com/volcengine/OpenViking/tree/main/docs/zh/concepts
- Architecture: https://raw.githubusercontent.com/volcengine/OpenViking/main/docs/zh/concepts/01-architecture.md
- Retrieval: https://raw.githubusercontent.com/volcengine/OpenViking/main/docs/zh/concepts/07-retrieval.md
- Session: https://raw.githubusercontent.com/volcengine/OpenViking/main/docs/zh/concepts/08-session.md
- Multi-tenant: https://raw.githubusercontent.com/volcengine/OpenViking/main/docs/zh/concepts/11-multi-tenant.md
