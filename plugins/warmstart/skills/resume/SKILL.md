---
name: resume
description: Use when the user types /warmstart:resume at the start of a new conversation to continue project work from the saved handoff.
disable-model-invocation: true
allowed-tools: Bash(python3 *) Bash(python *) Bash(grep *)
---

Resume work without loading the history. Reply in the language the user writes in. Do not edit any file until the user confirms.

Run the engine with `python3`; if the command is not found, run the identical command with `python`.

1. If the block "=== HANDOFF del proyecto" already appears earlier in this conversation (the SessionStart hook injects it), use it and run nothing. Otherwise run (ONE call):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" contexto`
2. If it says "NO HAY HANDOFF": say so and suggest `/warmstart:save` in the working session. CLAUDE.md is already in your context (Claude Code loads it); if the project also has a state file such as ESTADO.md, read only its first 40 lines to get oriented.
3. Freshness, one line, never blocking: handoff older than 7 days, different branch, uncommitted changes, or the warning "work happened AFTER the last save" (the engine prints "hubo trabajo DESPUES del ultimo guardado").
4. Receipt, max 6 lines: project, handoff date, in progress, blockers, **Next action**, the **open question** if any, and "(unverified)" lines for the user to confirm.
5. Ask "Proceed with the next action?" and wait.
6. After confirmation, do the action. Read from state files only the sections it needs (`grep -n '^#'`, then that range). If a fact that should be there is missing, say so and ask; do not invent it. For detail from earlier sessions, grep `.claude/handoff/sesiones/`; never load it whole.
7. When the block of work ends, suggest `/warmstart:save`.
