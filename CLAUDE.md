# CLAUDE.md — Course Scout

## Overview

Telegram art channel scanner. Fetches messages via Telethon with rich metadata (reactions/views/forwards, document filenames, link previews, pinned-message tracking), runs a per-image vision pre-compute pass (Haiku captions cached to disk), classifies via Claude Agent SDK with per-channel system prompts, and ranks via a two-stage LLM pipeline (categorize → preference). Produces weekly digest reports (markdown + PDF) with a Top 5 executive summary and native-app deep links. NAS cron: Sundays at 1am (`0 1 * * 0`).

## Architecture

```
Stage 0  CLI (scan)
   ↓
Stage 1  Telethon Fetch (per topic, runtime.topic_fetch_timeout)
            Captures: reactions, views, forwards, replies,
                      document_filename (non-image docs),
                      web_preview_{title,description,url,site}
   ↓
Stage 1b PINNED-MESSAGE DIFF (per topic, parallel with summarize)
            Fetches current pins, diffs vs media_cache/pins.json,
            prepends "### 📌 Pin Changes" to digest.summaries if changed
   ↓
Stage 2  VISION PRE-COMPUTE (parallel Haiku captions, cached)
            media_cache/captions.json keyed by filename
            Injects [Media/File: <caption>] into message content
   ↓
Stage 3  Content annotations
            [File: <name>]  for zips/pdfs/rars
            [Link: <site> — <title> — <desc>]  for URL previews
   ↓
Stage 4  Token-aware chunking + model escalation
            haiku-assigned  → tries haiku → sonnet → opus
            sonnet-assigned → tries sonnet → opus
            (splits chunk only if biggest model can't fit)
   ↓
Stage 5  CATEGORIZE (LLM, parallel per topic)
            per-channel system prompt + 10 few-shot examples (BASE_OUTPUT_GUIDANCE)
            → {category, status} per item
   ↓
Stage 6  ROUTE (Python, deterministic)
            _reclassify_by_topic_name (Request topics → category=request)
            _assign_priority via (category, status) → HIGH/MEDIUM/LOW
   ↓
Stage 7  Programmatic link grounding (reject hallucinated IDs)
   ↓
Stage 8  PREFERENCE (LLM, single call)
            Sees ALL items with (category, priority, status) hints
            Picks Top 5 by Adam's interests (2D / character / anatomy / Asian art)
            Writes Summary paragraph for the rest
   ↓
Stage 9  deep_linkify() — rewrite Instagram/Twitter/YouTube URLs to
            app-scheme URIs (instagram://, twitter://, vnd.youtube://)
   ↓
Stage 10 Markdown + PDF report
```

- **Domain**: `TelegramMessage` (with engagement counts + doc filename + link preview fields), `ChannelDigest`, `DigestItem` (discriminated union: file / course / discussion / request / announcement), `LinkItem`
- **Application**:
  - `GenerateDigestUseCase` — single-topic digest (used by worker, MCP, API)
  - `BatchScanUseCase` — many-topic orchestration (used by CLI scan, single or all)
  - `digest_processing.py` — post-LLM guardrails (allowlist, reclassify, priority)
  - `executive_summary.py` — cross-topic top-5 ranking
  - `worker.py` — long-running daemon mode (SSE)
- **Infrastructure**: `TelethonScraper`, `ClaudeProvider` (Agent SDK), `OrchestratedSummarizer`, `PDFRenderer`, `vision.py` (per-image Haiku caption cache), `pins.py` (pinned-message diff), `deep_links.py` (URL rewriter), `dedup.py` (cross-run seen-items filter), `runtime.py` (config singleton)
- **Interfaces**: CLI (`typer`), MCP (stdio), SSE server

**DDD layer contract**: `interfaces` → `application` → `infrastructure` → `domain`. Enforced by `import-linter`. Orchestration logic lives in `application/`, never in `interfaces/`. The previous `_scan_all_tasks` helper that lived in CLI is gone; all of it moved into `BatchScanUseCase`.

## Key Commands

