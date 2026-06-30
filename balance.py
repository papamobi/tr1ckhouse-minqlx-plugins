# minqlx - A Quake Live server administrator bot.
# Copyright (C) 2015 Mino <mino@minomino.org>

# This file is part of minqlx.

# minqlx is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# minqlx is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with minqlx. If not, see <http://www.gnu.org/licenses/>.

"""
Configuration cvars (set these in server.cfg):

    qlx_balanceUseLocal            (default "1")
        "1" = use cached local ratings first and only fetch from the API
        when missing; "0" = always go to the API. Caching reduces load on
        the rating server and is fine for typical use.

    qlx_balanceUrl                 (default "qlstats.net")
        The hostname of the rating-server API. Used together with
        qlx_balanceApi to build the URL the plugin fetches ratings from.

    qlx_balanceApi                 (default "elo")
        The rating endpoint to use on the rating server. Common values:
        "elo", "elo_b" (B-rating variant). Combined with qlx_balanceUrl
        as http://<url>/<api>/ for HTTP fetches.

    qlx_balanceMinimumSuggestionDiff   (default "25")
        Minimum team-ELO difference below which the plugin will not
        suggest a swap at all. Below this threshold, !teams just reports
        the ratings without a swap recommendation. Acts as a noise floor
        so trivial imbalances don't generate suggestions.

    qlx_balanceForceSwapDiff       (no default -- opt-in)
        ELO difference at or above which a swap is performed
        automatically, without players needing to !agree or call /callvote
        do. UNSET / empty / "0" = feature disabled (default behavior:
        regular suggestion flow). Set this in server.cfg to e.g. "125" to
        auto-swap whenever teams differ by 125+ ELO.

        On round-based gametypes (AD, CA, FT) the auto-swap queues for
        the next round_end so it happens between rounds. On continuous
        gametypes (TDM, CTF, DOM) it executes immediately, since those
        gametypes have no mid-game round boundary to wait for.

    qlx_balanceForceSwapDiff is intentionally not registered with a
    default value so a server admin who hasn't deliberately enabled the
    feature never sees automatic swaps. Add the line to server.cfg to
    opt in.
"""

import minqlx
import requests
import itertools
import threading
import random
import time

RATING_KEY = "minqlx:players:{0}:ratings:{1}" # 0 == steam_id, 1 == short gametype.
MAX_ATTEMPTS = 3
CACHE_EXPIRE = 60*10 # 10 minutes TTL.
DEFAULT_RATING = 1500
UNTRACKED_RATING = 9999
TEAMS_CALL_COOLDOWN = 5 # can't call !teams more frequently than once in 5 seconds
SUPPORTED_GAMETYPES = ("ad", "ca", "ctf", "dom", "ft", "tdm")
# Externally supported game types. Used by !getrating for game types the API works with.
EXT_SUPPORTED_GAMETYPES = ("ad", "ca", "ctf", "dom", "ft", "tdm", "duel", "ffa")
# Round-based gametypes have a natural "next round" boundary that switches can
# be deferred to. Continuous gametypes (ctf, dom, tdm) do not -- their only
# "round end" is the end of the game itself, so deferring a player switch on
# those gametypes effectively means "wait until the game finishes." cmd_agree
# uses this to decide whether to defer or execute immediately.
ROUND_BASED_GAMETYPES = ("ad", "ca", "ft")

# Built-in QL factories that need force-reset of qlx_balanceApi to "elo".
#
# Why this exists: factory cvars persist across factory switches. Built-in
# id Software factories cannot be edited and never set qlx_balanceApi
# themselves. So if a previous custom factory set the cvar to "elo_b" and
# the engine then switches to one of these built-ins, the cvar stays as
# "elo_b" and balance.py would fetch ratings from the wrong API.
#
# handle_new_game force-resets qlx_balanceApi based on the current built-in
# factory. Custom factories are left untouched -- their own .factories file
# sets qlx_balanceApi explicitly and the existing cache_cvars() flow handles
# them correctly.
DEFAULT_ELO_FACTORIES = frozenset((
    "ad", "ffa", "ca", "ft", "tdm", "duel", "ctf",
))
# Built-in factories that use the B-rating API (Instagib variants).
DEFAULT_ELO_B_FACTORIES = frozenset((
    "ictf", "ift",
))


