# Warmstart

> Español: [README.es.md](README.es.md)

**Start new Claude Code conversations warm, and stop paying to reload old ones.**

Warmstart is a Claude Code plugin (Python, standard library only) that:

1. **Auto-saves a handoff** of each session with no model tokens: your last requests, Claude's last reply (including an unanswered question), task list, files touched, workflow/sheet IDs. Keys are masked.
2. **Guards the expensive moments.** If you send a message into a big conversation whose prompt cache has expired, or switch model mid-conversation, it shows what that costs and what a fresh conversation costs instead. Re-send the same message to proceed anyway.
3. **Starts new conversations warm.** The handoff (~1-2k tokens) is injected when a new conversation opens.
4. **Measures the savings from your own logs** (`/warmstart:savings`), so you do not have to trust anyone's numbers.

## Why

A conversation that grew to hundreds of thousands of tokens is cheap while its cache is warm and very expensive to resume once it has expired (hours later). Opening a new conversation with a compact handoff avoids that.

Measured on the author's own logs (one user, 350 conversations, 8.3k responses; your numbers will differ):

| Cost source | Share of total cost |
|---|---:|
| Reloads after a pause > 1 h | 13.1 % |
| Reloads from switching model mid-conversation | 10.3 % |
| Messages inside conversations > 150k tokens | 31.2 % |

The guards would have avoided ~20 % of cost; closing long conversations with a saved handoff adds up to ~31 % more (an upper bound, not a promise). Cost uses Anthropic's list-price weights (cache read 0.1x, write 1.25x/2x, output 5x) and assumes your quota scales with cost. Run `/warmstart:savings` to see yours.

## Install

Requires Claude Code and Python 3.8+. Works in the VS Code extension and in the terminal CLI (both run the same hooks). The guards that use cache details (resume warning, model-switch warning) need Claude Code 2.1.251 or newer; the auto-save and the prompt guard do not depend on that.

**It fits how most people work:** it does not need any state file. If your project only has a `CLAUDE.md`, Warmstart never edits it (Claude Code already loads it) and never creates `ESTADO.md` or anything else in your project. The only thing it writes there is `.claude/handoff/`. If you keep your own state file (e.g. `ESTADO.md`), `/warmstart:save` may add one line to it, with a backup.

```
/plugin marketplace add addwebtech/claude-warmstart
/plugin install warmstart@warmstart
/warmstart:setup
```

`/warmstart:setup` shows exactly what it will add to `~/.claude/settings.json`, backs it up, and asks first. Then open a **new** conversation.

Without the plugin system: clone this repo and run `python plugins/warmstart/scripts/handoff.py instalar --simular` (preview) then without `--simular`. Remove everything with `desinstalar`; your saved data stays.

## Use

| Command | What it does |
|---|---|
| `/warmstart:save` | Close a session: writes `.claude/handoff/HANDOFF.md` (next action, open question, decisions with why, what didn't work) and verifies it |
| `/warmstart:resume` | In a new conversation: short receipt, then continues from the next action |
| `/warmstart:savings` | Measured cost report from your logs (`--todos` for all projects) |
| `/warmstart:setup` | Install / preview the hooks |

Day to day: finish with `/warmstart:save`; next day open a **new** conversation (do not resume the old one).

## What it does NOT do

- It does not lower the fixed base of every conversation (system prompt, MCP tools, skills, CLAUDE.md: tens of thousands of tokens).
- It does not make a long, warm conversation cheaper while you keep working in it.
- Once a cache has expired, resuming that conversation always costs; Warmstart can only warn you first.
- The automatic summary is mechanical: it cannot capture reasons or decisions that were only spoken. `/warmstart:save` (written by the model) can miss things too.
- It does not keep the cache warm (tools like cache-tax do, at a token cost). It is complementary: if you already run another cache guard, set `"guardiaPrompt": false` in `~/.claude/handoff/config.json` to avoid two guards.
- Claude Code only. Not claude.ai, not other editors' assistants.

## Privacy

Everything stays on your machine; nothing is sent anywhere. Per-project data lives in `.claude/handoff/`, which ships its own `.gitignore` (`*`) so it is never committed by accident. It stores your recent prompts, Claude's last reply, tasks and file names.

Secrets: known key formats (API keys, tokens, JWTs, private-key headers, `user:pass@` URLs) and `password=`/`token:`-style values are masked as `[CLAVE OCULTA]` before anything is written, and `HANDOFF.md` is refused if it still contains a known key format. Masking is pattern-based, so an unusual secret can slip through: do not paste secrets into prompts. Backups of your `settings.json` are kept in `~/.claude/handoff/respaldos/` (owner-only on macOS/Linux), never inside your project. Note that Claude Code itself keeps full transcripts in `~/.claude/projects/`; Warmstart only reads them. The repository's tests scan every file for key patterns and for the developer's username.

## Config

`~/.claude/handoff/config.json` (all optional): `umbralTokens` (default 120000: guards act above this size), `avisoTokens` (200000), `minutosOverride` (5), `guardiaPrompt` / `guardiaModelo` / `inyectarAlInicio` (true), `idioma` (`auto` follows your system language; `es` or `en` forces one; the `WARMSTART_LANG` environment variable overrides it). Hooks never break a session: on any error they exit silently and log to `~/.claude/handoff/errores.log`.

## Status

Pre-release. Tested on Windows: 33 automated tests plus real sessions up to 81 MB, and an end-to-end test against real Claude Code 2.1.312 (19 checks, all passing). The automated tests also pass in CI on macOS, Linux and Windows (Python 3.8, 3.10 and 3.12); the end-to-end test has only been run on Windows. Not yet verified on older Claude Code (e.g. 2.1.58) or live in the interactive UI. Messages shown to you are in English or Spanish (automatic). The internal reports the model reads (`preparar`, `verificar`, `ahorro`) are still Spanish only; the skills are in English.

Starting a new conversation needs no command: the handoff is already in context. Verified: a specific question about the previous session is answered correctly. **Not guaranteed:** a vague greeting like "hi" resuming on its own. In the author's 3 test runs it did so once; the other times Claude went looking for a state file instead, probably because the author's own global `CLAUDE.md` asks for that (not proven). Ask for what you want instead.

Run the end-to-end test yourself from a normal terminal (not inside a Claude Code session; it costs a few cents of quota): `python tests/e2e_claude.py` (or `--claude PATH`, `--model haiku`). It builds a throwaway project with only a `CLAUDE.md`, installs the hooks there only, walks the whole flow, and checks that no secret or personal file leaks.

## License

MIT

Warmstart is an independent community project. It is not made, endorsed or supported by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic, used here only to say what the plugin works with.
