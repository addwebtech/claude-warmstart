# Warmstart (plugin)

Warmstart starts new Claude Code conversations warm and stops you from paying to reload old ones. It auto-saves a handoff of each session without using model tokens, warns you before a message or a model switch would re-send a huge conversation whose prompt cache has expired, injects the handoff when a new conversation opens, and measures the savings from your own logs.

Skills: `/warmstart:setup` (install the hooks, with a preview and a backup), `/warmstart:save`, `/warmstart:resume`, `/warmstart:savings`. Requires Python 3.8+. See the repository README for details, limits and privacy.