class balance(minqlx.Plugin):
    def __init__(self):
        self.add_hook("round_countdown", self.handle_round_countdown)
        self.add_hook("round_start", self.handle_round_start)
        self.add_hook("round_end", self.handle_round_end)
        self.add_hook("vote_ended", self.handle_vote_ended)
        self.add_hook("player_connect", self.handle_player_connect)
        self.add_hook("player_disconnect", self.handle_player_disconnect)
        self.add_hook("new_game", self.handle_new_game)
        self.add_hook("team_switch", self.handle_team_switch)
        self.add_command(("setrating", "setelo"), self.cmd_setrating, 3, usage="<id> <rating>")
        self.add_command(("getrating", "getelo", "elo"), self.cmd_getrating, usage="<id> [gametype]")
        self.add_command(("remrating", "remelo"), self.cmd_remrating, 3, usage="<id>")
        self.add_command("balance", self.cmd_balance, 1)
        self.add_command(("teams", "teens"), self.cmd_teams)
        self.add_command("do", self.cmd_do, 1)
        self.add_command(("agree", "a"), self.cmd_agree, client_cmd_perm=0)
        self.add_command(("ratings", "elos", "selo"), self.cmd_ratings)

        self.ratings_lock = threading.RLock()
        self.teams_lock = threading.RLock()
        # Keys: steam_id - Items: {"ffa": {"elo": 123, "games": 321, "local": False}, ...}
        self.ratings = {}
        # Keys: steam_id - Items: {"deactivated": true/false, "ratings": {...}, "allowRating": true/false, "privacy": "public/private/anonymous/untracked"}
        self.player_info = {}
        # Keys: request_id - Items: (players, callback, channel)
        self.requests = {}
        self.request_counter = itertools.count()
        self.suggested_pair = None
        self.suggested_agree = [False, False]
        self.in_countdown = False
        # Tracked across threads via self.teams_lock at the read/write site
        # in cmd_teams. Initialized to 0 so the first call always passes the
        # cooldown check (any current timestamp - 0 >> TEAMS_CALL_COOLDOWN).
        self.last_teams_call_timestamp = 0

        self.set_cvar_once("qlx_balanceUseLocal", "1")
        self.set_cvar_once("qlx_balanceUrl", "qlstats.net")
        self.set_cvar_once("qlx_balanceAuto", "1")
        self.set_cvar_once("qlx_balanceMinimumSuggestionDiff", "25")
        # qlx_balanceForceSwapDiff is intentionally NOT registered with a
        # default here. The auto-swap feature is opt-in: leave this cvar
        # unset in server.cfg and no auto-swap will ever trigger (the
        # comparison site treats missing/None/<=0 identically as disabled).
        # To enable, set qlx_balanceForceSwapDiff "<elo_diff>" in server.cfg
        # (e.g. "125" to auto-swap when teams differ by >= 125 elo).
        self.set_cvar_once("qlx_balanceApi", "elo")

        self.cache_cvars()

    def cache_cvars(self):
        # Store some cvar values that are used in non-game threads
        self.use_local = self.get_cvar("qlx_balanceUseLocal", bool)
        self.api_url = "http://{}/{}/".format(self.get_cvar("qlx_balanceUrl"), self.get_cvar("qlx_balanceApi"))

    def handle_round_countdown(self, *args, **kwargs):
        # No swap is done here -- this server doesn't have round countdowns
        # enabled, so the swap happens in handle_round_end instead. If
        # countdowns are ever re-enabled, this is where the swap should go
        # (using @minqlx.next_frame to avoid clobbering the countdown sound
        # and text, per an old comment about that behavior).
        self.in_countdown = True

    def handle_round_start(self, *args, **kwargs):
        self.in_countdown = False

    # do the swap here - seems like the round start event is delayed by ~4 seconds; doing this in round end
    # should have the expected behaviour of immediately swapping people after round starts
    def handle_round_end(self, *args, **kwargs):
        players = self.teams()
        if all(self.suggested_agree) and len(players["red"]) == len(players["blue"]):
            # don't wait because we don't have the countdown
            self.execute_suggestion()

    def handle_vote_ended(self, votes, vote, args, passed):
        if passed == True and vote == "shuffle" and self.get_cvar("qlx_balanceAuto", bool):
            gt = self.game.type_short
            if gt not in SUPPORTED_GAMETYPES:
                return

            @minqlx.delay(3.5)
            def f():
                players = self.teams()
                if len(players["red"] + players["blue"]) % 2 != 0:
                    self.msg("Teams were ^6NOT^7 balanced due to the total number of players being an odd number.")
                    return

                players = dict([(p.steam_id, gt) for p in players["red"] + players["blue"]])
                self.add_request(players, self.callback_balance, minqlx.CHAT_CHANNEL)
            f()

    # load player elo on connect immediately
    def handle_player_connect(self, player):
        self.add_request({ player.steam_id: self.game.type_short }, self.callback_fetch_player_elo, minqlx.CHAT_CHANNEL)

    def handle_player_disconnect(self, player, reason):
        self.clean_player_data(player)

    def handle_new_game(self):
        # Built-in QL factories can't be edited and never set qlx_balanceApi.
        # If a previous custom factory set it (e.g. to "elo_b") and the engine
        # then switches to a built-in, the cvar would persist and balance.py
        # would fetch from the wrong API. Force-reset based on the current
        # built-in factory's expected rating type. Custom factories are left
        # untouched -- their .factories file sets qlx_balanceApi explicitly
        # and the existing cache_cvars() flow handles them correctly.
        # Must run BEFORE cache_cvars() so api_url reflects the corrected value.
        if self.game.factory in DEFAULT_ELO_FACTORIES:
            self.set_cvar("qlx_balanceApi", "elo")
        elif self.game.factory in DEFAULT_ELO_B_FACTORIES:
            self.set_cvar("qlx_balanceApi", "elo_b")

        self.cache_cvars()
        gt = self.game.type_short

        # reset ratings cache on start and load elos for all players
        if self.game.state == "warmup":
            with self.ratings_lock:
                self.ratings = {}
                players = dict([(p.steam_id, gt) for p in self.players()])
                self.add_request(players, self.callback_fetch_player_elo, minqlx.CHAT_CHANNEL)

    # check balance for even teams after switches
    def handle_team_switch(self, player, old, new):
        # only check during the actual game
        if self.game.state != "in_progress":
            return
        teams = self.teams()
        if (len(teams["red"]) != len(teams["blue"])):
            return
        gt = self.game.type_short
        if new in ['red', 'blue', 'spectator']:
            players = dict([(p.steam_id, gt) for p in teams["red"] + teams["blue"]])
            self.add_request(players, self.callback_teams, minqlx.CHAT_CHANNEL)

    @minqlx.thread
    def clean_player_data(self, player):
        for p in self.players().copy():
            if p.steam_id == player.steam_id and p.id != player.id:
                # there is a second client with same steam id
                return

        if player.steam_id in self.player_info:
            del self.player_info[player.steam_id]

        with self.ratings_lock:
            if player.steam_id in self.ratings:
                del self.ratings[player.steam_id]

    @minqlx.thread
    def fetch_ratings(self, players, request_id):
        if not players:
            return

        # We don't want to modify the actual dict, so we use a copy.
        players = players.copy()

        # Get local ratings if present in DB.
        if self.use_local:
            for steam_id in players.copy():
                gt = players[steam_id]
                key = RATING_KEY.format(steam_id, gt)
                if key in self.db:
                    with self.ratings_lock:
                        if steam_id in self.ratings:
                            self.ratings[steam_id][gt] = {"games": -1, "elo": int(self.db[key]), "local": True, "time": -1}
                        else:
                            self.ratings[steam_id] = {gt: {"games": -1, "elo": int(self.db[key]), "local": True, "time": -1}}
                    del players[steam_id]

            if not players:
                self.handle_ratings_fetched(request_id, requests.codes.ok)
                return

        attempts = 0
        last_status = 0
        untracked_sids = []

        while attempts < MAX_ATTEMPTS:
            attempts += 1
            url = self.api_url + "+".join([str(sid) for sid in players])
            res = requests.get(url, headers={"X-QuakeLive-Map": self.game.map})
            last_status = res.status_code
            if res.status_code != requests.codes.ok:
                continue

            js = res.json()
            if "players" not in js:
                last_status = -1
                continue

            # Fill our ratings dict with the ratings we just got.
            for p in js["players"]:
                sid = int(p["steamid"])
                del p["steamid"]
                t = time.time()

                with self.ratings_lock:
                    if sid not in self.ratings:
                        self.ratings[sid] = {}

                    for gt in p:
                        p[gt]["time"] = t
                        p[gt]["local"] = False
                        self.ratings[sid][gt] = p[gt]
                        if self.ratings[sid][gt]["elo"] == 0 and self.ratings[sid][gt]["games"] == 0:
                            self.ratings[sid][gt]["elo"] = DEFAULT_RATING

                        if sid in players and gt == players[sid]:
                            # The API gave us the game type we wanted, so we remove it.
                            del players[sid]

                    # Fill the rest of the game types the API didn't return but supports.
                    for gt in SUPPORTED_GAMETYPES:
                        if gt not in self.ratings[sid]:
                            self.ratings[sid][gt] = {"games": -1, "elo": DEFAULT_RATING, "local": False, "time": time.time()}

            # If the API didn't return all the players, we set them to the default rating.
            for sid in players:
                with self.ratings_lock:
                    if sid not in self.ratings:
                        self.ratings[sid] = {}
                    self.ratings[sid][players[sid]] = {"games": -1, "elo": DEFAULT_RATING, "local": False, "time": time.time()}

            # Setting ratings for untracked players.
            if "untracked" in js:
                untracked_sids = list(map( lambda sid: int(sid), js["untracked"]))

            for gt in SUPPORTED_GAMETYPES:
                for sid in untracked_sids:
                  with self.ratings_lock:
                      if sid not in self.ratings:
                          self.ratings[sid] = {}
                      self.ratings[sid][gt] = {"games": -1, "elo": UNTRACKED_RATING, "local": False, "time": time.time()}

            # Saving player info
            try:
                for player, data in js["playerinfo"].items():
                    sid = int(player)
                    self.player_info[sid] = js["playerinfo"][player]
                    self.player_info[sid]["time"] = time.time()
            except KeyError:
                pass

            break

        if attempts == MAX_ATTEMPTS:
            self.handle_ratings_fetched(request_id, last_status)
            return

        self.handle_ratings_fetched(request_id, requests.codes.ok)

    @minqlx.next_frame
    def handle_ratings_fetched(self, request_id, status_code):
        players, callback, channel, args = self.requests[request_id]
        del self.requests[request_id]
        if status_code != requests.codes.ok:
            # TODO: Put a couple of known errors here for more detailed feedback.
            channel.reply("ERROR {}: Failed to fetch ratings.".format(status_code))
        else:
            callback(players, channel, *args)

    def add_request(self, players, callback, channel, *args):
        req = next(self.request_counter)
        self.requests[req] = players.copy(), callback, channel, args

        # Only start a new thread if we need to make an API request.
        if self.remove_cached(players):
            self.fetch_ratings(players, req)
        else:
            # All players were cached, so we tell it to go ahead and call the callbacks.
            self.handle_ratings_fetched(req, requests.codes.ok)

    def remove_cached(self, players):
        with self.ratings_lock:
            for sid in players.copy():
                gt = players[sid]
                if sid in self.ratings and gt in self.ratings[sid]:
                    t = self.ratings[sid][gt]["time"]
                    if t == -1 or time.time() < t + CACHE_EXPIRE:
                        del players[sid]

        return players

    def cmd_getrating(self, player, msg, channel):
        if len(msg) == 1:
            sid = player.steam_id
        else:
            try:
                sid = int(msg[1])
                target_player = None
                if 0 <= sid < 64:
                    target_player = self.player(sid)
                    sid = target_player.steam_id
            except ValueError:
                player.tell("Invalid ID. Use either a client ID or a SteamID64.")
                return minqlx.RET_STOP_ALL
            except minqlx.NonexistentPlayerError:
                player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
                return minqlx.RET_STOP_ALL

        if len(msg) > 2:
            if msg[2].lower() in EXT_SUPPORTED_GAMETYPES:
                gt = msg[2].lower()
            else:
                player.tell("Invalid gametype. Supported gametypes: {}"
                    .format(", ".join(EXT_SUPPORTED_GAMETYPES)))
                return minqlx.RET_STOP_ALL
        else:
            gt = self.game.type_short
            if gt not in EXT_SUPPORTED_GAMETYPES:
                player.tell("This game mode is not supported by the balance plugin.")
                return minqlx.RET_STOP_ALL

        self.add_request({sid: gt}, self.callback_getrating, channel, gt)

    def callback_getrating(self, players, channel, gametype):
        sid = next(iter(players))
        player = self.player(sid)
        if player:
            name = player.name
        else:
            name = sid

        channel.reply("{} has a rating of ^6{}^7 in {}.".format(name, self.ratings[sid][gametype]["elo"], gametype.upper()))

    def cmd_setrating(self, player, msg, channel):
        if len(msg) < 3:
            return minqlx.RET_USAGE

        try:
            sid = int(msg[1])
            target_player = None
            if 0 <= sid < 64:
                target_player = self.player(sid)
                sid = target_player.steam_id
        except ValueError:
            player.tell("Invalid ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL
        except minqlx.NonexistentPlayerError:
            player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL

        try:
            rating = int(msg[2])
        except ValueError:
            player.tell("Invalid rating.")
            return minqlx.RET_STOP_ALL

        if target_player:
            name = target_player.name
        else:
            name = sid

        gt = self.game.type_short
        self.db[RATING_KEY.format(sid, gt)] = rating

        # If we have the player cached, set the rating.
        with self.ratings_lock:
            if sid in self.ratings and gt in self.ratings[sid]:
                self.ratings[sid][gt]["elo"] = rating
                self.ratings[sid][gt]["local"] = True
                self.ratings[sid][gt]["time"] = -1

        channel.reply("{}'s {} rating has been set to ^6{}^7.".format(name, gt.upper(), rating))

    def cmd_remrating(self, player, msg, channel):
        if len(msg) < 2:
            return minqlx.RET_USAGE

        try:
            sid = int(msg[1])
            target_player = None
            if 0 <= sid < 64:
                target_player = self.player(sid)
                sid = target_player.steam_id
        except ValueError:
            player.tell("Invalid ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL
        except minqlx.NonexistentPlayerError:
            player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL

        if target_player:
            name = target_player.name
        else:
            name = sid

        gt = self.game.type_short
        del self.db[RATING_KEY.format(sid, gt)]

        # If we have the player cached, remove the game type.
        with self.ratings_lock:
            if sid in self.ratings and gt in self.ratings[sid]:
                del self.ratings[sid][gt]

        channel.reply("{}'s locally set {} rating has been deleted.".format(name, gt.upper()))

    def cmd_balance(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        teams = self.teams()
        if len(teams["red"] + teams["blue"]) % 2 != 0:
            player.tell("The total number of players should be an even number.")
            return minqlx.RET_STOP_ALL

        players = dict([(p.steam_id, gt) for p in teams["red"] + teams["blue"]])
        self.add_request(players, self.callback_balance, minqlx.CHAT_CHANNEL)

    def callback_balance(self, players, channel):
        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = teams["red"] + teams["blue"]
        gt = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = dict([(p.steam_id, gt) for p in current])
                self.add_request(d, self.callback_balance, channel)
                return

        # Start out by evening out the number of players on each team.
        diff = len(teams["red"]) - len(teams["blue"])
        if abs(diff) > 1:
            if diff > 0:
                for i in range(diff - 1):
                    p = teams["red"].pop()
                    p.put("blue")
                    teams["blue"].append(p)
            elif diff < 0:
                for i in range(abs(diff) - 1):
                    p = teams["blue"].pop()
                    p.put("red")
                    teams["red"].append(p)

        # Start shuffling by looping through our suggestion function until
        # there are no more switches that can be done to improve teams.
        switch = self.suggest_switch(teams, gt)
        if switch:
            while switch:
                p1 = switch[0][0]
                p2 = switch[0][1]
                self.switch(p1, p2)
                teams["blue"].append(p1)
                teams["red"].append(p2)
                teams["blue"].remove(p2)
                teams["red"].remove(p1)
                switch = self.suggest_switch(teams, gt)
            avg_red = self.team_average(teams["red"], gt)
            avg_blue = self.team_average(teams["blue"], gt)
            diff_rounded = abs(round(avg_red) - round(avg_blue)) # Round individual averages.
            if round(avg_red) > round(avg_blue):
                self.msg("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^1{}"
                    .format(round(avg_red), round(avg_blue), diff_rounded))
            elif round(avg_red) < round(avg_blue):
                self.msg("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^4{}"
                    .format(round(avg_red), round(avg_blue), diff_rounded))
            else:
                self.msg("^1{} ^7vs ^4{}^7 - Holy shit!"
                    .format(round(avg_red), round(avg_blue)))
        else:
            channel.reply("Teams are good! Nothing to balance.")
        return True

    def cmd_teams(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        teams = self.teams()
        if len(teams["red"]) != len(teams["blue"]):
            player.tell("Both teams should have the same number of players.")
            return minqlx.RET_STOP_ALL

        teams = dict([(p.steam_id, gt) for p in teams["red"] + teams["blue"]])
        self.add_request(teams, self.callback_teams, channel)

    def callback_teams(self, players, channel):
        # prevent teams call from being called too fast; this also fixes the double-call when people join
        with self.teams_lock:
            t = time.time()
            if (t - self.last_teams_call_timestamp < TEAMS_CALL_COOLDOWN):
                return
            self.last_teams_call_timestamp = t

        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = teams["red"] + teams["blue"]
        gt = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = dict([(p.steam_id, gt) for p in current])
                self.add_request(d, self.callback_teams, channel)
                return

        avg_red = self.team_average(teams["red"], gt)
        avg_blue = self.team_average(teams["blue"], gt)
        switch = self.suggest_switch(teams, gt)
        diff_rounded = abs(round(avg_red) - round(avg_blue)) # Round individual averages.
        if round(avg_red) > round(avg_blue):
            channel.reply("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^1{}"
                .format(round(avg_red), round(avg_blue), diff_rounded))
        elif round(avg_red) < round(avg_blue):
            channel.reply("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^4{}"
                .format(round(avg_red), round(avg_blue), diff_rounded))
        else:
            channel.reply("^1{} ^7vs ^4{}^7 - Holy shit!"
                .format(round(avg_red), round(avg_blue)))

        minimum_suggestion_diff = self.get_cvar("qlx_balanceMinimumSuggestionDiff", float)
        force_swap_diff = self.get_cvar("qlx_balanceForceSwapDiff", float)
        if switch and switch[1] >= minimum_suggestion_diff:
            gt = self.game.type_short
            # Auto-swap is disabled (never triggers) when force_swap_diff is
            # missing, empty, zero, or negative. All three of these give the
            # same behavior:
            #   - cvar absent from server.cfg entirely
            #   - cvar set to "" (empty string)
            #   - cvar set to "0"
            # Without explicit handling, an empty/missing cvar makes
            # get_cvar(..., float) return None and `None > 0` would raise a
            # TypeError in Python 3.
            force_swap = (force_swap_diff is not None
                          and force_swap_diff > 0
                          and diff_rounded >= force_swap_diff)
            # On continuous gametypes (TDM, CTF, DOM), there is no
            # meaningful "end of the round" mid-game -- the queued swap
            # would only execute when the entire game ends. So when a
            # force-swap is triggered on a continuous gametype, execute it
            # immediately and say so. On round-based gametypes (AD, CA, FT),
            # keep the existing "queue for end of round" behavior.
            if force_swap and gt not in ROUND_BASED_GAMETYPES:
                message = "Players ^6{}^7 and ^6{}^7 will be swapped now because teams are greatly unbalanced!"
            elif force_swap:
                message = "Players ^6{}^7 and ^6{}^7 will be swapped at the end of the round because teams are greatly unbalanced!"
            else:
                message = "SUGGESTION: switch ^6{}^7 with ^6{}^7. Mentioned players can type !a to agree."
            channel.reply(message.format(switch[0][0].clean_name, switch[0][1].clean_name))
            if not self.suggested_pair or self.suggested_pair[0] != switch[0][0] or self.suggested_pair[1] != switch[0][1]:
                self.suggested_pair = (switch[0][0], switch[0][1])
                self.suggested_agree = [True, True] if force_swap else [False, False]
                # If we just decided to force-swap on a continuous gametype,
                # don't wait for handle_round_end -- it won't fire until the
                # whole game ends. Execute now.
                if force_swap and gt not in ROUND_BASED_GAMETYPES:
                    self.execute_suggestion()
        else:
            i = random.randint(0, 99)
            if not i:
                channel.reply("Teens look ^6good!")
            else:
                channel.reply("Teams look good!")
            self.suggested_pair = None

        return True

    def cmd_do(self, player, msg, channel):
        """Forces a suggested switch to be done."""
        if self.suggested_pair:
            self.execute_suggestion()

    def cmd_agree(self, player, msg, channel):
        """After the bot suggests a switch, players in question can use this to agree to the switch."""
        if self.suggested_pair and not all(self.suggested_agree):
            p1, p2 = self.suggested_pair

            if p1 == player:
                self.suggested_agree[0] = True
            elif p2 == player:
                self.suggested_agree[1] = True

            if all(self.suggested_agree):
                # On round-based gametypes (AD, CA, FT), defer to the next
                # round start if the game's in progress -- the existing
                # handle_round_end hook will catch and execute it.
                # On continuous gametypes (TDM, CTF, DOM), there's no
                # meaningful "next round" boundary mid-game -- the only
                # round_end event fires when the whole game ends, which
                # would leave players waiting up to a full timelimit/
                # fraglimit before the switch happens. Execute immediately
                # in that case.
                gt = self.game.type_short
                if self.game.state == "in_progress" and not self.in_countdown \
                        and gt in ROUND_BASED_GAMETYPES:
                    self.msg("Both players agreed. The switch will be executed at the start of next round.")
                    return

                # Otherwise, switch right away.
                self.execute_suggestion()

    def cmd_ratings(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in EXT_SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        players = dict([(p.steam_id, gt) for p in self.players()])
        self.add_request(players, self.callback_ratings, channel)

    def callback_ratings(self, players, channel):
        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = self.players()
        gt = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = dict([(p.steam_id, gt) for p in current])
                self.add_request(d, self.callback_ratings, channel)
                return

        if teams["free"]:
            free_sorted = sorted(teams["free"], key=lambda x: self.ratings[x.steam_id][gt]["elo"], reverse=True)
            free = ", ".join(["{}: ^6{}^7".format(p.clean_name, self.ratings[p.steam_id][gt]["elo"]) for p in free_sorted])
            channel.reply(free)
        if teams["red"]:
            red_sorted = sorted(teams["red"], key=lambda x: self.ratings[x.steam_id][gt]["elo"], reverse=True)
            red = ", ".join(["{}: ^1{}^7".format(p.clean_name, self.ratings[p.steam_id][gt]["elo"]) for p in red_sorted])
            channel.reply(red)
        if teams["blue"]:
            blue_sorted = sorted(teams["blue"], key=lambda x: self.ratings[x.steam_id][gt]["elo"], reverse=True)
            blue = ", ".join(["{}: ^4{}^7".format(p.clean_name, self.ratings[p.steam_id][gt]["elo"]) for p in blue_sorted])
            channel.reply(blue)
        if teams["spectator"]:
            spec_sorted = sorted(teams["spectator"], key=lambda x: self.ratings[x.steam_id][gt]["elo"], reverse=True)
            spec = ", ".join(["{}: {}".format(p.clean_name, self.ratings[p.steam_id][gt]["elo"]) for p in spec_sorted])
            channel.reply(spec)

    def suggest_switch(self, teams, gametype):
        """Suggest a switch based on average team ratings."""
        avg_red = self.team_average(teams["red"], gametype)
        avg_blue = self.team_average(teams["blue"], gametype)
        cur_diff = abs(avg_red - avg_blue)
        min_diff = 999999
        best_pair = None

        for red_p in teams["red"]:
            for blue_p in teams["blue"]:
                r = teams["red"].copy()
                b = teams["blue"].copy()
                b.append(red_p)
                r.remove(red_p)
                r.append(blue_p)
                b.remove(blue_p)
                avg_red = self.team_average(r, gametype)
                avg_blue = self.team_average(b, gametype)
                diff = abs(avg_red - avg_blue)
                if diff < min_diff:
                    min_diff = diff
                    best_pair = (red_p, blue_p)

        if min_diff < cur_diff:
            return (best_pair, cur_diff - min_diff)
        else:
            return None

    def team_average(self, team, gametype):
        """Calculates the average rating of a team."""
        avg = 0
        if team:
            for p in team:
                avg += self.ratings[p.steam_id][gametype]["elo"]
            avg /= len(team)

        return avg

    def execute_suggestion(self):
        p1, p2 = self.suggested_pair
        try:
            p1.update()
            p2.update()
        except minqlx.NonexistentPlayerError:
            return

        if p1.team != "spectator" and p2.team != "spectator":
            self.switch(self.suggested_pair[0], self.suggested_pair[1])

        self.suggested_pair = None
        self.suggested_agree = [False, False]

    # helper functions for the queue plugin
    # empty callback on purpose - used to fetch the player elo through sending it to add_request without printing
    # anything in chat
    def callback_fetch_player_elo(self, players, channel):
        pass

    def get_player_elo(self, player, attempt=0):
        try:
            return self.ratings[player.steam_id][self.game.type_short]["elo"]
        except:
            # normally this shouldn't happen at all but if for whatever reason we couldn't fetch the elo we need to
            # re-fetch it and return it after some wait again
            if attempt > 3:
                raise Exception("couldn't fetch rating for player {}".format(player.steam_id))

            minqlx.console_command("echo Couldn't fetch rating for player {} when adding to teams".format(player.steam_id))
            self.add_request({ player.steam_id: self.game.type_short }, self.callback_fetch_player_elo, minqlx.CHAT_CHANNEL)
            time.sleep(0.1)
            return self.get_player_elo(player, attempt + 1)

    def get_team_averages(self, attempt=0):
        gt = self.game.type_short
        try:
            teams = self.teams()
            avg_red = self.team_average(teams["red"], gt)
            avg_blue = self.team_average(teams["blue"], gt)
            return { "red": avg_red, "blue": avg_blue }
        except:
            # normally this shouldn't happen at all but if for whatever reason we couldn't fetch the elo of some player
            # we need to re-fetch it and return it after some wait again
            if attempt > 3:
                raise Exception("couldn't calculate the average rating for teams")

            minqlx.console_command("echo Couldn't calculate the average rating for teams!")
            teams = self.teams()
            current = teams["red"] + teams["blue"]
            d = dict([(p.steam_id, gt) for p in current])
            self.add_request(d, self.callback_fetch_player_elo, minqlx.CHAT_CHANNEL)
            time.sleep(0.1)
            return self.get_team_averages(attempt + 1)