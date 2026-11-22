# Changelog

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
