"""
tr1ckhouse_roster.py - publish live QL roster to central registry

Snapshots the server's per-player state on game/team events and POSTs the
JSON to a central tr1ckhouse registry over HTTPS. A heartbeat re-publishes
every ~60s so the registry's TTL never expires the entry during quiet play.

The registry stores snapshots in memory keyed by "ip:port" and serves them
to the discord-gamestatus bot.

Setup on a new server:
    1. Drop this file into your minqlx plugins folder.
    2. In server.cfg or console:
         set qlx_tr1ckhouseUrl "https://tr1ckhouse.net/roster"
         set qlx_tr1ckhouseKey "<your shared secret>"
    3. !load tr1ckhouse_roster

To request a shared secret for the Tr1ckHouse-run registry, join
https://discord.gg/8sjDdcz and message mobi.

Player country flags:
    Each player's IP is resolved to a two-letter country code (ip-api.com,
    batched, cached per IP) and only the code is published -- IPs never leave
    the server towards the registry. Disable with:
         set qlx_tr1ckhouseCountry "0"

See https://github.com/papamobi/tr1ckhouse-minqlx-plugins/tree/main/tr1ckhouse_roster
for full documentation.
"""

import ipaddress
import json
import threading
import time
import urllib.request
import urllib.error

import minqlx


GEO_BATCH_URL = "http://ip-api.com/batch?fields=status,countryCode,query"
GEO_BATCH_MAX = 100          # ip-api batch limit
GEO_RETRY_SECONDS = 600      # retry failed lookups after 10 min
GEO_CACHE_MAX = 4096         # bound memory on long-running servers


