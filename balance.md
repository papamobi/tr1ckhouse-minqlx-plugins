# balance.py — Tr1ckHouse fork

A replacement of the original `balance.py` shipped with minqlx. All upstream cvars and commands work identically.

## What has been improved/changed

- **`qlx_balanceForceSwapDiff`** (optional) — auto-swap two players when team ELO difference is above the configured value. Add `set qlx_balanceForceSwapDiff "<elo_diff>"` to `server.cfg` (e.g. `"125"`) to enable. Omitted/empty/`"0"` = feature disabled.
- **Auto-fetch elo on player connect** (`handle_player_connect`).
- **Fresh balance suggestion after every team change** (`handle_team_switch`) — when someone joins from spec or moves teams mid-game, `!teams` automatically re-runs and posts an updated rating breakdown and swap suggestion. The original balance.py requires manually typing `!teams` to see the new state.
- **Swap timing moved to round end** (`handle_round_end`) — agreed swaps execute the moment the round ends, rather than at round countdown. Some gametypes (e.g. Instagib Freeze Tag) don't have a round countdown at all, so swap-at-countdown logic fires late or not as expected; moving execution to `round_end` ensures the swap happens reliably between rounds regardless of countdown.
- **5-second cooldown on `!teams`** to prevent spam.
- **Non-round based gametype fix (TDM, CTF, DOM)** — `!agree`, `/callvote do` (only if doVote plugin is enabled), and auto-swap execute immediately. Original balance.py queues them for a `round_end` event that doesn't fire mid-game on these gametypes, meaning swaps never happen.
- **Built-in factory API auto-reset** — built-in QL factories can't be edited and never set `qlx_balanceApi`, so a previous custom factory's `elo_b` setting would leak across factory switches. Now `qlx_balanceApi` is force-reset on `new_game`:
  - `duel, tdm, ca, ft, ctf, ad, ffa` → forced to `elo`
  - `ictf, ift` → forced to `elo_b`
  - Custom factories → left untouched, the `.factories` file's setting is honored (this was already the case in the original balance.py via `cache_cvars()`, this fork preserves it and you need to add `qlx_balanceapi` to your custom `.factories` file for this to work correctly).
- **Helpers for other plugins**: `get_player_elo`, `get_team_averages`, `callback_fetch_player_elo` (used by `queue.py`, etc.).
- **Module-level docstring** documenting all `server.cfg`-relevant cvars.

## Compatibility

- All original balance.py cvars (`qlx_balanceUseLocal`, `qlx_balanceUrl`, `qlx_balanceApi`, `qlx_balanceAuto`, `qlx_balanceMinimumSuggestionDiff`) behave identically.
- All original balance.py commands (`!getrating`, `!setrating`, `!balance`, `!teams`, `!do`, `!agree`, `!ratings`, etc.) behave identically on round-based gametypes (FT, CA, AD).
- On TDM/CTF/DOM, `!agree` or `!a` now executes immediately rather than queueing forever — this is more like a bug fix.

## License

GPLv3 — same as the upstream `balance.py` this is forked from. See the license header at the top of the source file.

## Contributors

- [MadHypnofrog (Vlad Kurilenko)](https://github.com/MadHypnofrog)
- Codenames Instagib FreezeTag
