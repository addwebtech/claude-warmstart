---
name: savings
description: Use when the user types /warmstart:savings to see measured token costs and savings from cache reloads, model switches and long conversations.
disable-model-invocation: true
allowed-tools: Bash(python3 *) Bash(python *)
---

Show the measured savings. Reply in the language the user writes in.

Run the engine with `python3`; if the command is not found, run the identical command with `python`.

1. Run (ONE call; it reads Claude Code's own logs, uses no model tokens and no network). For this project:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" ahorro`
   If the user asks for "all projects", add `--todos`. It prints only; it writes nothing unless `--guardar` is added.
2. Paste the report as is (table and percentages). Do not recompute or round it.
3. Close with at most 3 lines: the most expensive cause the report shows and the concrete action that avoids it. If "Desde que instalaste las guardias" is absent, say there are no real guard events yet and the numbers above come from history.
4. If asked why a number is what it is, explain with the report's footnote (price weights and the assumption that quota scales with cost). Never claim a saving the report does not show.