```bash
# Scan (auto-publishes to TaskNotes vault by default — see "TaskNotes integration" below)
uv run course-scout scan                          # Yesterday, all topics, dedup on, +TaskNotes stub
uv run course-scout scan --today                  # Today (incomplete day)
uv run course-scout scan -d 3                     # Last 3 complete days
uv run course-scout scan --topic 10683            # Single topic by ID (no auto-publish)
uv run course-scout scan --topic "Pan Baidu"      # Single topic by name (substring)
uv run course-scout scan --no-pdf                 # Markdown only
uv run course-scout scan --no-dedup               # Re-surface previously-seen items
uv run course-scout scan --no-publish-task        # Skip TaskNotes stub (e.g. NAS Docker)

# Auxiliary
uv run course-scout list-topics <channel_id>      # List forum topics
uv run course-scout resolve-channel-id <alias>    # Resolve coursebusters → -1001603660516
uv run course-scout post-task                     # Manual publish from latest report
uv run course-scout post-task --date 2026-04-25   # Specific date

# QA
just test                # All tests (~1.5s)
just qa                  # lint + types + arch + coverage
just qa-strict           # qa + vulture + bandit + codespell
just check-dead          # vulture only
just check-security      # bandit only
just check-spelling      # codespell only
```

**One CLI command, two modes.** As of 2026-05-02 there is a single `scan`
command. Single-topic mode (`--topic`) and all-topics mode (no flag) flow
through the same `BatchScanUseCase` in the application layer, so they share
post-processing semantics by construction. The previous separate `digest`
command has been removed; everything goes through `scan`.

## TaskNotes Publishing

The weekly flow is integrated: `scan` auto-publishes a TaskNotes Inbox stub
at the end of every full run. The standalone `post-task` command remains
for re-publishing an older report on demand.

**Auto-publish rules** (in `interfaces/cli/main.py::_maybe_publish_task`):

- Default ON for full scans. Skip with `--no-publish-task`.
- Skipped automatically for `--topic` runs (single-topic = ad-hoc, not weekly).
- Best-effort: if the resolved vault dir doesn't exist (NAS Docker without a
  vault mount), it logs and moves on. The scan never fails because TaskNotes
  publishing failed.

The `post-task` command writes a TaskNotes-formatted stub of the most recent
scan into the user's Obsidian vault Inbox so the weekly digest surfaces as an
actionable task.

