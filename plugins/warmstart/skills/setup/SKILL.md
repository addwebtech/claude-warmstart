---
name: setup
description: Use when the user types /warmstart:setup to install or remove the warmstart hooks that guard against expensive cache reloads and auto-save the session.
disable-model-invocation: true
allowed-tools: Bash(python3 *) Bash(python *)
---

Install the warmstart hooks. Reply in the language the user writes in. The hooks go into the user's `~/.claude/settings.json`, so show the plan first and wait for approval.

Run the engine with `python3`; if the command is not found, run the identical command with `python`.

1. Show the plan (writes nothing):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" instalar --sin-skills --simular`
2. Summarize it in three lines: which hook events are added, that `settings.json` is backed up first, and that existing hooks are untouched. Ask: "Install?"
3. On a yes, run the same command without `--simular`:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" instalar --sin-skills`
4. Tell the user to open a NEW conversation so the hooks load. To undo later: same command with `desinstalar` instead of `instalar`; their saved data stays.

Never edit `settings.json` yourself; only the engine does, with a backup and a check that nothing else changed.
