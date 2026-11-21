---
name: save
description: Use when the user types /warmstart:save to close a work session so a brand-new conversation can resume without reloading the history.
disable-model-invocation: true
allowed-tools: Bash(python3 *) Bash(python *) Bash(grep *)
---

Save the state so a NEW conversation can continue. Reply in the language the user writes in. Work in the current project folder.

Run the engine with `python3`; if the command is not found, run the identical command with `python`.

## Rules (not negotiable)
- Never create or edit CLAUDE.md, ESTADO.md or any other project file on your own: Claude Code already loads CLAUDE.md, and what goes into the user's own files is the user's call. The only file you write is `.claude/handoff/HANDOFF.md`, and only through the engine's `guardar` (the engine already archived the previous one).
- Never write keys, tokens or passwords: for a credential, write only its name.
- Anything the user did not say and you did not verify this session is marked "(unverified)". Do not fill gaps.
- Do not read whole state files: find headings with `grep -n '^#'` and read only the needed range. Reading 40 KB to add one line is the cost this skill exists to avoid.

## Steps
1. Prepare (ONE call, uses no model tokens):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" preparar`
   It returns the previous handoff (already archived), backups of the state files, an automatic summary of the session (requests, last reply, tasks, files, IDs) and files touched that no state file mentions.
2. Carry over: review every line of the previous handoff. Finished and durable -> ONE line in the state file (step 4). Unresolved and not mentioned today -> keep it as "(previous, unconfirmed)". "Didn't work" accumulates (max 8). Never delete silently.
3. Write the handoff with the engine, NOT with the Write tool (Claude Code protects `.claude/` and would refuse or prompt). Pipe the content to `guardar` with a heredoc; the engine rejects keys, writes `.claude/handoff/HANDOFF.md` and verifies it. At most ~1,000 tokens, in this order:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" guardar <<'HANDOFF_EOF'
   # HANDOFF <project> -- <date time>
   **Next action:** <ONE concrete action: file/node/workflow and what to do>
   **Open question for the user:** <the last pending question, verbatim, or "none">
   **In progress:** <what is half done and exactly where>
   **Pending (priority):** - ...
   **Blockers / to decide:** <or "none">
   **Decisions (with why):** - decision -- why (only what the user said or approved)
   **Didn't work (don't repeat):** - what was tried -- why it failed
   **Where things are:** paths, workflow/sheet IDs, URLs, credential NAMES (no values)
   **Unverified:** - ...
   HANDOFF_EOF
   ```
   Use the automatic summary as a checklist: nothing important in it may go unmentioned. Always include the open question when Claude's last reply ends in a question. The engine requires the text "Next action" (or "Siguiente accion") and refuses anything that looks like a key.
4. Project files, only if the output of step 1 lists "ARCHIVOS DE ESTADO DEL USUARIO" (a state file the user maintains, such as ESTADO.md or PROGRESO.md): if durable facts (architecture decisions, IDs, credentials to review, stack changes) are missing from it, add ONE line in the right section with Edit and update its "Last updated" line if present. If it says "ninguno", do nothing here and never create one. If durable facts belong in CLAUDE.md, show the user the exact line and ask first.
5. `guardar` already verified HANDOFF.md. Only if you edited a state file in step 4, verify again (ONE call):
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" verificar`
   On FALLO/FAIL: fix and verify again. If a state file shrank, the engine already restored it from backup: re-add only new lines with Edit.
6. Reply ONLY with: what was saved (HANDOFF.md, plus any state-file line you added), the "(unverified)" lines for the user to confirm, and the Next action. End with: "You can open a new conversation now; the handoff loads by itself. Type /warmstart:resume to see the receipt."