- Source module: `infrastructure/tasknotes.py` (`TaskNotesPublisher`)
- Output: `<vault>/TaskNotes/Inbox/course-scout-YYYY-MM-DD.md`
- Frontmatter: `status: inbox`, `tags: [task, course-scout]`, `contexts: [dailies]`
- Body: extracted Executive Summary + Top finds, plus `file://` links back to
  the full report (PDF opens in Skim if set as macOS' default PDF handler)
- Idempotent on (date, source) — re-running overwrites the same stub but
  preserves the original `dateCreated` so the TaskNotes age stamp doesn't reset
- Vault path resolution order: `--vault-dir` flag → `COURSE_SCOUT_VAULT_DIR`
  env var → default `~/Library/CloudStorage/OneDrive-Personal/Obsidian Vault`

### Cross-machine publishing (current and future)

**Current**: run `post-task` on the Mac. The vault filesystem is owned by
the Mac (OneDrive-synced from a Mac path), and the NAS reports dir is
SMB-mounted at `~/NAS/course-scout/reports/`, so the Mac-side invocation
can read NAS reports + write the vault stub directly. Mac is the single
writer; OneDrive propagates the stub to other devices.

**Why NOT publish from the NAS Docker container**: the container has no
access to the Mac filesystem, and reverse-mounting (Mac exposes vault
inbound to NAS) adds attack surface plus breaks on Mac sleep. Pushing
to OneDrive directly via rclone from the container would create a
second sync engine racing OneDrive — recipe for sync conflicts.

**Future cross-machine options** (when you want the stub to land even
when the Mac is asleep / off):

1. **rsync-from-NAS-then-publish on Mac wake**: NAS cron rsyncs each new
   `reports/<date>/` to a stable local path on the Mac when Mac is online,
   and a Mac launchd "wake from sleep" watcher fires `post-task` against
   it. Cleanest if you don't mind a 1-message-delay until Mac wakes.
2. **NAS writes a stub to a shared Syncthing folder** (not the OneDrive
   vault), and a Mac-side script merges it into `TaskNotes/Inbox/` on
   wake. Avoids the dual-sync-engine problem.
3. **Run a small Mac-side HTTP service** that NAS POSTs to after each
   scan; service writes the stub. Lowest latency, highest setup cost
   (need launchd-managed daemon + auth).

For now: launchd timer on the Mac firing `just post-task` at e.g. 7:30am
covers 95% of cases (Mac is awake by then) without any new infrastructure.

## Config

- `config.yaml` — topics, agent defaults, prompt templates, **runtime knobs** (`runtime:` block — see "Runtime config" below)
- `.env` — Telegram credentials (`TG_API_ID`, `TG_API_HASH`, `PHONE_NUMBER`)
- Auth: Claude Agent SDK auto-detects Claude Max subscription via CLI
- **SDK pinned to `claude-agent-sdk==0.1.65`** in `pyproject.toml`. Past pin to 0.1.50 was lifted after discovering:
  - The hang on multi-modal calls in 0.1.50 was *our bug*, not the SDK's — `query(prompt=[{...blocks...}])` with a bare list silently hits neither of the `str | AsyncIterable[dict]` branches, so stdin never closes. Fix: wrap content in an async generator that yields `{"type":"user","message":{"role":"user","content":[...]}}`. See `_stream_user_turn()` in `claude_provider.py` / `vision.py`.
  - `output_format=json_schema` injects a `StructuredOutput` pseudo-tool that consumes at least one turn internally. With richer inputs Haiku can need 3-4 turns — we use `max_turns=5` as a safety ceiling (unused turns don't cost tokens).
- **Why Agent SDK**: We use the Agent SDK (not the raw `anthropic` API) because we authenticate via Claude Max subscription, not an API key. The Agent SDK piggybacks on the Claude Code CLI auth.
- **Security / tool control**: We opt in with `allowed_tools=[]` (empty allowlist = no built-in tools at all). This is the future-proof pattern in 0.1.51+; `disallowed_tools` is a brittle blocklist because new built-in tools added upstream bypass it. `permission_mode` stays at default (no `bypassPermissions`). `setting_sources=[]` prevents filesystem settings injection.

### Runtime config (timeouts, retries, rate limits, log path)

All previously-hardcoded knobs (API timeouts, retry counts, rate limits, image caps,
Telethon fetch timeout, log path) live under the `runtime:` block in `config.yaml`.
Loaded once at startup via `infrastructure/runtime.py::get_runtime()` and cached;
removing the block uses defaults defined in `RuntimeConfig`.

```yaml
runtime:
  provider_call_timeout: 600.0       # outer wrapper around generate_structured()
  max_retries: 3                     # per-model retry attempts
  rate_limit_retry_sleep: 65.0       # sleep on 429/RATE error
  max_turns: 5                       # max turns per claude_agent_sdk.query()
  rate_limit_rpm: 50                 # local rate limiter rpm
  topic_fetch_timeout: 180.0         # per-topic Telethon fetch timeout
  max_images_per_call: 20            # max image attachments per LLM call
  log_path: "/tmp/course-scout-runtime.log"
```

**To add a new tunable knob:** add the field to `RuntimeConfig` in
`infrastructure/runtime.py` (with a default), then read it in code via
`from course_scout.infrastructure.runtime import get_runtime; rt = get_runtime()`.
Don't add new module-level constants for tunables — keep them in `RuntimeConfig`
so there's one place to manage configs.

**Runtime log** at `runtime.log_path` — append-only JSON-line file written by
`worker.py::_runtime_log()` around the batch run. One line per run with
`started_at`, `ended_at`, `duration_s`, `exit_status` (`ok` / `failed`),
`error`, and `traceback`. Tail with:

```bash
tail -f /tmp/course-scout-runtime.log | jq .
```

### Per-topic agent config

Each topic can override global defaults and use a specialized system prompt:
```yaml
agent_defaults:
  summarizer_model: "claude-sonnet-4-6"
  chunk_size: 10000      # token-aware chunking is primary; this is a safety cap
  max_messages: 100
  thinking: "adaptive"
  effort: "medium"

prompts:
  course_requests: |    # request channels — only category=request allowed
    ...
  file_sharing: |       # file-drop channels — file + discussion only
    ...
  discussion_lounge: |  # discussion channels — discussion + course + file
    ...
  course_review: |      # review channels — course + discussion only
    ...
  language_chat: |      # multilingual chats — all 5 categories
    ...

tasks:
  - name: "Coloso Requests"
    system_prompt: "course_requests"
    summarizer_model: "claude-haiku-4-5"   # cheap model for simple extraction
```

### Prompt Templates (in config.yaml `prompts:` section)

| Template | Used by | Allowed categories |
|----------|---------|--------------------|
| `course_requests` | All Request channels + GBUYB + Pan Baidu/Bilibili Download Request | request only |
| `file_sharing` | Members Collaboration | file + discussion |
| `discussion_lounge` | Asian Artists, 2D Artists, Webcomic | discussion + course + file |
| `course_review` | Course Review | course + discussion |
| `language_chat` | Russian, Spanish/Portuguese, Hindi/Urdu | all 5 |

**Every per-channel prompt is auto-prepended** with `BASE_OUTPUT_GUIDANCE` (`config.py`):
- Output schema reminders (no string-encoded lists, no trailing data)
- 10 worked input/output examples spanning fulfilled/unfulfilled requests, file shares, discussions, course reviews, SKIPs, interleaved threads, multi-thread channels, reply-across-gap, and stalled-thread patterns

### Token budgets and escalation

```python
_MODEL_BUDGETS = {
    "claude-haiku-4-5":  180_000,    # 200K context − 20K headroom
    "claude-sonnet-4-6": 950_000,    # 1M native, no beta header needed
    "claude-opus-4-7":   950_000,    # 1M native, no beta header
}
_ESCALATION = {
    "claude-haiku-4-5":  ["haiku", "sonnet", "opus"],
    "claude-sonnet-4-6": ["sonnet", "opus"],
    "claude-opus-4-7":   ["opus"],
}
```

`OrchestratedSummarizer.summarize()` estimates tokens (~3 chars/token), picks the smallest model in the chain whose budget fits, then chunks against THAT budget. Splitting only happens if even the top model can't fit.

## Pipeline (detailed)

1. **Fetch**: Telethon gets messages per topic with **180s timeout** (`TOPIC_FETCH_TIMEOUT_SEC` in `telegram.py`). On timeout, returns partial messages and continues — does NOT block the whole scan. Per-message we capture reactions, views, forwards, replies, document_filename (for zips/pdfs), and webpage preview fields.
2. **Pin diff** (`pins.py::diff_and_record`): fetches current pinned set via `InputMessagesFilterPinned`, diffs vs `media_cache/pins.json`, prepends a `### 📌 Pin Changes` block to `digest.summaries` if anything changed. First run per topic is silent (no "everything is new" spam). Errors swallowed — pinning must never break the main scan.
3. **Vision pre-compute** (`vision.py`): each attached image gets a cheap Haiku caption call (concurrency=5), cached at `media_cache/captions.json` keyed by filename. The caption is injected as `[Media/File: <caption>]` into the parser-visible content. Parser stays text-only.
4. **Content annotations** (`summarization.py::_prepare_structured_input`):
   - `[File: <filename>]` appended for non-image documents (highest-leverage signal for course drops — filename often carries title + instructor + platform verbatim)
   - `[Link: <site> — <title> — <desc>]` appended for URLs with webpage previews
5. **Token-aware chunk**: see escalation above. Single call when fits, else greedy-pack into ≤budget chunks.
6. **Categorize**: Each chunk → Claude → `SummarizerOutputSchema`. Parallel across topics. Per-channel prompt + base guidance + few-shot examples.
7. **Python post-processing**:
   - `_reclassify_by_topic_name`: items in topics with "Request" / "Download" in name → forced category=request (defense against parser drift)
   - `_assign_priority`: deterministic priority from (category, status). LLM does not pick priority.
8. **Merge**: Combine chunk outputs (items, links).
9. **Ground**: Programmatic link validation — reject hallucinated IDs (>32-bit), verify URLs against raw messages.
10. **Preference / Executive Summary**: One Claude call sees ALL items, ranks Top 5 by Adam-relevance (LLM judgment), writes Summary for rest.
11. **Deep-link rewrite** (`deep_links.py::deep_linkify`): rewrites Instagram/Twitter/X/YouTube URLs in the combined markdown to app-scheme URIs (`instagram://`, `twitter://`, `vnd.youtube://`) so they open the native app instantly on mobile. Keeps https fallback in parens for desktop.
12. **Report**: Markdown + PDF with clickable links. Section headers use `[FILES]`, `[REQUESTS]`, `[DISCUSSION]`, etc. (no emojis).

## Failure-mode defenses

| Mode | Defense | Where |
|---|---|---|
| Telegram connection drops | 180s per-topic timeout, return partial | `telegram.py::TOPIC_FETCH_TIMEOUT_SEC` |
| SDK hangs on multi-modal `query()` | Wrap content blocks in `_stream_user_turn()` async generator (SDK needs `str \| AsyncIterable[dict]`, not bare list) | `claude_provider.py`, `vision.py` |
| `error_max_turns` with `output_format=json_schema` | `max_turns=5` (StructuredOutput pseudo-tool eats turns) | `claude_provider.py::generate_structured` |
| Vision call rate-limited by sync lock | `asyncio.Lock + asyncio.sleep` rate limiter (prior `threading.Lock + time.sleep` froze the event loop) | `rate_limiter.py`, `agents.py` |
| Parser emits `{"items": "[...]"}` (string-encoded list) | JSON repair: parse string fields as JSON, truncate to last balanced bracket | `claude_provider.py::_repair_string_json_fields` |
| Topic name says "Request" but parser emits `file` | Python post-processing forces request category | `application/digest_processing.py::reclassify_by_topic_name` |
| Parser sets wrong priority | Deterministic Python override | `application/digest_processing.py::assign_priority` |
| Configured topic with N=1 message silently dropped | Removed `>=3` filter; now `if messages:` (any non-zero count). Regression guard in `tests/integration/test_batch_scan_coverage.py::test_single_message_topic_not_dropped` | `application/batch_scan.py::_fetch_all` |
| Context overflow on big topics | Token-aware chunking + model escalation | `summarization.py::_pick_model` |
| Pin fetch fails for one topic | Logged + swallowed — never breaks main scan | `pins.py::diff_and_record` |

## Benchmark Setup (`benchmark/`)

Standalone dev tooling (not wired into main CLI). Two benches per pipeline stage:

```
benchmark/
├── sample.py                    # extract fixtures from logs/course_scout.log
│                                # --days 1|7|30 [--full-topic to merge same-topic-same-day chunks]
├── label.py                     # interactive labeler for CATEGORIZE
├── autolabel_categorize.py      # parser self-labels (smoke test for plumbing)
├── bench_categorize.py          # score category accuracy + Hungarian-matched set F1
├── bench_preference.py          # score Precision@5 on top-5 ranking
├── bench_sweep.py               # YAML-driven multi-config sweep, shared semaphore
├── compare_chunking.py          # chunked vs full-topic structural comparison
├── group_by.py                  # per-prompt accuracy slice
├── inspect_failures.py          # show thinking trace for failures
├── quick.py                     # one-shot autolabel + categorize eval
├── configs/default.yaml         # sweep config: models × efforts to test
├── fixtures/{1,7,30}d.jsonl     # parser-input chunks
├── fixtures/{1,7,30}d_full.jsonl  # one chunk = one topic's full day
├── labels/                      # hand-labeled ground truth (TODO)
├── results/                     # cached predictions + score reports
└── TODO.md                      # ground-truth labeling backlog
```

**Current bench status**: Hand-labeled gold exists at `benchmark/labels/canon10.yaml` — 105 samples (10 non-empty days per channel × 13 channels), 359 gold items. F1 ≈ 0.488 against gold (as of Apr 2026). Self-label F1 = 0.878 is the saturated number; gold reveals the real gap. Next bench pass should run against gold with the new `[File:/Link:]` annotations flowing.

**Concurrency**: 5 parallel calls (Anthropic Max plan guidance). Higher risks "concurrent connections" 429.

## Topics Scanned

| Category | Channels | Model | Prompt |
|----------|----------|-------|--------|
| Discussion lounges | Asian Artists, 2D Artists, Webcomic | sonnet-4-6 | discussion_lounge |
| Course review | Course Review | sonnet-4-6 | course_review |
| Language chats | Russian, Spanish/Portuguese, Hindi/Urdu | sonnet-4-6 | language_chat |
| Course requests | Coloso, 2D Related, Animation, Domestika, Class 101, Wingfox, Patreon, ALL REQUESTS | haiku-4-5 | course_requests |
| Download requests | Pan Baidu Download Request, Bilibili | haiku-4-5 | course_requests |
| File sources | Members Collaboration, **Pan Baidu Files**, **Pan Baidu Courses** | sonnet-4-6 | file_sharing |
| External | GBUYB | haiku-4-5 | course_requests |

**Note**: Pan Baidu Files (10686) and Pan Baidu Courses (319355) were added on
2026-05-02 along with the >=3 filter fix. Pan Baidu Download Request (10683)
was already in the config but was being silently filtered out by the bug;
it now produces output for single-message days (typical for that topic).

## Conventions

- No verifier agent — programmatic grounding replaces LLM verification
- Default scan = yesterday (midnight to midnight), `--today` for rolling
- PDF is default (use `--no-pdf` to skip)
- Logs default to `/tmp/course-scout/` (override via `COURSE_SCOUT_LOG_DIR` env var). Per-topic logs at `/tmp/course-scout/scans/YYYY-MM-DD_HHMMSS/<topic_name>.log`, rotating root log at `/tmp/course-scout/course_scout.log`
- Reports saved to `reports/YYYY-MM-DD/scan_YYYY-MM-DD.{md,pdf}`
- Caches: `media_cache/captions.json` (vision pre-compute), `media_cache/pins.json` (per-topic pinned-message snapshots), `media_cache/media_<msg_id>.jpg` (downloaded images)
- Section headers in reports use bracket tags (`[FILES]`, `[REQUESTS]`, `[DISCUSSION]`) not emojis
- Priority is **deterministic** (Python from category+status). Parser MUST NOT set it — the schema field exists but the prompt instructs to leave null.

## Testing & QA

**137 tests, 65% coverage, all gates green** as of 2026-05-02.

```
tests/
├── domain/           Pure-function tests (no IO)
├── application/      Use cases against fakes — including:
│                     test_executive_summary.py (100% module cov)
│                     test_digest_usecase.py + test_digest_extra.py
│                     test_worker.py
├── infrastructure/   Adapters — telegram, dedup, agents, providers,
│                     reporting, summarization, config, rate_limiter,
│                     persistence, pins, schema_parsing, notifier
├── integration/      Cross-layer end-to-end with fake Telegram + LLM:
│                     test_batch_scan_coverage.py — 6 tests including
│                     the 1-msg-topic regression guard
└── interfaces/       CLI / API / MCP entry surfaces
                      test_cli.py + test_cli_extra.py + test_cli_helpers.py
                      test_sse.py + test_mcp.py
```

**Coverage gate**: 60% (set in `pyproject.toml [tool.coverage.report]`).
Currently at 65%. Some modules are intentionally at 0% — `vision.py`,
`deep_links.py`, `tasknotes.py` are separate-concern, low-risk surfaces.

**QA tooling** (configured in `pyproject.toml`, fronted in `justfile`):

| Tool | Purpose | Config |
|---|---|---|
| `ruff` | Lint + format | `[tool.ruff]` |
| `pyright` | Static types | `[tool.pyright]` (`typeCheckingMode = "standard"`) |
| `import-linter` | DDD layer enforcement | `[[tool.importlinter.contracts]]` |
| `pytest-cov` | Coverage | `[tool.coverage.report]` |
| `vulture` | Dead code | `[tool.vulture]` (whitelists CLI/API decorators + entrypoint names) |
| `bandit` | Security lint | `[tool.bandit]` (skips B101/B104/B108 — intentional /tmp + dev asserts) |
| `codespell` | Typos | `[tool.codespell]` |

`just qa-strict` runs the full chain: fix → check-types → check-architecture
→ check-dead → check-security → check-spelling → coverage. Use as the
pre-commit / pre-push gate.

## Observability

Every Python override of parser output is logged to `logs/overrides.jsonl`:
```json
{"ts": "...", "stage": "allowlist|reclassify",
 "topic": "Coloso Requests", "before": "file", "after": "request",
 "title": "...", "reason": "..."}
```

Use this to audit when/why guardrails fire:
```bash
# How often does each stage fire?
jq -r .stage logs/overrides.jsonl | sort | uniq -c

# Which channels trigger the most remaps?
jq -r '[.topic, .stage, .before, .after] | @tsv' logs/overrides.jsonl | sort | uniq -c | sort -rn

# What does file_sharing over-emit?
jq 'select(.stage == "allowlist" and (.title | startswith("WLOP")))' logs/overrides.jsonl
```

If a guardrail fires frequently, fix the prompt upstream. If it never fires, consider deleting the guardrail.

## Bench Methodology (current limits & scoring)

Bench scores at **3 Hungarian thresholds** (60 / 75 / 90) to expose the title-match sensitivity. Also reports:

| Metric | What it measures | Game-ability |
|---|---|---|
| Set F1 @ threshold 60 | Loose match, legacy number | High — similar titles pair |
| Set F1 @ threshold 90 | Tight match, near-identical titles | Low |
| **Strict match F1** | title≥90 AND category correct | Lowest — the "really right" metric |
| Category acc on matched | Category correctness on pairs we found | **Biased up** when recall drops |
| Category acc over gold | Correct / total_gold (unmatched = miss) | Drops when recall drops — prefer this |

**Self-labels are a lower bound on self-agreement, not accuracy.** A consistently-wrong parser scores 1.0 on self-labels. Real F1 requires hand labels.

## Backlog

Prioritized. Items marked ⭐ have highest signal-to-effort ratio.

### Benchmark

- ⭐ **Hand-label 50 items of the 1d fixture** — only way out of the self-label trap (Husain/Shankar consensus). Deferred — labeling fatigue. Tracked in `benchmark/TODO.md`.
- ⭐ **Bootstrap CIs instead of point estimates** — n=13 gives F1 CI of ~[0.55, 0.95]. Per-slice metrics with n<20 should be dropped, not reported.
- **Track status + priority fields in scoring** — currently only `category` is scored. Regressions in status/priority are invisible.
- **Label preference bench** — `bench_preference.py` exists but needs pool items tagged as RELEVANT/MAYBE/IRRELEVANT.
- **Verify coalesced discussion consolidation** — 7d full-topic had 10 fewer `discussion` items vs chunked. Hypothesis: consolidation, not loss. Spot-check 2-3 samples.

### Pipeline hardening

- **Engagement-based ranking** — reactions/views/forwards are captured on `TelegramMessage` but not yet consumed. Two possible uses: post-hoc sort items by engagement before preference call, or re-introduce as parser-visible fields if a bench shows material gain.
- **Vision benchmark** — separate per-image gold (title / instructor / platform / readable-text) to measure caption quality independently. Caption quality directly gates end-to-end F1 now that annotations flow verbatim.
- **More deep-link domains** — only Instagram/Twitter/YouTube today. Add Pinterest, TikTok, Bilibili, ArtStation as they show up in scans.
- **Pydantic `Extra data` errors on large outputs** — occasional failure mode on long coalesced inputs. Mitigated by `_repair_string_json_fields` but not eliminated.
- **Consider "assert" mode for dev** — when `_enforce_category_allowlist` fires, optionally raise instead of silently remap. Surfaces prompt bugs during bench runs; stays silent in prod.

### Deletable candidates (measure first, then delete)

Reviewer critique flagged these as possible dead code. Instrument via `logs/overrides.jsonl` first, then decide:
- **Model escalation** — personal scanner may never exceed Haiku 180K; if override log shows 0 escalations over a week, delete it.
- **5 prompts** — if override counters show low leakage between channels, test "1 unified prompt + `<channel_profile>` block" for token savings.

### Docker / NAS Deployment

- Dockerfile for scheduled scans on UGREEN NAS
- Cron-based weekly scan (Sundays 1am) with Telegram notification of results
- Session file persistence across container restarts

### Claude Agent SDK Enhancements

- **Custom `@tool` decorator** — vault search (course exists?), Calibre lookup (PDF downloaded?), NAS art course catalog cross-reference
- **MCP server integration** — connect `vault-mcp` and `arete` to Agent SDK for cross-referencing during summarization
- **Hooks system** — `PreToolUse` audit logging to replace manual per-topic file loggers
- **`max_budget_usd`** — hard cost cap per scan as safety net

### Output Improvements

- Cross-topic deduplication (same course requested in multiple topics)
- Historical tracking — compare today's scan to yesterday's, highlight new items only
- Cross-stage `trace_id` — track an item from parser output through every Python stage. Currently we can't answer "why did item X get priority HIGH?" without grepping.