class tr1ckhouse_roster(minqlx.Plugin):

    def __init__(self):
        super().__init__()

        self.set_cvar_once("qlx_tr1ckhouseUrl", "https://tr1ckhouse.net/roster")
        self.set_cvar_once("qlx_tr1ckhouseKey", "")
        self.set_cvar_once("qlx_tr1ckhouseHeartbeat", "60")
        self.set_cvar_once("qlx_tr1ckhousePublicIp", "")  # optional override
        self.set_cvar_once("qlx_tr1ckhouseCountry", "1")

        self.url = self.get_cvar("qlx_tr1ckhouseUrl", str)
        self.key = self.get_cvar("qlx_tr1ckhouseKey", str)
        self.public_ip_override = self.get_cvar("qlx_tr1ckhousePublicIp", str)
        self.country_enabled = self.get_cvar("qlx_tr1ckhouseCountry", bool)

        # ip -> (country_code, resolved_at). "" code = lookup failed/unknown.
        self._geo_cache = {}
        self._geo_pending = set()
        self._geo_lock = threading.Lock()
        self.heartbeat_interval = (
            self.get_cvar("qlx_tr1ckhouseHeartbeat", int) or 60
        )

        if not self.key:
            self.logger.warning(
                "qlx_tr1ckhouseKey is empty — publishes will be rejected (401)"
            )

        self.add_hook("team_switch", self.on_event)
        self.add_hook("player_connect", self.on_event)
        self.add_hook("player_disconnect", self.on_event)
        self.add_hook("player_loaded", self.on_event)
        self.add_hook("round_end", self.on_event)
        self.add_hook("game_start", self.on_event)
        self.add_hook("game_end", self.on_event)
        self.add_hook("map", self.on_map_change)
        self.add_hook("unload", self.on_unload)

        self.add_command("roster", self.cmd_roster, permission=5)

        self._stop = threading.Event()
        # Debounce state: event-driven publishes (from team_switch, round_end, etc.)
        # are throttled so a burst of hot-hook events (e.g. during a shuffle/rebalance
        # or in rapid-fire round_end sequences) collapses into a single publish.
        # The heartbeat is unaffected and still fires on its own interval, so any
        # events that get suppressed here get picked up within one heartbeat cycle.
        self._last_publish_monotonic = 0.0
        self._publish_debounce_seconds = 2.0
        self._publish_lock = threading.Lock()

        self._start_heartbeat()

        self.logger.info(
            f"tr1ckhouse_roster loaded (url={self.url}, "
            f"heartbeat={self.heartbeat_interval}s)"
        )
        self.publish()

    # --- hooks ---

    def on_event(self, *args, **kwargs):
        self._publish_debounced()

    def on_map_change(self, mapname, factory):
        # mapname/factory cvars aren't fully updated until after the map load
        # completes and new_game fires. Delay a few seconds so the snapshot
        # reflects the new state rather than the previous map's.
        @minqlx.delay(3)
        def delayed():
            self._publish_debounced()
        delayed()

    def _publish_debounced(self):
        """Publish immediately if it's been >= debounce interval since the last
        publish; otherwise skip. The heartbeat is unaffected and will publish
        on its own schedule regardless."""
        now = time.monotonic()
        with self._publish_lock:
            if now - self._last_publish_monotonic < self._publish_debounce_seconds:
                return
            self._last_publish_monotonic = now
        self.publish()

    def on_unload(self, plugin):
        if plugin == self.__class__.__name__:
            self._stop.set()
            self.logger.info("tr1ckhouse_roster unloaded, heartbeat stopped")

    # --- admin command ---

    def cmd_roster(self, player, msg, channel):
        snapshot = self.build_snapshot()
        self.logger.info(f"Current roster: {json.dumps(snapshot, indent=2)}")
        channel.reply(
            f"^7Roster: ^1{len(snapshot['teams']['red'])}R ^4{len(snapshot['teams']['blue'])}B "
            f"^7specs:{len(snapshot['teams']['spectator'])} free:{len(snapshot['teams']['free'])}"
        )

    # --- heartbeat ---

    def _start_heartbeat(self):
        def beat():
            while not self._stop.wait(self.heartbeat_interval):
                try:
                    self.publish()
                except Exception:
                    self.logger.warning(
                        "tr1ckhouse_roster heartbeat failed", exc_info=True
                    )
        t = threading.Thread(
            target=beat, name="tr1ckhouse_roster_heartbeat", daemon=True
        )
        t.start()

    # --- publishing ---

    @minqlx.next_frame
    def publish(self):
        """Build snapshot on main thread (player attrs are frame-safe),
        then hand to HTTP writer thread."""
        try:
            snapshot = self.build_snapshot()
        except Exception:
            self.logger.warning("build_snapshot failed", exc_info=True)
            return
        self.post_snapshot(snapshot)

    @minqlx.thread
    def post_snapshot(self, snapshot):
        payload = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Tr1ckhouse-Key": self.key or "",
                "User-Agent": "tr1ckhouse_roster/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.getcode()
                if code != 200:
                    self.logger.warning(
                        f"registry returned status {code} for POST {self.url}"
                    )
        except urllib.error.HTTPError as e:
            self.logger.warning(
                f"registry rejected POST: HTTP {e.code} ({e.reason})"
            )
        except urllib.error.URLError as e:
            # Network issue, DNS failure, connection refused, timeout.
            # Silent-on-transient: next heartbeat will retry.
            self.logger.debug(f"registry unreachable: {e.reason}")
        except Exception:
            self.logger.warning("unexpected POST failure", exc_info=True)

    # --- country lookup ---

    def _country_for(self, ip, missing):
        """Called on the game thread from build_snapshot. Returns the cached
        country code (or "") and collects IPs that still need a lookup."""
        if not self.country_enabled or not ip:
            return ""
        now = time.time()
        with self._geo_lock:
            cached = self._geo_cache.get(ip)
            if cached is not None:
                code, at = cached
                if code or now - at < GEO_RETRY_SECONDS:
                    return code
            if ip in self._geo_pending:
                return ""
        try:
            if not ipaddress.ip_address(ip).is_global:
                return ""          # LAN, loopback, bots
        except ValueError:
            return ""
        missing.add(ip)
        return ""

    def _request_countries(self, ips):
        with self._geo_lock:
            ips = [ip for ip in ips if ip not in self._geo_pending][:GEO_BATCH_MAX]
            self._geo_pending.update(ips)
        if ips:
            self._lookup_countries(ips)

    @minqlx.thread
    def _lookup_countries(self, ips):
        """Background thread: one batched HTTP call, no engine access."""
        results = {}
        try:
            req = urllib.request.Request(
                GEO_BATCH_URL,
                data=json.dumps(ips).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "tr1ckhouse_roster/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                for row in json.loads(resp.read().decode("utf-8")):
                    if row.get("status") == "success":
                        code = str(row.get("countryCode") or "")
                        if len(code) == 2 and code.isalpha():
                            results[row.get("query")] = code.upper()
        except Exception as e:
            self.logger.debug(f"country lookup failed: {e}")

        now = time.time()
        with self._geo_lock:
            if len(self._geo_cache) > GEO_CACHE_MAX:
                self._geo_cache.clear()
            for ip in ips:
                self._geo_cache[ip] = (results.get(ip, ""), now)
                self._geo_pending.discard(ip)

        # Push the new flags out without waiting for the heartbeat.
        # publish() is @next_frame, so this is safe from a thread.
        if results:
            self._publish_debounced()

    # --- snapshot ---

    def build_snapshot(self):
        teams = {"red": [], "blue": [], "spectator": [], "free": []}
        missing_geo = set()

        for p in self.players():
            team = p.team
            if team not in teams:
                continue

            stats = p.stats if hasattr(p, "stats") and p.stats else None
            kills = int(stats.kills) if stats and hasattr(stats, "kills") else 0
            deaths = int(stats.deaths) if stats and hasattr(stats, "deaths") else 0
            damage = (
                int(stats.damage_dealt)
                if stats and hasattr(stats, "damage_dealt")
                else 0
            )

            teams[team].append({
                "steam_id": str(p.steam_id),
                "name": p.clean_name,
                "score": int(p.score) if p.score is not None else 0,
                "kills": kills,
                "deaths": deaths,
                "damage": damage,
                "ping": int(p.ping) if p.ping is not None else 0,
                "country": self._country_for(getattr(p, "ip", ""), missing_geo),
            })

        if missing_geo:
            self._request_countries(missing_geo)

        # Team scores: read from self.game (QL-native), not g_redScore cvars
        # which don't exist as persistent cvars in QL.
        try:
            game = self.game
            red_score = int(game.red_score) if game and game.red_score is not None else 0
            blue_score = int(game.blue_score) if game and game.blue_score is not None else 0
        except Exception:
            red_score = 0
            blue_score = 0

        try:
            instagib = int(minqlx.get_cvar("g_instagib") or 0)
        except (ValueError, TypeError):
            instagib = 0

        try:
            net_port = int(minqlx.get_cvar("net_port") or "0")
        except (ValueError, TypeError):
            net_port = 0

        # Public IP resolution: prefer the operator-set override (for NAT
        # servers where net_ip is internal), otherwise trust net_ip. The
        # registry falls back to the request's source IP as last resort.
        public_ip = self.public_ip_override or minqlx.get_cvar("net_ip") or ""

        return {
            "net_ip": public_ip,
            "net_port": net_port,
            "hostname": minqlx.get_cvar("sv_hostname") or "",
            "gametype": minqlx.get_cvar("g_gametype") or "0",
            "instagib": instagib,
            "map": minqlx.get_cvar("mapname") or "",
            "score_red": red_score,
            "score_blue": blue_score,
            "teams": teams,
            "updated": int(time.time()),
        }