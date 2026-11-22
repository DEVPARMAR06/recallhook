# recallhook

[![tests](https://github.com/DEVPARMAR06/recallhook/actions/workflows/test.yml/badge.svg)](https://github.com/DEVPARMAR06/recallhook/actions/workflows/test.yml) [![PyPI](https://img.shields.io/pypi/v/recallhook)](https://pypi.org/project/recallhook/) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

**Private, local long-term memory for Claude Code.** Tell Claude something once, like *"I use pnpm"* or *"Atlas deploys to Fly.io"*, and every future session already knows it, in the **Claude desktop app, the VS Code extension and the terminal**.

No cloud, no proxy, no API key: a Claude Code hook + [mem0](https://github.com/mem0ai/mem0) + your own [Ollama](https://ollama.com) models. In our tests it **costs the same as retyping the context yourself, and nothing at all on prompts that don't need it.**

![recallhook dashboard](https://raw.githubusercontent.com/DEVPARMAR06/recallhook/main/docs/dashboard.jpg)

```text
You:     What port does the Atlas dev server use, and how do I run its tests?

 without recallhook   "I don't know. Let me search your files..."   (0/9 correct, wasted tool calls)
 with recallhook      "pnpm test:unit, port 4310."                  (9/9 correct, 57% cheaper than the search)
```

## Why

- Every new Claude Code session starts blank: your stack, conventions, ports, deploy targets, "never force-push to main"... you re-explain them, or maintain a `CLAUDE.md` by hand.
- Hosted memory services send your conversations to someone else's cloud.
- Memory tools that rely on the model *choosing* to call a tool are unreliable; this one injects relevant facts deterministically before the model sees your prompt.

recallhook watches what you tell Claude, stores the durable facts locally, and quietly adds the relevant few lines to future prompts.

## How it works

```text
you type a prompt ──▶ Claude Code UserPromptSubmit hook ──▶ local gateway (:8788) ──▶ mem0 + Ollama
                            ▲   relevant memories added as context (~30-150 tokens, ~50 ms)
                            └── durable statements queued for background fact extraction
```

- **Recall** (~50 ms): the closest memories above a relevance cutoff are added to your prompt. Each memory is injected once per session (it stays in the transcript), so it doesn't repeat and burn tokens.
- **Capture** (background): a local model extracts lasting facts from statements like *"my project X uses..."*, *"I always..."*, *"remember..."*. Questions and one-off tasks ("write a one-liner...") are **not** stored.
- **Fail-open**: if anything is down or slow, the hook does nothing and your prompt goes through untouched.
- Everything is stored in `~/.recallhook` (Qdrant + SQLite). Nothing leaves your machine except what you type to Claude as usual.

| Surface | Status |
|---|---|
| Claude desktop app (Code tab) | works, tested |
| Terminal `claude` | works, tested |
| VS Code extension | works, tested |

## Quickstart

You need: Python 3.10+, [Ollama](https://ollama.com) installed and running, and Claude Code. The default models are about 5.5 GB to download and an 8B model wants roughly 6 GB of VRAM/RAM (a lighter option is below).

```bash
pipx install recallhook      # or: pip install recallhook   (pipx keeps its dependencies isolated)
recallhook init              # checks Ollama, downloads the models (asks first), installs autostart + hook
recallhook status            # tells you what works and how to fix what doesn't
recallhook ui                # open the dashboard
```

`init` is safe to re-run. Hooks load when a session starts, so **open a new Claude session** afterwards. Try it: say *"My project Atlas runs its tests with `pnpm test:unit` and its dev server listens on port 4310."* In a **new** session ask *"how do I run the Atlas tests?"*

Autostart is Windows-only for now (a hidden Task Scheduler job). On macOS/Linux run `recallhook serve` yourself and `recallhook hooks on` (a systemd/launchd unit would make a great PR).

### Prefer a Claude Code plugin?

The hook is also available as a plugin, so you can add and remove it from inside Claude Code:

```text
/plugin marketplace add DEVPARMAR06/recallhook
/plugin install recallhook@recallhook
```

The plugin registers the hook and adds two commands: `/recallhook:status` and `/recallhook:ui`. It still needs the gateway and models from the quickstart, so install the package and run **`recallhook init --no-hook`** (the plugin supplies the hook, so the settings hook is skipped). If both end up active, `recallhook status` warns and `recallhook hooks off` keeps the plugin. If `recallhook` isn't installed, the plugin's hook does nothing and your prompts are unaffected.

## Choosing and changing models

| Model | Size | Notes |
|---|---|---|
| `qwen3:8b` (default) | 5.2 GB | best extraction in our checks; ~7 s per message once warm |
| `llama3.2:3b` | 2.0 GB | faster (7 s vs 15 s on our 3-message check) and lighter, but caught 4 of 5 test facts vs 5 of 5, so expect the occasional missed fact |
| `nomic-embed-text` (embeddings, default) | 0.3 GB | what the 0.5 relevance cutoff is tuned for |

Only these were tested. Other chat models should work, since any Ollama model can extract facts, but compare before you trust one: `python scripts/bench_models.py qwen3:8b <your-model>`.

No code changes needed. Settings live in a config file, `~/.recallhook/config.json`, and can be changed from the command line:

```bash
recallhook config show                       # every setting, and where its value comes from
recallhook config set llm_model llama3.2:3b  # saves it and restarts the service
recallhook init --llm-model llama3.2:3b      # same, and downloads the model if missing
recallhook config unset llm_model            # back to the default
```

Precedence: command-line flag > `RECALLHOOK_*` environment variable > config file > default. Settable keys: `llm_model`, `embed_model`, `embed_dims`, `ollama_url`, `port`, `num_ctx`, `keep_alive`, `embed_on_cpu`, `user_id`, `top_k`, `min_score`, `search_timeout`, `inject`, `write`.

**Changing the embedding model is different.** Vectors from different embedding models can't be mixed, so recallhook refuses a silent switch (and the dashboard shows why). Do it in three steps; your memories are kept and the old vectors are backed up:

```bash
recallhook config set embed_model mxbai-embed-large
recallhook config set embed_dims 1024        # the vector size of that model
recallhook reindex
```

Similarity scores differ between embedding models, so you may need to retune `recallhook config set min_score <0-1>`.

## Commands

| Command | What it does |
|---|---|
| `recallhook init` | guided first run: checks Ollama, downloads models, installs autostart + hook |
| `recallhook status` | what works / what doesn't, each problem with its fix |
| `recallhook ui` | open the dashboard |
| `recallhook config show / set / unset` | view and change settings |
| `recallhook reindex` | rebuild the store after changing the embedding model |
| `recallhook restart` | restart the background service |
| `recallhook install` | (re)install autostart + hook; **also the upgrade step** |
| `recallhook uninstall` | remove autostart + hook, stop the processes |
| `recallhook hooks on / off` | add or remove only the Claude Code hook |
| `recallhook serve` | run the gateway in the foreground (macOS/Linux, debugging) |

## Upgrade, uninstall, troubleshooting

- **Upgrade:** `pipx upgrade recallhook` (or `pip install -U recallhook`), then `recallhook install`. The running service keeps its old code until restarted, and `install` restarts it.
- **Uninstall:** `recallhook uninstall` **first**, then `pipx uninstall recallhook` (or `pip uninstall recallhook`). Doing it the other way round leaves the autostart task and hook behind. Your memories stay in `~/.recallhook`; delete that folder to erase them.
- **"It doesn't remember anything":** run `recallhook status`. It checks, in plain words: Ollama running? Models downloaded? Embedding model matches the store? Hook installed, and does the Python it points to still exist? Gateway up? The dashboard shows the same problems in a red banner.
- **Logs:** `~/.recallhook/logs/gateway.log`.
- A moved or deleted Python environment breaks the hook silently (it is fail-open on purpose). `recallhook install` fixes the path.

## Does it save anything? (we measured)

Billed usage from `claude -p --output-format json`, Haiku, 3 rounds × 6 prompts. Three facts about a made-up project were stored; prompts either needed them or were unrelated. Interleaved runs, warm prompt cache.

| | no memory | **recallhook** | you retyping the facts |
|---|---|---|---|
| prompts needing the facts: correct | 0 / 9 | **9 / 9** | 9 / 9 |
| cost per call | $0.0061 | **$0.0026** | $0.0026 |
| time per call | 5.0 s | **4.0 s** | 3.4 s |
| unrelated prompts: cost per call | $0.0030 | **$0.0029** | n/a |

How to read this honestly:

- Without memory the model can't know the answer, so it burns turns searching files and still fails; that is where the extra cost comes from.
- **Against typing the facts yourself the cost is identical.** What you save is the typing, and not forgetting. The value is convenience and correctness, not a discount.
- On unrelated prompts nothing is injected (cutoff 0.5), so there is no overhead. Our first version used a 0.3 cutoff and injected ~170 tokens of unrelated memories per prompt, which cost +24% on those calls. We measured it, found it, and fixed it.
- Small sample, one synthetic project, one cheap model. Reproduce or extend it: [`bench/`](https://github.com/DEVPARMAR06/recallhook/tree/main/bench).

### What we tried that did *not* pay off

We first built this as a proxy in front of the [Headroom](https://github.com/chopratejas/headroom) compression proxy. Billed cost for a 5-prompt tool-heavy session: direct $0.060, Headroom default **+14%**, Headroom lossless **+16%**, and routing through even a bare pass-through gateway **+27%** (cause not found). Rewriting requests breaks prompt-cache reuse, so short sessions pay more than they save. We dropped the proxy for the hook. (Headroom's own dashboard "savings" are estimates; billed usage is what counts.) The optional proxy mode is still in the code (`serve --spawn-headroom`, `install --with-headroom`) but off by default and not recommended.

## Dashboard

`recallhook ui` or `http://127.0.0.1:8788/_recallhook/ui`

Live stats (memories, recalls, hit rate, latency, tokens injected), a 14-day chart, a live activity feed showing which memories were recalled or stored, a **"needs attention" banner** when something is misconfigured, and search / delete for every memory. Localhost-only; writes need a custom header and a foreign `Host` header is rejected, so web pages can't reach it. Prompt text is never logged, only counts, timings and the memory snippets involved.

## Lessons baked in

- **Strip the wrapper.** A new desktop session's first message starts with thousands of characters of app boilerplate. Naively searching on the start of the prompt missed the real question; recallhook strips it and searches the start *and* end of long prompts.
- **Ignore automated turns.** Harness notifications arrive as "user" messages; they are never recalled for or stored.
- **Don't store questions.** Capturing "what port does X use?" made mem0 write near-duplicate facts.
- **Tune Ollama.** mem0 exposes none of Ollama's options, so the gateway sets them: 4k context (the 32k default spilled a 7B model onto the CPU), thinking off (`qwen3`: 150 s → ~7 s per extraction), models kept loaded, embeddings on the CPU so recall never waits behind the LLM. Extraction 15-150 s → 5-8 s; recall max 0.05 s during writes.

## Privacy & safety

- Stored locally and **unredacted**. Don't type secrets you wouldn't want remembered. Delete any memory in the dashboard, or remove `~/.recallhook`.
- Recalled memories are added to your prompt, so those few lines reach Anthropic like anything else you type.
- Only *your* prompts are captured, never assistant replies or tool output.

## Limits

- Built and used on Windows 11. The unit tests also pass on Linux in CI, but the full Ollama + hook flow is untested off Windows. Autostart is Windows Task Scheduler only for now.
- **English only.** The "is this a lasting statement?" filter, the question detector and the fact-extraction instructions are English heuristics. Other languages will mostly be skipped.
- One shared memory per user id (no per-project scoping yet).
- Small local models sometimes miss a fact on the first try (the writer retries once); extraction quality follows your model.
- The capture filter favours "I always / my project / X uses / remember" phrasing. Durable facts phrased as bare commands may be skipped.

## Develop

```bash
pip install -e ".[dev]" && pytest
```

Issues and PRs welcome, especially Linux/macOS autostart, per-project scoping, non-English support and more benchmarks. MIT licensed.
