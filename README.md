# tr1ckhouse-minqlx-plugins

Tr1ckHouse community fork of selected plugins for [minqlx](https://github.com/MinoMino/minqlx).

## Plugins

| Plugin | Description |
|---|---|
| [`balance.py`](balance.py) | Team-balance plugin with auto-swap, factory-aware ELO/ELO_B switching, and continuous-gametype fixes. See [`balance.md`](balance.md) for the full list of changes vs upstream. |
| [`tr1ckhouse_roster/`](tr1ckhouse_roster/) | Publishes live roster (players, K/D, damage, captures) to the [Tr1ckHouse Discord bot](https://github.com/papamobi/discord-gamestatus/tree/tr1ckhouse). Requires an API key. |

## Installation

Drop the plugin file into your `qlds/minqlx-plugins/` directory alongside your other plugins, then add it to your `qlx_plugins` list in `server.cfg` and restart (or `!load <plugin>` from the in-game console).

Each plugin's licensing follows the original author's terms (typically GPLv3 — see headers within each file).
