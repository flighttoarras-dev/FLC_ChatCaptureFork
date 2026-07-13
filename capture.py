"""
capture.py -- FLC_ChatCapture companion script
Part of the Chronicler project.

Polls the Foundry VTT JavaScript runtime via the WebView2 debug port,
captures chat messages and PF2e party state, writes session JSON files.
"""

import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import websockets

TOOL_VERSION = "1.0.0"

# =============================================================================
# Config & logging
# =============================================================================

def load_config():
    path = Path(__file__).parent / "capture_config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"ERROR: capture_config.json not found at {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: capture_config.json is invalid JSON: {e}")


def setup_logging(log_path):
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


# =============================================================================
# CDP helpers
# =============================================================================

def find_game_page(debug_port):
    """Return the WebSocket debugger URL for the Foundry /game page, or None."""
    try:
        with urllib.request.urlopen(
            f"http://localhost:{debug_port}/json", timeout=5
        ) as resp:
            pages = json.loads(resp.read())
        for page in pages:
            if "/game" in page.get("url", ""):
                return page["webSocketDebuggerUrl"]
    except Exception:
        pass
    return None


async def cdp_eval(ws_url, expression):
    """
    Evaluate one JavaScript expression in the Foundry page via CDP.
    Returns a Python object (dict, list, str, bool, int, float) or None.
    Raises RuntimeError on connection failure.
    """
    try:
        async with websockets.connect(ws_url, open_timeout=10, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except (OSError, websockets.exceptions.WebSocketException) as e:
        raise RuntimeError(f"CDP connection failed: {e}") from e
    except asyncio.TimeoutError as e:
        raise RuntimeError("CDP eval timed out") from e

    node = resp.get("result", {}).get("result", {})
    rtype = node.get("type")

    if rtype in ("undefined", None) or node.get("subtype") == "null":
        return None
    if rtype == "string":
        val = node.get("value") or ""
        if not val:
            return None
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val  # plain string (e.g. a title that isn't valid JSON)
    return node.get("value")  # bool, number


# =============================================================================
# JavaScript expressions
# All complex values use JSON.stringify() so cdp_eval gets a JSON string back.
# =============================================================================

JS_READY = "game.ready === true"

JS_META = """JSON.stringify({
    game_title: game.world?.title   ?? null,
    world_id:   game.world?.id      ?? null,
    system:     game.system?.id     ?? null,
    user:       game.user?.name     ?? null
})"""

JS_SCENE = "JSON.stringify(game.scenes?.current?.name ?? null)"

JS_MESSAGES = "JSON.stringify(game.messages.contents.map(m => m.toObject()))"

JS_PARTY = """JSON.stringify(
    (game.actors?.party?.members ?? []).map(m => ({
        name:       m.name,
        hp: {
            value: m.system?.attributes?.hp?.value ?? null,
            max:   m.system?.attributes?.hp?.max   ?? null,
            temp:  m.system?.attributes?.hp?.temp  ?? 0
        },
        sp: m.system?.attributes?.hp?.sp
              ? { value: m.system.attributes.hp.sp.value,
                  max:   m.system.attributes.hp.sp.max }
              : null,
        heroPoints: m.system?.resources?.heroPoints?.value ?? null,
        conditions: (m.conditions ?? []).map(c => ({
            name:  c.name,
            value: c.value ?? null
        }))
    }))
)"""

JS_WEALTH_PC = """JSON.stringify(
    (game.actors?.party?.members ?? []).map(m => ({
        name:  m.name,
        coins: m.inventory?.coins
                 ? { pp: m.inventory.coins.pp,
                     gp: m.inventory.coins.gp,
                     sp: m.inventory.coins.sp,
                     cp: m.inventory.coins.cp }
                 : null
    }))
)"""

JS_WEALTH_PARTY = """JSON.stringify(
    game.actors?.party?.inventory?.coins
      ? { pp: game.actors.party.inventory.coins.pp,
          gp: game.actors.party.inventory.coins.gp,
          sp: game.actors.party.inventory.coins.sp,
          cp: game.actors.party.inventory.coins.cp }
      : null
)"""

# Captures end-of-session character data. Evaluated every poll and cached so we
# always have the latest values ready when the connection drops unexpectedly.
JS_END_DATA = """JSON.stringify(
    (game.actors?.party?.members ?? []).map(m => ({
        name:     m.name,
        class:    m.class?.name    ?? null,
        ancestry: m.ancestry?.name ?? null,
        level:    m.system?.details?.level?.value ?? null,
        xp:       m.system?.details?.xp?.value    ?? null,
        coins: m.inventory?.coins
                 ? { pp: m.inventory.coins.pp,
                     gp: m.inventory.coins.gp,
                     sp: m.inventory.coins.sp,
                     cp: m.inventory.coins.cp }
                 : null,
        saves: {
            fortitude: m.system?.saves?.fortitude?.value ?? null,
            reflex:    m.system?.saves?.reflex?.value    ?? null,
            will:      m.system?.saves?.will?.value      ?? null
        },
        skills: Object.fromEntries(
            Object.entries(m.system?.skills ?? {}).map(([k, v]) => [
                k,
                {
                    label:       v.label,
                    proficiency: ["Untrained","Trained","Expert","Master","Legendary"][v.rank]
                                 ?? String(v.rank),
                    modifier:    v.totalModifier
                }
            ])
        )
    }))
)"""

JS_ENEMY_CONDITIONS = """JSON.stringify(
  (canvas.tokens?.placeables ?? [])
    .filter(t => t.actor && !t.actor.hasPlayerOwner && t.actor.type !== 'party')
    .map(t => ({
      token_id:   t.id,
      name:       t.name,
      conditions: (t.actor?.conditions?.active ?? [])
                    .map(c => ({ slug: c.slug, name: c.name, value: c.value ?? null }))
    }))
    .filter(t => t.conditions.length > 0)
)"""

JS_COMBAT = """JSON.stringify(
  (game.combat && game.combat.active) ? {
    id:         game.combat.id,
    round:      game.combat.round,
    combatants: (game.combat.combatants?.contents ?? []).map(c => ({
      name:           c.name,
      initiative:     c.initiative,
      defeated:       c.isDefeated,
      hidden:         c.hidden,
      hasPlayerOwner: c.actor?.hasPlayerOwner ?? false
    }))
  } : null
)"""


# =============================================================================
# Utilities
# =============================================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ms_to_iso(ms):
    """Convert a Foundry Unix-millisecond timestamp to an ISO-8601 UTC string."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def sanitize_filename(name):
    """Remove characters illegal in Windows filenames."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "unknown").strip() or "unknown"


def resolve_path(raw, base_dir):
    """
    Expand Windows environment variables (e.g. %USERPROFILE%), then return
    an absolute Path. Relative paths are resolved against base_dir so dev
    mode still works without any config changes.
    """
    expanded = Path(os.path.expandvars(raw))
    return expanded if expanded.is_absolute() else base_dir / expanded


def safe_write(path, data):
    """Write JSON atomically via a temp file to prevent corruption on crash or kill."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic on NTFS within the same volume


def state_fingerprint(party_state, scene_name):
    """
    Stable string summarising party state for change detection.
    Only includes fields that matter for the timeline (HP, SP, hero points,
    conditions, scene). Returns None if party_state is empty.
    """
    if not party_state:
        return None
    simplified = [
        {
            "name":       m.get("name"),
            "hp":         m.get("hp"),
            "sp":         m.get("sp"),
            "heroPoints": m.get("heroPoints"),
            "conditions": sorted(
                c.get("name", "") + str(c.get("value") or "")
                for c in (m.get("conditions") or [])
            ),
        }
        for m in party_state
    ]
    return json.dumps({"members": simplified, "scene": scene_name}, sort_keys=True)


# =============================================================================
# Message filtering
# =============================================================================

def should_capture(msg, capture_private):
    """
    Returns (capture: bool, flag_as_private: bool).
    Blind GM rolls are always skipped (they're secret GM dice).
    Whispered messages are included only when capture_private is True.
    """
    if msg.get("blind"):
        return False, False
    if msg.get("whisper"):
        if not capture_private:
            return False, False
        return True, True
    return True, False


# =============================================================================
# Session
# =============================================================================

class Session:
    """
    Holds all data for one game session. Created when the /game page is first
    detected. Updated on every poll. Written to disk after each poll so the
    file is never more than one poll interval out of date.
    """

    def __init__(self, meta, log_dir, capture_private, split_from=None,
                 use_subfolders=False, game_schedules=None):
        self.capture_private = capture_private
        title_slug = sanitize_filename(meta.get("game_title") or "unknown")
        self.log_dir = log_dir / title_slug if use_subfolders else log_dir

        split_at = now_iso() if split_from else None

        schedule = (game_schedules or {}).get(meta.get("game_title") or "")
        scheduled_day   = schedule.get("day")        if schedule else None
        scheduled_start = schedule.get("start_time") if schedule else None

        self.meta = {
            "game_title":      meta.get("game_title"),
            "world_id":        meta.get("world_id"),
            "system":          meta.get("system"),
            "user":            meta.get("user"),
            "start_time":      split_at or now_iso(),
            "end_time":        None,
            "end_reason":      None,
            "split_from":      split_from,
            "split_at":        split_at,
            "scheduled_day":   scheduled_day,
            "scheduled_start": scheduled_start,
            "tool_version":    TOOL_VERSION,
        }

        self.session_start  = {"wealth_per_pc": None, "party_wealth": None}
        self.session_end    = {"wealth_per_pc": None, "party_wealth": None, "characters": None}

        self.seen_ids       = set()
        self.messages       = []
        self.state_timeline = []
        self._last_fp       = None

        # Combat tracker state
        self.combat_log      = []
        self._current_enc    = None
        self._last_combat_id = None
        self._last_round     = None
        self._last_order     = None
        self._last_defeated  = set()

        # Enemy condition state
        self.enemy_conditions     = []
        self._last_enemy_cond_fp  = {}

        date = datetime.now().strftime("%Y-%m-%d")
        if scheduled_day and scheduled_start:
            start_tag = scheduled_start.replace(":", "")
            filename  = f"{date}_{scheduled_day}_{start_tag}_{title_slug}.json"
        else:
            ts       = datetime.now().strftime("%Y-%m-%d_%H-%M")
            filename = f"{ts}_{title_slug}.json"

        self.path = self.log_dir / filename
        logging.warning("Session started → %s", self.path.name)

    # ── messages ──────────────────────────────────────────────────────────────

    def add_messages(self, raw):
        """Dedup by _id, filter, append new messages. Returns count added."""
        added = 0
        for msg in (raw or []):
            mid = msg.get("_id")
            if not mid or mid in self.seen_ids:
                continue
            self.seen_ids.add(mid)
            capture, flag = should_capture(msg, self.capture_private)
            if not capture:
                continue
            msg = dict(msg)
            msg["timestamp_iso"] = ms_to_iso(msg.get("timestamp"))
            if flag:
                msg["_private_gm_roll"] = True
            self.messages.append(msg)
            added += 1
        if added:
            logging.warning("%d new message(s) captured (session total: %d)", added, len(self.messages))
        return added

    def sync_deletions(self, current_ids):
        """
        Flag any captured messages absent from the current Foundry chat.
        Only runs when current_ids is non-empty (guards against partial page loads
        returning zero messages while game.ready is briefly true).
        """
        if not current_ids:
            return
        for msg in self.messages:
            if msg["_id"] not in current_ids and not msg.get("deleted"):
                msg["deleted"]    = True
                msg["deleted_at"] = now_iso()
                logging.warning("Message %s flagged as deleted", msg["_id"])

    # ── wealth ────────────────────────────────────────────────────────────────

    def set_start_wealth(self, wealth_pc, wealth_party):
        """Record the session-start wealth snapshot once on the first poll that has data."""
        if self.session_start["wealth_per_pc"] is None and wealth_pc:
            self.session_start["wealth_per_pc"] = wealth_pc
            self.session_start["party_wealth"]   = wealth_party

    def cache_end_data(self, wealth_pc, wealth_party, characters):
        """Overwrite the end-of-session cache on every poll."""
        self.session_end["wealth_per_pc"] = wealth_pc
        self.session_end["party_wealth"]  = wealth_party
        self.session_end["characters"]    = characters

    # ── state timeline ────────────────────────────────────────────────────────

    def record_state(self, party_state, scene_name):
        """Append a timeline entry only when HP, conditions, hero points, or scene changed."""
        fp = state_fingerprint(party_state, scene_name)
        if fp is None or fp == self._last_fp:
            return
        self._last_fp = fp
        self.state_timeline.append({
            "timestamp": now_iso(),
            "scene":     scene_name,
            "members":   party_state,
        })

    # ── enemy conditions ──────────────────────────────────────────────────────

    def record_enemy_conditions(self, tokens):
        """Append to enemy_conditions timeline only when a token's condition set changes."""
        if tokens is None:
            return
        current_fp = {
            t["token_id"]: frozenset(
                (c["slug"], c.get("value")) for c in t["conditions"]
            )
            for t in tokens
        }
        if current_fp == self._last_enemy_cond_fp:
            return
        self._last_enemy_cond_fp = current_fp
        self.enemy_conditions.append({
            "time":   now_iso(),
            "tokens": tokens,
        })

    # ── combat tracker ────────────────────────────────────────────────────────

    def record_combat(self, combat):
        """Called each poll cycle with the result of JS_COMBAT (or None)."""
        if not combat:
            if self._current_enc is not None:
                self._close_encounter()
            return

        combat_id  = combat.get("id")
        combatants = combat.get("combatants") or []
        round_num  = combat.get("round") or 0

        if combat_id != self._last_combat_id:
            if self._current_enc is not None:
                self._close_encounter()
            self._start_encounter(combat_id, combatants, round_num)
            return

        current_order = self._order_key(combatants)
        defeated_now  = {c["name"] for c in combatants if c.get("defeated")}
        new_defeats   = defeated_now - self._last_defeated
        for name in sorted(new_defeats):
            self._enc_event("defeated", {"combatant": name})
        if new_defeats:
            self._last_defeated = defeated_now

        if round_num != self._last_round and round_num > 0:
            self._enc_event("round", {"round": round_num})
            self._last_round = round_num
        elif current_order != self._last_order:
            self._enc_event("delay", {"detail": "initiative order changed"})

        self._last_order = current_order
        if self._current_enc is not None:
            self._current_enc["rounds"] = max(self._current_enc.get("rounds", 0), round_num)

    def _order_key(self, combatants):
        return tuple(sorted((c.get("name", ""), c.get("initiative") or 0) for c in combatants))

    def _start_encounter(self, combat_id, combatants, round_num):
        enc = {
            "encounter_id":     combat_id,
            "started_at":       now_iso(),
            "ended_at":         None,
            "rounds":           round_num,
            "initiative_order": [
                {
                    "name":           c.get("name"),
                    "initiative":     c.get("initiative"),
                    "hasPlayerOwner": c.get("hasPlayerOwner", False),
                    "hidden":         c.get("hidden", False),
                }
                for c in sorted(combatants,
                                key=lambda c: c.get("initiative") or -999, reverse=True)
            ],
            "events": [],
        }
        if round_num > 1:
            enc["events"].append({"time": now_iso(), "type": "round", "round": round_num})
        self._current_enc    = enc
        self._last_combat_id = combat_id
        self._last_round     = round_num
        self._last_order     = self._order_key(combatants)
        self._last_defeated  = {c["name"] for c in combatants if c.get("defeated")}
        logging.warning("Combat started → encounter %s, round %d", combat_id, round_num)

    def _close_encounter(self):
        self._current_enc["ended_at"] = now_iso()
        self._enc_event("combat_end", {"final_round": self._current_enc.get("rounds", 0)})
        self.combat_log.append(self._current_enc)
        logging.warning("Combat ended → %d round(s)", self._current_enc.get("rounds", 0))
        self._current_enc    = None
        self._last_combat_id = None
        self._last_round     = None
        self._last_order     = None
        self._last_defeated  = set()

    def _enc_event(self, event_type, extra=None):
        if self._current_enc is None:
            return
        evt = {"time": now_iso(), "type": event_type}
        if extra:
            evt.update(extra)
        self._current_enc["events"].append(evt)

    # ── file write ────────────────────────────────────────────────────────────

    def write(self, final=False, end_reason=None, next_world=None):
        """
        Write the session file to disk.
        end_reason="world_change" marks a precise audio-split boundary for the
        voice pipeline. next_world names the game that follows.
        Routine poll writes leave end_time/end_reason as None so a reconnect
        can continue the same session without an incorrect end timestamp.
        """
        if final or end_reason:
            self.meta["end_time"] = now_iso()
            if end_reason:
                self.meta["end_reason"] = end_reason
            if next_world:
                self.meta["next_world"] = next_world
            logging.warning(
                "Session closed → %s  (%d messages, %d state entries)%s",
                self.path.name, len(self.messages), len(self.state_timeline),
                f"  [world_change → {next_world}]" if next_world else "",
            )
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            sorted_msgs = sorted(self.messages, key=lambda m: m.get("timestamp", 0))
            safe_write(self.path, {
                "session_meta":   self.meta,
                "session_start":  self.session_start,
                "session_end":    self.session_end,
                "messages":       sorted_msgs,
                "state_timeline": self.state_timeline,
                "combat_log":       self.combat_log,
                "enemy_conditions": self.enemy_conditions,
            })
        except Exception as e:
            logging.error("Failed to write session file: %s", e)


# =============================================================================
# Poll cycle
# =============================================================================

async def poll_once(ws_url, config, log_dir, session):
    """
    Execute one full poll cycle against the live Foundry page.
    Returns (session, error). error is None on success, a string on CDP failure.
    Session is always returned even on error so main() never loses a partially-
    initialised session and creates a duplicate file on the next reconnect.
    """
    capture_party   = config.get("party_data_system", "pf2e") != "none"
    capture_private = config.get("capture_private_gm_rolls", False)
    capture_combat            = config.get("capture_combat", True)
    capture_enemy_conditions  = config.get("capture_enemy_conditions", False)
    use_subfolders            = config.get("use_game_subfolders", False)
    game_schedules  = config.get("game_schedules", {})

    try:
        # If game isn't fully loaded yet, skip this cycle
        ready = await cdp_eval(ws_url, JS_READY)
        if ready is not True:
            return session, None

        meta = await cdp_eval(ws_url, JS_META) or {}
        if not meta.get("game_title"):
            return session, None

        # Start a new session, or detect world change (audio-split boundary)
        if session is None:
            session = Session(meta, log_dir, capture_private,
                              use_subfolders=use_subfolders, game_schedules=game_schedules)
        elif meta.get("game_title") != session.meta.get("game_title"):
            prev_title = session.meta.get("game_title")
            new_title  = meta.get("game_title")
            logging.warning("World change: %s → %s", prev_title, new_title)
            session.write(end_reason="world_change", next_world=new_title)
            session = Session(meta, log_dir, capture_private, split_from=prev_title,
                              use_subfolders=use_subfolders, game_schedules=game_schedules)

        # Chat messages
        raw_msgs = await cdp_eval(ws_url, JS_MESSAGES) or []
        session.add_messages(raw_msgs)
        session.sync_deletions({m["_id"] for m in raw_msgs if "_id" in m})

        # Wealth
        wealth_pc    = await cdp_eval(ws_url, JS_WEALTH_PC)
        wealth_party = await cdp_eval(ws_url, JS_WEALTH_PARTY)
        session.set_start_wealth(wealth_pc, wealth_party)

        # Party state for the timeline
        scene = await cdp_eval(ws_url, JS_SCENE)
        if capture_party:
            party = await cdp_eval(ws_url, JS_PARTY)
            session.record_state(party, scene)

        # Enemy condition timeline
        if capture_enemy_conditions:
            enemy_tokens = await cdp_eval(ws_url, JS_ENEMY_CONDITIONS)
            session.record_enemy_conditions(enemy_tokens)

        # Combat tracker
        if capture_combat:
            combat = await cdp_eval(ws_url, JS_COMBAT)
            session.record_combat(combat)

        # Cache end-of-session data (race condition guard: CDP may drop without warning)
        end_chars = await cdp_eval(ws_url, JS_END_DATA)
        session.cache_end_data(wealth_pc, wealth_party, end_chars)

        # Persist to disk after every successful poll
        session.write()

        return session, None

    except RuntimeError as e:
        if session:
            session.write()
        return session, str(e)


# =============================================================================
# Entry point
# =============================================================================

async def main():
    config   = load_config()
    base_dir = Path(__file__).parent
    log_dir  = resolve_path(config.get("log_directory",  "chatlogcaptures"),  base_dir)
    log_file = resolve_path(config.get("log_file", "capture_log.txt"), base_dir)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(log_file)
    logging.warning("capture.py %s starting", TOOL_VERSION)

    debug_port = config.get("debug_port", 9222)
    interval   = config.get("poll_interval_seconds", 5)
    session    = None
    waiting_logged = False

    while True:
        ws_url = find_game_page(debug_port)

        if ws_url:
            waiting_logged = False
            session, err = await poll_once(ws_url, config, log_dir, session)
            if err:
                logging.warning("CDP disconnected (%s) — waiting to reconnect", err)
        else:
            if not waiting_logged:
                logging.warning("Waiting for Foundry game page on port %d…", debug_port)
                waiting_logged = True

        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.warning("capture.py stopped by user")
