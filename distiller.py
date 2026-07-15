#!/usr/bin/env python3
"""
distiller.py  —  Chronicler Distiller

Collapses a raw Foundry/PF2e chatlog capture into compact per-event records.
Shared upstream for Notion+GPT (recap/dossier) and homelab Postgres+pgvector.

Usage:
    python distiller.py <input.json> [output.json]
    python distiller.py <file1.json> <file2.json> ...   # merge split sessions
    If output is omitted, writes to <first_input>_distilled.json

─────────────────────────────────────────────────────────────────────────────
ARCHITECTURE BOUNDARY — READ BEFORE MODIFYING
─────────────────────────────────────────────────────────────────────────────
This file is the ONLY PF2e-aware component in the Chronicler pipeline.
Everything downstream (Postgres schema, pgvector, the AI agent, Notion, GPT)
consumes the distilled event shape and never touches a Foundry/PF2e field.
Swap this file for a dnd5e_distiller.py and nothing else moves.

What belongs INSIDE this file (PF2e-specific, quarantined here):
  - flags.pf2e.* parsing — context, origin, appliedDamage, slug arrays
  - HTML class parsing (item-card, action-content, data-applications)
  - rolls[] escaped-JSON shape and the flattenDice walker
  - Trait slugs (self:trait:eidolon, etc.) and actor_role classification
  - IWR / data-applications mitigation parsing
  - system_version / core_version stamping

The OUTPUT CONTRACT is system-agnostic (this is the seam):
  - actor, controller, actor_role, event_type, outcome, summary
  - roll { total, dice, formula, breakdown }
  - mitigation fields
  - grepped timeline fields (round, turn, hp_remaining, etc.)

event_type is a closed, generic enum: attack-roll | saving-throw | damage-roll |
damage-taken | spell-cast | ability-posted | action | chat | healing
These are TTRPG-universal concepts. A D&D 5e distiller emits the same enum.

LITMUS TEST for new fields: would this field name make sense to a 5e distiller?
If no, it either gets normalised to a generic name or stays as an internal
intermediate — it must not appear on the output event object.
Examples of what NOT to leak: fortitude_dc, degreeOfSuccess (as an integer),
pf2e_origin_uuid, systemVersion on individual events.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

DISTILLER_VERSION = "1.1.0"


# =============================================================================
# HTML helpers
# =============================================================================

_TAG_RE         = re.compile(r"<[^>]+>")
_WS_RE          = re.compile(r"\s+")
_ACTION_GLYPH   = re.compile(r'<span[^>]*class="[^"]*action-glyph[^"]*"[^>]*>.*?</span>', re.DOTALL)

def strip_tags(html):
    text = _TAG_RE.sub(" ", html or "")
    return _WS_RE.sub(" ", text).strip()

def first_match(pattern, html, flags=re.DOTALL):
    m = re.search(pattern, html or "", flags)
    return m.group(1) if m else None

def extract_h3(html):
    raw = first_match(r"<h3[^>]*>(.*?)</h3>", html)
    return strip_tags(raw) if raw else None

def extract_h4(html, cls=None):
    if cls:
        pattern = rf'<h4[^>]*class="[^"]*{re.escape(cls)}[^"]*"[^>]*>(.*?)</h4>'
    else:
        pattern = r"<h4[^>]*>(.*?)</h4>"
    raw = first_match(pattern, html)
    if raw:
        raw = _ACTION_GLYPH.sub("", raw)
        return strip_tags(raw) or None
    return None

def extract_data_attr(html, attr):
    m = re.search(rf'data-{re.escape(attr)}="([^"]*)"', html or "")
    return m.group(1) if m else None

def extract_damage_text(html):
    m = re.search(r"takes\s+(\d+)\s+damage", html or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


# =============================================================================
# Roll parser
# =============================================================================

def flatten_dice(node, out=None):
    """
    Recursive dice walker — handles attack/save flat rolls and nested
    crit Grouping/ArithmeticExpression damage shapes PF2e uses.
    """
    if out is None:
        out = []
    if not node or not isinstance(node, dict):
        return out
    if node.get("class") == "Die":
        out.append({
            "faces":   node.get("faces"),
            "results": [r.get("result") for r in (node.get("results") or [])],
            "flavor":  (node.get("options") or {}).get("flavor") or None,
        })
        return out
    for key in ("terms", "rolls", "operands", "term"):
        v = node.get(key)
        if isinstance(v, list):
            for child in v:
                flatten_dice(child, out)
        elif v:
            flatten_dice(v, out)
    return out


def parse_roll(roll_str):
    try:
        r = json.loads(roll_str)
    except Exception:
        return None
    return {
        "total":     r.get("total"),
        "dice":      flatten_dice(r),
        "formula":   r.get("formula"),
        "breakdown": r.get("breakdown"),
    }


# =============================================================================
# Version lift  (§1)
# =============================================================================

def lift_versions(messages):
    """Extract systemVersion and coreVersion from the first message that has _stats."""
    for msg in messages:
        stats = msg.get("_stats") or {}
        sv = stats.get("systemVersion")
        cv = stats.get("coreVersion")
        if sv or cv:
            return sv, cv
    return None, None


# =============================================================================
# RollOptions whitelist grep  (§6)
# =============================================================================

_GREP_PATTERNS = [
    (r"hp-remaining:(\d+)",                                "hp_remaining", int),
    (r"hp-percent:(\d+)",                                  "hp_percent",   int),
    (r"encounter:round:(\d+)",                             "round",        int),
    (r"encounter:turn:(\d+)",                              "turn",         int),
    (r"self:participant:initiative:rank:(\d+)",            "init_rank",    int),
    (r"self:participant:initiative:roll:(-?\d+(?:\.\d+)?)", "init_roll",   float),
]

def grep_roll_options(options_list):
    result = {}
    for item in (options_list or []):
        for pattern, key, cast in _GREP_PATTERNS:
            if key not in result:
                m = re.search(pattern, str(item))
                if m:
                    result[key] = cast(m.group(1))
    return result


# =============================================================================
# Event classification  (§5)
# =============================================================================

_CONTEXT_TYPE_MAP = {
    "attack-roll":  "attack-roll",
    "saving-throw": "saving-throw",
    "damage-roll":  "damage-roll",
    "damage-taken": "damage-taken",
    "spell-cast":   "spell-cast",
}

def classify_event(msg):
    pf2e    = (msg.get("flags") or {}).get("pf2e") or {}
    context = pf2e.get("context") or {}
    ctype   = context.get("type")

    if ctype in _CONTEXT_TYPE_MAP:
        return _CONTEXT_TYPE_MAP[ctype]
    if pf2e.get("casting"):
        return "spell-cast"

    style   = msg.get("style", 0)
    content = msg.get("content") or ""

    if style == 0 and "item-card" in content and not msg.get("rolls"):
        return "ability-posted"
    if style == 3 and "action-content" in content:
        return "action"
    return "chat"


# =============================================================================
# Field extraction
# =============================================================================

_PAREN_SUFFIX = re.compile(r'\s*\([^)]*\)\s*$')

def _strip_action_prefix(text):
    """'Ranged Strike: +3 Striking Musket ( Critical Hit )' → '+3 Striking Musket'"""
    if not text:
        return text
    if ": " in text:
        text = text.split(": ", 1)[1]
    return _PAREN_SUFFIX.sub("", text).strip()

def extract_name(msg):
    """Item / spell / action name from flags, flavor, or content HTML."""
    pf2e   = (msg.get("flags") or {}).get("pf2e") or {}
    origin = pf2e.get("origin") or {}

    if origin.get("name"):
        return origin["name"]

    # Flavor h4 has full name incl. enhancement (+3 Striking …); prefer it over slug.
    flavor = msg.get("flavor") or ""
    name   = extract_h4(flavor, "action") or extract_h4(flavor)
    if name:
        return _strip_action_prefix(name)

    # Content h3 (item cards)
    content = msg.get("content") or ""
    name = extract_h3(content)
    if name:
        return name

    # Slug fallback: origin:item:slug:<slug> → Title Cased (no enhancement bonus)
    for opt in (origin.get("rollOptions") or []):
        m = re.match(r"origin:item:slug:(.+)", opt)
        if m:
            return " ".join(w.capitalize() for w in m.group(1).split("-"))

    return None


def extract_target(msg):
    context = ((msg.get("flags") or {}).get("pf2e") or {}).get("context") or {}
    target  = context.get("target") or {}
    name    = target.get("name") or None
    # Prefer token UUID (scene-scoped, unique per encounter) over actor UUID
    uid     = target.get("token") or target.get("actor") or None
    if name or uid:
        return {"name": name, "id": uid}
    return None


def extract_damage_taken(msg):
    content = msg.get("content") or ""
    result  = {}

    net = extract_damage_text(content)
    if net is not None:
        result["net_damage"] = net

    app_str = extract_data_attr(content, "applications")
    if app_str:
        try:
            iwr_list = json.loads(app_str.replace("&quot;", '"'))
            result["iwr"] = iwr_list

            resistance_absorbed = 0
            weakness_triggered  = 0
            immunity_triggered  = []
            for entry in iwr_list:
                cat     = entry.get("category")
                adj     = entry.get("adjustment") or 0
                ignored = entry.get("ignored", False)
                if cat == "resistance" and not ignored:
                    resistance_absorbed += abs(adj)
                elif cat == "weakness":
                    weakness_triggered += adj
                elif cat == "immunity" and not ignored:
                    immunity_triggered.append(entry.get("type", "unknown"))

            if resistance_absorbed:
                result["resistance_absorbed"] = resistance_absorbed
            if weakness_triggered:
                result["weakness_triggered"] = weakness_triggered
            if immunity_triggered:
                result["immunity_triggered"] = immunity_triggered
        except Exception:
            result["iwr"] = app_str

    pf2e    = (msg.get("flags") or {}).get("pf2e") or {}
    applied = pf2e.get("appliedDamage") or {}
    updates = applied.get("updates")

    # isHealing flag is more reliable than inferring from negative hp delta
    if applied.get("isHealing"):
        result["is_healing"] = True

    # Shield block
    shield = applied.get("shield") or {}
    if shield.get("damage"):
        result["shield_absorbed"] = shield["damage"]

    if updates:
        result["hp_updates"] = updates

        temp_absorbed = sum(
            u.get("value", 0) for u in updates
            if u.get("path", "").endswith(".hp.temp") and (u.get("value") or 0) > 0
        )
        if temp_absorbed:
            result["temp_hp_absorbed"] = temp_absorbed

        # Fallback net_damage for shield-block events where HTML has no "takes N damage" text
        if result.get("net_damage") is None and not applied.get("isHealing"):
            hp_delta = sum(
                u.get("value", 0) for u in updates
                if u.get("path", "").endswith(".hp.value") and (u.get("value") or 0) > 0
            )
            shield_amt = result.get("shield_absorbed", 0)
            if hp_delta or shield_amt:
                result["net_damage"] = hp_delta + shield_amt

        # Healing via negative hp delta (fallback when isHealing flag absent)
        if not applied.get("isHealing"):
            net_heal = sum(
                abs(u.get("value", 0)) for u in updates
                if u.get("path", "").endswith(".hp.value") and (u.get("value") or 0) < 0
            )
            if net_heal:
                result["net_heal"] = net_heal

    return result


# =============================================================================
# Summary line builder
# =============================================================================

_OUTCOME_ATTACK = {
    "criticalSuccess": "crit",
    "success":         "hit",
    "failure":         "miss",
    "criticalFailure": "crit fail",
}
_OUTCOME_SAVE = {
    "criticalSuccess": "crit success",
    "success":         "success",
    "failure":         "failure",
    "criticalFailure": "crit fail",
}

def build_summary(evt, raw_content=None):
    actor      = evt.get("actor") or "?"
    etype      = evt.get("event_type") or "chat"
    name       = evt.get("name")
    outcome    = evt.get("outcome")
    _target    = evt.get("target") or {}
    target     = _target.get("name") or _target.get("id") if isinstance(_target, dict) else _target
    dc         = evt.get("dc")
    roll       = evt.get("roll") or {}
    total      = roll.get("total") if isinstance(roll, dict) else None

    if etype in ("attack-roll", "damage-roll"):
        parts = [actor]
        if outcome:
            parts.append(_OUTCOME_ATTACK.get(outcome, outcome))
        if name:
            parts.append(f"{name} vs {target}" if target else name)
        if total is not None:
            dice   = roll.get("dice") or []
            flavor = next((d["flavor"] for d in dice if d.get("flavor")), None)
            parts.append(f"{total} {flavor}" if flavor else str(total))
        return " | ".join(parts)

    if etype == "saving-throw":
        parts = [actor]
        if outcome:
            parts.append(_OUTCOME_SAVE.get(outcome, outcome))
        dc_str = f"DC {dc}" if dc else ""
        if name:
            parts.append(f"{name} {dc_str}".strip())
        if total is not None:
            parts.append(f"roll {total}")
        return " | ".join(parts)

    if etype == "healing":
        net = evt.get("net_heal")
        return f"{actor} healed {net} HP" if net is not None else f"{actor} was healed"

    if etype == "damage-taken":
        net = evt.get("net_damage")
        return f"{actor} takes {net} damage" if net is not None else f"{actor} takes damage"

    if etype == "spell-cast":
        return f"{actor} casts {name}" if name else f"{actor} casts a spell"

    if etype == "ability-posted":
        return f"{actor} posts {name}" if name else f"{actor} posts ability"

    if etype == "action":
        return f"{actor} uses {name}" if name else f"{actor} takes action"

    # chat — include stripped text preview
    if raw_content:
        text = strip_tags(raw_content)[:120]
        return f"{actor}: {text}" if text else actor
    return actor


# =============================================================================
# Per-message distiller
# =============================================================================

def distill_message(msg):
    pf2e    = (msg.get("flags") or {}).get("pf2e") or {}
    context = pf2e.get("context") or {}
    origin  = pf2e.get("origin") or {}

    event_type = classify_event(msg)

    # Grep rollOptions BEFORE dropping them
    all_options = list(origin.get("rollOptions") or []) + list(context.get("options") or [])
    grepped = grep_roll_options(all_options)

    # Parse rolls
    raw_rolls    = msg.get("rolls") or []
    parsed_rolls = [r for r in (parse_roll(s) for s in raw_rolls if s) if r is not None]

    raw_content = msg.get("content")

    evt = {
        "id":           msg.get("_id"),
        "ts":           msg.get("timestamp_iso"),
        "actor":        (msg.get("speaker") or {}).get("alias"),
        "author":       msg.get("author"),
        "event_type":   event_type,
        "name":         extract_name(msg),
        "outcome":      context.get("outcome"),
        "target":       extract_target(msg),
        "dc":           (context.get("dc") or {}).get("value"),
        "gm_secret":    bool(msg.get("blind")) or bool(msg.get("whisper")),
        # grepped timeline fields
        "round":        grepped.get("round"),
        "turn":         grepped.get("turn"),
        "init_rank":    grepped.get("init_rank"),
        "init_roll":    grepped.get("init_roll"),
        "hp_remaining": grepped.get("hp_remaining"),
        "hp_percent":   grepped.get("hp_percent"),
        # rolls
        "roll": parsed_rolls[0] if len(parsed_rolls) == 1 else (parsed_rolls or None),
    }

    # damage-taken extras (may reclassify to "healing")
    if event_type == "damage-taken":
        extras = extract_damage_taken(msg)
        evt.update(extras)
        if extras.get("is_healing") or (extras.get("net_heal") and not extras.get("net_damage")):
            evt["event_type"] = "healing"

    # deleted flag passthrough
    if msg.get("deleted"):
        evt["deleted"]    = True
        evt["deleted_at"] = msg.get("deleted_at")

    # chat: include stripped text (only event type where raw content is the message)
    if event_type == "chat":
        text = strip_tags(raw_content or "")
        if text:
            evt["text"] = text[:500]

    evt["summary"] = build_summary(evt, raw_content if event_type == "chat" else None)

    return evt


# =============================================================================
# Actor roster — Pass 1b / Pass 2
# =============================================================================

_ROLE_TRAITS = {
    "eidolon":          "eidolon",
    "familiar":         "familiar",
    "familiar-behavior":"familiar",
    "animal-companion": "companion",
    "companion":        "companion",
    "summoned":         "summon",
    "minion":           "summon",
}

def build_actor_roster(messages, events, session_end):
    """
    Pass 1b: collect per-actor authors + traits from raw messages.
    Pass 2:  classify actor_role, resolve controller, backfill both onto events.
    Returns the serialisable actor_roster dict.
    """
    party_names = {
        c["name"] for c in ((session_end or {}).get("characters") or [])
        if c.get("name")
    }

    # --- Pass 1b: gather ---
    roster = {}
    for msg, evt in zip(messages, events):
        actor = evt.get("actor")
        if not actor:
            continue
        if actor not in roster:
            roster[actor] = {"authors": set(), "traits": set()}
        if evt.get("author"):
            roster[actor]["authors"].add(evt["author"])

        pf2e     = (msg.get("flags") or {}).get("pf2e") or {}
        all_opts = (
            list((pf2e.get("origin") or {}).get("rollOptions") or [])
            + list((pf2e.get("context") or {}).get("options") or [])
        )
        for opt in all_opts:
            m = re.match(r"self:trait:(.+)", opt)
            if m:
                roster[actor]["traits"].add(m.group(1))

    # author → pc name (from confirmed PC actors)
    author_to_pc = {}
    for actor, entry in roster.items():
        if actor in party_names:
            for auth in entry["authors"]:
                author_to_pc[auth] = actor

    # --- classify ---
    def classify_role(actor, entry):
        if actor in party_names:
            return "pc"
        for trait in entry["traits"]:
            role = _ROLE_TRAITS.get(trait)
            if role:
                return role
        return "npc"

    def resolve_controller(actor, entry):
        if actor in party_names:
            return actor
        for auth in entry["authors"]:
            pc = author_to_pc.get(auth)
            if pc:
                return pc
        return None

    role_map = {a: classify_role(a, e) for a, e in roster.items()}

    def resolve_controller(actor, entry):
        if actor in party_names:
            return actor
        # Only resolve controller for persistent/transient minions — not for NPCs/enemies.
        # Without this guard, a GM who also plays a PC would cause all their NPC rolls
        # to inherit the PC as controller.
        if role_map.get(actor) not in ("companion", "eidolon", "familiar", "summon"):
            return None
        for auth in entry["authors"]:
            pc = author_to_pc.get(auth)
            if pc:
                return pc
        return None

    controller_map = {a: resolve_controller(a, e) for a, e in roster.items()}

    # --- Pass 2: backfill ---
    for evt in events:
        actor = evt.get("actor")
        evt["actor_role"] = role_map.get(actor, "unknown")
        evt["controller"] = controller_map.get(actor)

    return {
        actor: {
            "role":       role_map.get(actor, "unknown"),
            "controller": controller_map.get(actor),
            "traits":     sorted(entry["traits"]),
            "authors":    sorted(entry["authors"]),
        }
        for actor, entry in roster.items()
    }


# =============================================================================
# Session stats
# =============================================================================

def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _cond_stats(apps):
    if not apps:
        return None
    by_slug = {}
    per_player = {}
    unattributed = {}
    for a in apps:
        slug  = a["slug"]
        actor = a.get("attributed_to")
        by_slug[slug] = by_slug.get(slug, 0) + 1
        if actor:
            player_map = per_player.setdefault(actor, {})
            player_map[slug] = player_map.get(slug, 0) + 1
        else:
            unattributed[slug] = unattributed.get(slug, 0) + 1
    return {
        "total_applied": len(apps),
        "by_slug":       by_slug,
        "per_player":    per_player,
        "unattributed":  unattributed,
        "_note": "Attribution: most recent party roll targeting the same token within 10 s of the condition appearing; unattributed entries had no matching roll in window",
    }


def build_stats(events, session_end=None, condition_apps=None):
    """
    Compute session_stats from distilled events.
    Expects actor_role + controller already backfilled by build_actor_roster().
    Deleted events excluded from all stats.
    """

    active = [e for e in events if not e.get("deleted")]

    # actor_role is backfilled by build_actor_roster() before build_stats() is called
    def is_party(evt):
        return evt.get("actor_role") == "pc"

    def is_gm(evt):
        return evt.get("actor_role") in ("npc", "unknown", None)

    def is_minion(evt):
        return evt.get("actor_role") in ("companion", "eidolon", "familiar", "summon")

    # ── Roll stats ────────────────────────────────────────────────────────────
    def roll_stat(evts):
        totals, outcomes = [], []
        for e in evts:
            roll = e.get("roll")
            if not isinstance(roll, dict):
                continue
            t = roll.get("total")
            if t is not None:
                totals.append(t)
                outcomes.append(e.get("outcome"))
        if not totals:
            return None
        n = len(totals)
        crits   = sum(1 for o in outcomes if o == "criticalSuccess")
        fumbles = sum(1 for o in outcomes if o == "criticalFailure")
        s = sorted(totals)
        median  = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {
            "count":         n,
            "avg":           round(sum(totals) / n, 1),
            "median":        median,
            "high":          max(totals),
            "low":           min(totals),
            "crits":         crits,
            "fumbles":       fumbles,
            "crit_rate_pct": round(crits / n * 100, 1),
        }

    roll_events = [e for e in active if isinstance(e.get("roll"), dict) and not e.get("gm_secret")]
    party_rolls     = [e for e in roll_events if is_party(e)]
    minion_rolls    = [e for e in roll_events if is_minion(e)]
    gm_rolls        = [e for e in roll_events if is_gm(e)]
    per_player_rolls = {}
    for e in party_rolls:
        per_player_rolls.setdefault(e["actor"], []).append(e)
    per_controller_rolls = {}
    for e in minion_rolls:
        ctrl = e.get("controller") or e.get("actor")
        per_controller_rolls.setdefault(ctrl, []).append(e)

    # ── Damage dealt (damage-roll events, split healing vs damage) ────────────
    def is_healing_roll(evt):
        """Healing spell — damage-roll event where dice flavor contains 'heal'."""
        roll = evt.get("roll") or {}
        if not isinstance(roll, dict):
            return False
        return any("heal" in (d.get("flavor") or "").lower() for d in (roll.get("dice") or []))

    def dmg_stat(evts):
        totals = [e["roll"]["total"] for e in evts
                  if isinstance(e.get("roll"), dict) and e["roll"].get("total") is not None]
        if not totals:
            return {"total": 0, "hits": 0, "avg_per_hit": 0, "high": 0}
        n = len(totals)
        return {
            "total":       sum(totals),
            "hits":        n,
            "avg_per_hit": round(sum(totals) / n, 1),
            "high":        max(totals),
        }

    dmg_roll_events = [e for e in active if e.get("event_type") == "damage-roll" and not e.get("gm_secret")]
    # Healing can arrive two ways: reclassified "healing" events (negative hp delta)
    # or damage-roll events with heal-flavored dice (healing spells).
    heal_roll_events = [e for e in dmg_roll_events if is_healing_roll(e)]
    dmg_events       = [e for e in dmg_roll_events if not is_healing_roll(e)]
    heal_hp_events   = [e for e in active if e.get("event_type") == "healing"]

    party_dmg        = [e for e in dmg_events if is_party(e)]
    minion_dmg       = [e for e in dmg_events if is_minion(e)]
    gm_dmg           = [e for e in dmg_events if is_gm(e)]
    per_player_dmg   = {}
    for e in party_dmg:
        per_player_dmg.setdefault(e["actor"], []).append(e)
    per_controller_dmg = {}
    for e in minion_dmg:
        ctrl = e.get("controller") or e.get("actor")
        per_controller_dmg.setdefault(ctrl, []).append(e)

    # Healing stat uses roll.total for spell heals, net_heal for hp-delta heals
    def heal_stat(roll_evts, hp_evts):
        totals = [e["roll"]["total"] for e in roll_evts
                  if isinstance(e.get("roll"), dict) and e["roll"].get("total") is not None]
        totals += [e["net_heal"] for e in hp_evts if e.get("net_heal") is not None]
        if not totals:
            return {"total": 0, "events": 0, "avg": 0, "high": 0}
        return {
            "total":  sum(totals),
            "events": len(totals),
            "avg":    round(sum(totals) / len(totals), 1),
            "high":   max(totals),
        }

    party_heal_roll  = [e for e in heal_roll_events if is_party(e)]
    gm_heal_roll     = [e for e in heal_roll_events if is_gm(e)]
    party_heal_hp    = [e for e in heal_hp_events if is_party(e)]
    gm_heal_hp       = [e for e in heal_hp_events if is_gm(e)]
    per_player_heal  = {}
    for e in party_heal_roll + party_heal_hp:
        per_player_heal.setdefault(e["actor"], {"roll": [], "hp": []})
    for e in party_heal_roll:
        per_player_heal[e["actor"]]["roll"].append(e)
    for e in party_heal_hp:
        per_player_heal[e["actor"]]["hp"].append(e)

    # ── Damage taken (by party vs by enemies) ─────────────────────────────────
    taken_events = [e for e in active if e.get("event_type") == "damage-taken"]

    def taken_stat(evts):
        totals = [e["net_damage"] for e in evts if e.get("net_damage") is not None]
        if not totals:
            return {"total": 0, "hits": 0, "avg_per_hit": 0, "high": 0}
        n = len(totals)
        return {
            "total":       sum(totals),
            "hits":        n,
            "avg_per_hit": round(sum(totals) / n, 1),
            "high":        max(totals),
        }

    # ── Mitigation ────────────────────────────────────────────────────────────
    def mitig_stat(evts):
        resistance  = sum(e.get("resistance_absorbed", 0) for e in evts)
        weakness    = sum(e.get("weakness_triggered",  0) for e in evts)
        temp_hp     = sum(e.get("temp_hp_absorbed",    0) for e in evts)
        shield      = sum(e.get("shield_absorbed",     0) for e in evts)
        immunities  = sum(1 for e in evts if e.get("immunity_triggered"))
        net         = resistance - weakness + temp_hp + shield
        return {
            "resistance_absorbed": resistance,
            "weakness_triggered":  weakness,
            "temp_hp_absorbed":    temp_hp,
            "shield_absorbed":     shield,
            "immunity_triggers":   immunities,
            "net_mitigation":      net,
        }

    all_taken       = [e for e in active if e.get("event_type") in ("damage-taken", "healing")]
    party_taken_m   = [e for e in all_taken if is_party(e)]
    enemy_taken_m   = [e for e in all_taken if not is_party(e)]
    per_player_mitig = {}
    for e in party_taken_m:
        per_player_mitig.setdefault(e["actor"], []).append(e)

    # ── Saving throws ─────────────────────────────────────────────────────────
    _OUTCOME_KEY = {
        "criticalSuccess": "crit_success",
        "success":         "success",
        "failure":         "failure",
        "criticalFailure": "crit_fail",
    }

    def save_stat(evts):
        counts = {"count": len(evts), "crit_success": 0, "success": 0, "failure": 0, "crit_fail": 0}
        for e in evts:
            k = _OUTCOME_KEY.get(e.get("outcome"))
            if k:
                counts[k] += 1
        return counts

    save_events      = [e for e in active if e.get("event_type") == "saving-throw"]
    party_saves      = [e for e in save_events if is_party(e)]
    enemy_saves      = [e for e in save_events if not is_party(e) and not e.get("gm_secret")]
    per_player_saves = {}
    for e in party_saves:
        per_player_saves.setdefault(e["actor"], []).append(e)
    per_enemy_saves = {}
    for e in enemy_saves:
        per_enemy_saves.setdefault(e["actor"], []).append(e)

    # ── Combat encounters ─────────────────────────────────────────────────────
    combat_events = [e for e in active if e.get("round") is not None]
    encounters, current_enc, last_round = [], None, None

    for e in combat_events:
        r, ts = e.get("round"), e.get("ts")
        if current_enc is None:
            current_enc = {"started_at": ts, "max_round": r, "evts": [e]}
        elif r < last_round:
            encounters.append(current_enc)
            current_enc = {"started_at": ts, "max_round": r, "evts": [e]}
        else:
            current_enc["max_round"] = max(current_enc["max_round"], r)
            current_enc["evts"].append(e)
        last_round = r
    if current_enc:
        encounters.append(current_enc)

    def enc_avg_turn_time(enc_evts):
        seen = {}
        for e in enc_evts:
            key = (e.get("round"), e.get("turn"))
            if None not in key and key not in seen:
                seen[key] = e.get("ts")
        times = sorted(t for t in (_parse_ts(v) for v in seen.values()) if t)
        if len(times) < 2:
            return None
        gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        return round(sorted(gaps)[len(gaps) // 2])  # median gap

    enc_detail = []
    for enc in encounters:
        enc_detail.append({
            "started_at":            enc["started_at"],
            "rounds":                enc["max_round"],
            "avg_turn_time_seconds": enc_avg_turn_time(enc["evts"]),
        })

    total_rounds  = sum(e["max_round"] for e in encounters)
    att_values    = [d["avg_turn_time_seconds"] for d in enc_detail if d["avg_turn_time_seconds"] is not None]
    overall_att   = round(sum(att_values) / len(att_values)) if att_values else None

    spell_events    = [e for e in active if e.get("event_type") == "spell-cast"]
    spells_by_actor = {}
    for e in spell_events:
        a = e.get("actor") or "?"
        spells_by_actor[a] = spells_by_actor.get(a, 0) + 1

    # ── Session-level ─────────────────────────────────────────────────────────
    type_counts  = {}
    actor_counts = {}
    for e in active:
        t = e.get("event_type") or "unknown"
        type_counts[t]  = type_counts.get(t, 0) + 1
        a = e.get("actor") or "?"
        actor_counts[a] = actor_counts.get(a, 0) + 1

    most_active = max(actor_counts, key=actor_counts.get) if actor_counts else None

    all_ts = sorted(t for t in (_parse_ts(e.get("ts")) for e in active) if t)
    duration_min = round((all_ts[-1] - all_ts[0]).total_seconds() / 60) if len(all_ts) >= 2 else None

    _COMBAT_TYPES = {"attack-roll", "damage-roll", "saving-throw", "damage-taken", "spell-cast", "ability-posted", "action"}

    # ── Deletion classification ────────────────────────────────────────────────
    # Group deleted events by deleted_at within a 1-second window.
    # Events in the same window = bulk "Clear Chat Log"; lone events = individual deletions.
    deleted_events = sorted(
        (e for e in events if e.get("deleted") and e.get("deleted_at")),
        key=lambda e: e["deleted_at"],
    )
    chat_clears        = []
    individual_deletes = 0
    if deleted_events:
        group_start_str = deleted_events[0]["deleted_at"]
        group_start_dt  = _parse_iso(group_start_str)
        group           = [deleted_events[0]]
        for e in deleted_events[1:]:
            dt = _parse_iso(e["deleted_at"])
            if dt and group_start_dt and (dt - group_start_dt).total_seconds() <= 1:
                group.append(e)
            else:
                if len(group) > 1:
                    chat_clears.append({"time": group_start_str, "count": len(group)})
                else:
                    individual_deletes += 1
                group_start_str = e["deleted_at"]
                group_start_dt  = dt
                group           = [e]
        if len(group) > 1:
            chat_clears.append({"time": group_start_str, "count": len(group)})
        else:
            individual_deletes += 1

    return {
        "session": {
            "duration_minutes":    duration_min,
            "total_events":        len(active),
            "events_by_type":      type_counts,
            "gm_secret_count":     sum(1 for e in events if e.get("gm_secret")),
            "deleted_count":       sum(1 for e in events if e.get("deleted")),
            "individual_deletes":  individual_deletes,
            "chat_clears":         chat_clears,
            "most_active_actor":   most_active,
            "combat_events":       sum(1 for e in active if e.get("event_type") in _COMBAT_TYPES),
            "chat_events":         type_counts.get("chat", 0),
        },
        "rolls": {
            "party":            roll_stat(party_rolls),
            "minions":          roll_stat(minion_rolls),
            "gm":               roll_stat(gm_rolls),
            "per_player":       {name: roll_stat(evts) for name, evts in per_player_rolls.items()},
            "per_controller":   {ctrl: roll_stat(evts) for ctrl, evts in per_controller_rolls.items()},
        },
        "damage": {
            "party":            dmg_stat(party_dmg),
            "minions":          dmg_stat(minion_dmg),
            "gm":               dmg_stat(gm_dmg),
            "per_player":       {name: dmg_stat(evts) for name, evts in per_player_dmg.items()},
            "per_controller":   {ctrl: dmg_stat(evts) for ctrl, evts in per_controller_dmg.items()},
        },
        "damage_taken": {
            "by_party":   taken_stat([e for e in taken_events if is_party(e)]),
            "by_enemies": taken_stat([e for e in taken_events if not is_party(e)]),
        },
        "healing": {
            "party":      heal_stat(party_heal_roll, party_heal_hp),
            "gm":         heal_stat(gm_heal_roll, gm_heal_hp),
            "per_player": {
                name: heal_stat(buckets["roll"], buckets["hp"])
                for name, buckets in per_player_heal.items()
            },
        },
        "mitigation": {
            "by_party":   mitig_stat(party_taken_m),
            "by_enemies": mitig_stat(enemy_taken_m),
            "per_player": {name: mitig_stat(evts) for name, evts in per_player_mitig.items()},
            "_note": "net_mitigation = resistance_absorbed - weakness_triggered + temp_hp_absorbed + shield_absorbed.",
        },
        "saving_throws": {
            "party":      save_stat(party_saves),
            "per_player": {name: save_stat(evts) for name, evts in per_player_saves.items()},
            "enemies":    save_stat(enemy_saves),
            "per_enemy":  {name: save_stat(evts) for name, evts in per_enemy_saves.items()},
            "_note": "party/per_player outcomes are null in PF2e — system does not write save results back to the PC's roll message; enemy saves have full outcomes",
        },
        "combat": {
            "encounters":               len(encounters),
            "encounters_detail":        enc_detail,
            "total_rounds":             total_rounds,
            "avg_rounds_per_encounter": round(total_rounds / len(encounters), 1) if encounters else 0,
            "avg_turn_time_seconds":    overall_att,
            "spells_cast":              len(spell_events),
            "spells_by_actor":          spells_by_actor,
        },
        "conditions":                   _cond_stats(condition_apps or []),
    }


# =============================================================================
# Condition timeline processing
# =============================================================================

def _parse_iso(s):
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def process_condition_timeline(enemy_conditions, events):
    """
    Diff consecutive enemy_conditions timeline entries to find when conditions
    appeared or escalated on each token.  For each new condition, attribute it
    to the most recent party event that targeted the same token within 10 s.
    Returns a list of condition-application records.
    """
    WINDOW_S = 10

    # Index party events that have a target, sorted chronologically
    party_targeted = []
    for e in events:
        if e.get("actor_role") != "pc":
            continue
        target = e.get("target")
        if not target:
            continue
        t_time = _parse_iso(e.get("timestamp"))
        if t_time is None:
            continue
        party_targeted.append((t_time, e.get("actor"), target))
    party_targeted.sort(key=lambda x: x[0])

    prev_state = {}   # token_id → {slug: condition_dict}
    applications = []

    for entry in enemy_conditions:
        entry_time_str = entry.get("time")
        entry_time     = _parse_iso(entry_time_str)

        current_state = {
            t["token_id"]: {"name": t["name"],
                            "conds": {c["slug"]: c for c in t["conditions"]}}
            for t in entry.get("tokens", [])
        }

        # Detect new or escalated conditions
        new_apps = []
        for tid, state in current_state.items():
            tname = state["name"]
            prev_conds = prev_state.get(tid, {}).get("conds", {})
            for slug, cond in state["conds"].items():
                prev_val = prev_conds[slug].get("value") if slug in prev_conds else None
                if slug not in prev_conds or prev_val != cond.get("value"):
                    new_apps.append({
                        "time":       entry_time_str,
                        "token_id":   tid,
                        "token_name": tname,
                        "slug":       slug,
                        "name":       cond.get("name", slug),
                        "value":      cond.get("value"),
                    })

        # Attribute each new condition to the closest prior party event in window
        for app in new_apps:
            tid   = app["token_id"]
            tname = app["token_name"]
            best_actor = None
            if entry_time:
                for t_time, actor, target in reversed(party_targeted):
                    dt = (entry_time - t_time).total_seconds()
                    if dt < 0:
                        continue          # future event — skip
                    if dt > WINDOW_S:
                        break             # too old — stop
                    t_id_str = target.get("id", "")
                    tok_suffix = t_id_str.split("Token.")[-1] if "Token." in t_id_str else t_id_str
                    if target.get("name") == tname or tok_suffix == tid:
                        best_actor = actor
                        break             # take most recent match
            app["attributed_to"] = best_actor

        applications.extend(new_apps)
        prev_state = current_state

    return applications


# =============================================================================
# Session merge
# =============================================================================

def merge_raw_sessions(paths):
    """Load and merge multiple raw session JSON files into one combined dict."""
    loaded = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            loaded.append((Path(p), json.load(f)))

    loaded.sort(key=lambda x: (x[1].get("session_meta") or {}).get("start_time") or "")

    first_meta = dict((loaded[0][1].get("session_meta") or {}))
    last_meta  = loaded[-1][1].get("session_meta") or {}

    # Messages — dedup by _id, sort by Foundry timestamp
    seen_ids, messages = set(), []
    for _, s in loaded:
        for m in s.get("messages") or []:
            mid = m.get("_id")
            if mid not in seen_ids:
                seen_ids.add(mid)
                messages.append(m)
    messages.sort(key=lambda m: m.get("timestamp") or 0)

    state_timeline = sorted(
        (e for _, s in loaded for e in (s.get("state_timeline") or [])),
        key=lambda e: e.get("timestamp") or "",
    )
    combat_log = [e for _, s in loaded for e in (s.get("combat_log") or [])]
    enemy_conditions = sorted(
        (e for _, s in loaded for e in (s.get("enemy_conditions") or [])),
        key=lambda e: e.get("time") or "",
    )

    first_meta["end_time"]    = last_meta.get("end_time")
    first_meta["end_reason"]  = last_meta.get("end_reason")
    first_meta["merged_from"] = [str(p) for p, _ in loaded]
    first_meta.pop("split_from", None)
    first_meta.pop("split_at",   None)

    return {
        "session_meta":     first_meta,
        "session_start":    loaded[0][1].get("session_start"),
        "session_end":      loaded[-1][1].get("session_end"),
        "messages":         messages,
        "state_timeline":   state_timeline,
        "combat_log":       combat_log,
        "enemy_conditions": enemy_conditions,
    }


# =============================================================================
# Entry point
# =============================================================================

def _distill_raw(raw, output_path):
    """Core distillation pipeline: process a raw session dict, write to output_path."""
    messages = raw.get("messages") or []
    system_version, core_version = lift_versions(messages)
    session_end = raw.get("session_end")

    events       = [distill_message(msg) for msg in messages]
    actor_roster = build_actor_roster(messages, events, session_end)

    enemy_conditions = raw.get("enemy_conditions") or []
    condition_apps   = process_condition_timeline(enemy_conditions, events) if enemy_conditions else []

    output = {
        "distiller_version": DISTILLER_VERSION,
        "session_meta":      raw.get("session_meta"),
        "session_start":     raw.get("session_start"),
        "session_end":       session_end,
        "state_timeline":    raw.get("state_timeline"),
        "combat_log":        raw.get("combat_log"),
        "condition_apps":    condition_apps or None,
        "system_version":    system_version,
        "core_version":      core_version,
        "actor_roster":      actor_roster,
        "session_stats":     build_stats(events, session_end, condition_apps=condition_apps),
        "events":            events,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    return events


def distill(input_path, output_path=None):
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.with_name(input_path.stem + "_distilled.json")
    else:
        output_path = Path(output_path)

    with open(input_path, encoding="utf-8") as f:
        raw = json.load(f)

    events    = _distill_raw(raw, output_path)
    raw_size  = input_path.stat().st_size
    dist_size = Path(output_path).stat().st_size
    pct       = (1 - dist_size / raw_size) * 100 if raw_size else 0
    gm_secret = sum(1 for e in events if e.get("gm_secret"))
    deleted   = sum(1 for e in events if e.get("deleted"))

    print(f"Input:     {input_path.name}  ({raw_size:,} bytes)")
    print(f"Output:    {output_path.name}  ({dist_size:,} bytes)")
    print(f"Reduction: {pct:.1f}%  |  {len(events)} events  |  {gm_secret} gm_secret  |  {deleted} deleted")
    print(f"Written:   {output_path}")


def distill_merged(paths, output_path=None):
    """Merge multiple raw session files and distill the combined result."""
    paths = [Path(p) for p in paths]
    if output_path is None:
        output_path = paths[0].with_name(paths[0].stem + "_distilled.json")
    else:
        output_path = Path(output_path)

    raw       = merge_raw_sessions(paths)
    events    = _distill_raw(raw, output_path)
    total_raw = sum(p.stat().st_size for p in paths)
    dist_size = output_path.stat().st_size
    pct       = (1 - dist_size / total_raw) * 100 if total_raw else 0
    gm_secret = sum(1 for e in events if e.get("gm_secret"))
    deleted   = sum(1 for e in events if e.get("deleted"))

    print(f"Merged:    {len(paths)} files  ({total_raw:,} bytes total)")
    print(f"Output:    {output_path.name}  ({dist_size:,} bytes)")
    print(f"Reduction: {pct:.1f}%  |  {len(events)} events  |  {gm_secret} gm_secret  |  {deleted} deleted")
    print(f"Written:   {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python distiller.py <input.json> [output.json]")
        print("       python distiller.py <file1.json> <file2.json> ...  # merge split sessions")
        sys.exit(1)
    if len(sys.argv) == 2:
        distill(sys.argv[1])
    elif len(sys.argv) == 3 and not sys.argv[2].endswith(".json"):
        distill(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[2].endswith("_distilled.json"):
        distill(sys.argv[1], sys.argv[2])
    else:
        distill_merged(sys.argv[1:])
