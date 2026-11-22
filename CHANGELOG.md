# Changelog

## 0.2.0
- **Smart capture:** project knowledge is now learned from nearly every prompt and from Claude's final reply each turn (new Stop hook; `recallhook install` adds it, the plugin includes it), instead of only from explicit statements. `capture_mode strict` restores the old behaviour; `capture_assistant false` turns reply capture off.
- **No more invented memories:** text with no concrete details is skipped, every extracted memory must be grounded in its source text, and narration ("the user ran the tests") is dropped.
- **Fixed:** mem0 fed the last 10 captured messages into each extraction, leaking old facts onto new projects. Disabled (this also stops mem0 keeping a raw copy of captured text).
- Facts are attributed to the project folder the session ran in; a fact naming a project that its source never mentioned is rejected (the model was pinning new facts on existing projects).
- **Stale memories:** snapshots of work in progress ("has 46 files that may be incorrectly filed") are no longer stored; recalled memories older than a week show their age; new "Forget that ..." prompt deletes the best-matching memories.
- Fixed: the dashboard and memory count were capped at 20 memories (mem0's default limit).
- **Outdated memories are replaced:** when a new fact changes an old one (port, version, tool), the old memory is retired automatically (one changed number: deterministic; otherwise one narrow yes/no model question per close memory; at most 2 per fact). Each replacement is logged with its old text and the dashboard has a "restore old" button. `replace_outdated false` turns it off.
- **Secrets are masked** (API keys, tokens, private keys, passwords, credentials in URLs) before text reaches the extraction model, and any memory that still looks like a credential is deleted.
- When the capture queue is backed up, the oldest work is dropped instead of the newest; queue size 32.
- `recallhook init` explains that Claude's replies are learned from and asks first (`--no-assistant` to decline).
- Fixed: a literal line break inside a JavaScript string in the dashboard (caught by a new test that scans the page script).
- Dashboard: "Why some prompts aren't stored" panel with counts by reason (question, task, automated message, too short, already stored, ...). Only the reason is logged, never the prompt text. Skipped prompts also appear in the live activity feed.
- README: troubleshooting entry for "nothing is being stored" and the "Remember that ..." tip.

## 0.1.3
- Docs only: the VS Code extension is now confirmed working (desktop app, VS Code and terminal are all tested).

## 0.1.2
- Claude Code plugin + marketplace (`/plugin marketplace add DEVPARMAR06/recallhook`): registers the hook and adds `/recallhook:status` and `/recallhook:ui`.
- `init --no-hook` / `install --no-hook` for plugin users; `status` recognises the plugin and warns if the hook is registered twice.

## 0.1.1
- `recallhook init`: guided first run. Checks Ollama, downloads missing models (asks first), installs autostart + hook.
- Config file `~/.recallhook/config.json` and `recallhook config show/set/unset`; the autostarted service and the hook now honour it (including the port).
- `recallhook status` explains every problem with its fix; the dashboard shows a "needs attention" banner.
- Embedding-model guard: changing `embed_model` without rebuilding is refused with an explanation; `recallhook reindex` rebuilds the store and keeps memories.
- `recallhook install` now restarts the service, so it is also the upgrade step; `recallhook restart` added.
- `recallhook uninstall` explains the order (uninstall before `pip uninstall`).
- Fixed: unset flags no longer override config-file values.
- README: model guide (qwen3:8b default, llama3.2:3b lighter option), upgrade/uninstall/troubleshooting, English-only note.

## 0.1.0
- First public release.
