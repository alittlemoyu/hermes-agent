# OpenViking Memory Provider

Context database by Volcengine (ByteDance) with filesystem-style knowledge hierarchy, tiered retrieval, and automatic memory extraction.

## Requirements

- `pip install openviking`
- OpenViking server running (`openviking-server`)
- Embedding + VLM model configured in `~/.openviking/ov.conf`

## Setup

```bash
hermes memory setup    # select "openviking"
```

Or manually:
```bash
hermes config set memory.provider openviking
echo "OPENVIKING_ENDPOINT=http://localhost:1933" >> ~/.hermes/.env
```

## Config

All config via environment variables in `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENVIKING_ENDPOINT` | `http://127.0.0.1:1933` | Server URL |
| `OPENVIKING_API_KEY` | (none) | API key (optional) |
| `OPENVIKING_ACCOUNT` | `default` | Tenant account header |
| `OPENVIKING_USER` | `default` | Tenant user header |
| `OPENVIKING_AGENT` | `hermes` | Agent namespace header |
| `OPENVIKING_AUTO_RECALL` | `true` | Enable OpenClaw-style automatic memory recall |
| `OPENVIKING_AUTO_CAPTURE` | `true` | Enable automatic turn capture and threshold commits |
| `OPENVIKING_RECALL_RESOURCES` | `false` | Include `viking://resources` in automatic recall |
| `OPENVIKING_RECALL_LIMIT` | `6` | Maximum auto-recalled memories injected per turn |
| `OPENVIKING_RECALL_SCORE_THRESHOLD` | `0.15` | Minimum score for auto-recall candidates |
| `OPENVIKING_RECALL_MAX_INJECTED_CHARS` | `4000` | Character budget for injected recall block |
| `OPENVIKING_COMMIT_TOKEN_THRESHOLD` | `20000` | Pending-token threshold for background commit |
| `OPENVIKING_COMMIT_KEEP_RECENT_COUNT` | `10` | Recent messages kept live after threshold commit |

For normal search, memory, session, resource, and skill calls, prefer a
tenant/user API key. Use a root/admin key only for `viking_admin` and other
tenant-management operations. Hermes sends account/user/agent headers when
configured, but tenant isolation remains OpenViking's auth/API-key concern; the
plugin does not invent tenant prefixes in `viking://` URIs.

## Tools

| Tool | Description |
|------|-------------|
| `viking_search` | Search with compatible `find` mode or session-aware `search` mode |
| `viking_read` | Read content at a viking:// URI (abstract/overview/full, with offset/limit for full reads) |
| `viking_browse` | Read-only filesystem navigation (list/tree/stat) |
| `viking_fs` | Explicit filesystem maintenance through OpenViking APIs (mkdir/mv/rm) |
| `viking_remember` | Store a fact for extraction on session commit; use `content_path` for larger notes |
| `viking_write` | Create or update content through `/api/v1/content/write`; use `content_path` for large text |
| `viking_link` | Link related OpenViking URIs |
| `viking_relations` | Inspect relations for a URI |
| `viking_find` | Find files by name pattern |
| `viking_grep` | Search file content with regex patterns |
| `viking_glob` | Match files with glob patterns |
| `viking_add_resource` | Ingest URLs, git sources, local files, or directories |
| `viking_archive` | Search or expand OpenViking session archives |
| `viking_add_skill` | Add a structured skill, MCP tool dict, raw `SKILL.md`, local `SKILL.md`, or skill directory |
| `viking_sync_skills` | Sync Hermes `SKILL.md` files into the configured OpenViking agent scope |
| `viking_system` | Health, readiness, status, wait, observer, and capped/filterable Prometheus metrics |
| `viking_admin` | Multi-tenant Admin API reads and approved writes |
| `viking_consistency` | Check filesystem/vector-index consistency |
| `viking_reindex` | Rebuild vectors or semantic products for a subtree |

Direct HTTP resource ingestion follows the OpenViking API contract: remote URLs
are sent as `path`, while local files and directories are uploaded first through
`/api/v1/resources/temp_upload` and then added by `temp_file_id`. Directory
uploads are zipped locally and symlink escapes are skipped.
When `to` or `parent` is provided, it must be under `viking://resources/`;
local files and directories are sent by `temp_file_id`, while remote and git
sources are sent by `path`.

For `viking_write` and `viking_remember`, keep inline `content` short. If the
text is large, write it to a local UTF-8 file first and pass `content_path`;
Hermes reads the file inside the tool so the model does not need to emit a huge
JSON tool-call argument. Temporary transfer files are caller-owned by default;
set `delete_content_path_after_write=true` on `viking_write` to delete the file
only after OpenViking accepts the write. For `viking_add_skill`, prefer `path`
over inline `data` when syncing full `SKILL.md` bodies or skill directories.

Skill ingestion follows the same direct-HTTP shape as OpenViking's Skills API:
inline `data` is posted to `/api/v1/skills` unchanged, while local `SKILL.md`
files or skill directories are uploaded through
`/api/v1/resources/temp_upload` first and then referenced by `temp_file_id`.

`viking_admin` read actions (`list_accounts`, `list_users`, `list_agents`) call
the Admin API directly. Write actions (`create_account`, `delete_account`,
`register_user`, `remove_user`, `set_role`, `regenerate_key`) always request
Hermes approval before sending the HTTP request; denial or timeout means no
OpenViking mutation request is sent.

## Fact-Memory Cooperation

OpenViking is a recall, archive, and searchable mirror layer. It is not the
source of truth for Moyu's structured fact-memory entities.

When a memory belongs to fact-memory's structured domain (`project`, `task`,
`fact`, or `problem`), write it through the fact-memory skill:
`query.py` first, then `ingest.py` with dry-run/report verification as needed.
Do not use `viking_remember` or `viking_write` as the only durable write path
for those entities.

OpenViking auto-recall, session archives, and automatic memory extraction are
candidate signals. If they mention project progress, task state, a production
problem, or a stable fact, feed that signal back through fact-memory duplicate
checks and ingest before treating it as official memory.

The synchronization direction is fact-memory -> OpenViking. Fact-memory entity
files under `.memory/entities/` may be mirrored into OpenViking AGFS for search
and `viking_read`, but OpenViking content must not overwrite those local entity
files.

## Session Integration

The provider follows the official OpenClaw plugin lifecycle shape. Each Hermes
session maps to an OpenViking session, turns are recorded via
`/api/v1/sessions/{session_id}/messages`, actual context usage is recorded via
`/api/v1/sessions/{session_id}/used`, and auto-recall injects a capped
`<openviking_context>` block for the next turn. Auto-recall searches user and
agent memories by default; resource recall is opt-in with
`OPENVIKING_RECALL_RESOURCES=true`.

After each captured turn, Hermes checks OpenViking `pending_tokens`. Once the
configured threshold is reached, it commits in the background with
`keep_recent_count=10`, letting OpenViking archive older history while keeping
recent turns live. Before Hermes context compression, the provider commits with
`keep_recent_count=0` and waits for OpenViking's archive/extraction task so the
compression boundary is preserved server-side. Shutdown/session-end commits
reuse the same commit path.

Hermes self-evolution review agents use OpenViking in artifact-only mode:
their internal review prompts and tool transcripts are not captured as normal
session messages, but explicit memory/skill artifacts can still read from and
write to the selected OpenViking tools. Mirrored review memory writes are
marked with `background_review` provenance.

This plugin focuses on the OpenViking context lifecycle: session context,
recall, capture, archive search, and commit policy. Durable Store/KV state is a
separate integration layer and is intentionally not exposed here.
