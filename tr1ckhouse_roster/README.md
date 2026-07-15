# tr1ckhouse_roster

minqlx plugin that publishes live server roster to the
[GameStatus Support Discord bot](https://github.com/papamobi/discord-gamestatus/tree/tr1ckhouse).

Sends server state (players, team scores, K/D, damage, captures) to a central
HTTPS registry every ~60s. The Discord bot reads from that registry to render
enriched embeds with per-team columns.

## Install

1. Copy `tr1ckhouse_roster.py` to your minqlx plugins folder.
2. Add to `server.cfg`:
set qlx_tr1ckhouseUrl "https://tr1ckhouse.net/roster"
set qlx_tr1ckhouseKey "<your key>"
3. `!load tr1ckhouse_roster`

## Getting a key

Reach out to me by discord username `f.mobile` or join the [GameStatus Support Discord](https://discord.gg/VMySf6wtaS) and message `mobi`.

## What it collects

Per-player: Steam ID, name, score, kills, deaths, damage, ping.
Per-server: hostname, gametype, map, team scores. In-memory only, 180s TTL,
not persisted. See [privacy policy](https://tr1ckhouse.net/gamestatus/privacy.html).
