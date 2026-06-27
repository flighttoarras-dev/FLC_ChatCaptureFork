# Chronicler / FLC_ChatCapture — Development Primer

This document is for anyone picking up this codebase fresh: contributors, forks, friends, and AI assistants continuing development. It answers "what is this, what does it do, what's built, and where do I touch things."

---

## What this is

A Tauri v2 desktop app (Windows) that wraps Foundry VTT in a WebView2 window and silently captures the session to structured JSON. The app is a fork of [FLC](https://github.com/phenomen/flc); the capture logic is entirely new.

The captured JSON is the source of truth for the **Chronicler** system — a personal TTRPG session tracking pipeline covering session summaries, PC dossiers, party stats, and voice transcription.

---

## Repository structure

```
FLC/
├── src/                          upstream Svelte frontend (unmodified)
├── src-tauri/
│   ├── src/
│   │   ├── lib.rs                ← MODIFIED: adds CDP port + launches capture.py
│   │   └── capture_launcher.rs  ← NEW: Rust module to spawn/kill capture.py
│   └── tauri.conf.json           ← MODIFIED: bundles capture.py + disables auto-update
├── capture.py                   ← NEW: main capture script (CDP polling → session JSON)
├── capture_config.json          ← NEW: runtime config for capture.py
├── distiller.py                 ← NEW: post-processing script (raw JSON → distilled JSON)
├── README.md
├── CHANGES.md
└── PRIMER.md                    ← this file
```

---

## How it works

```
Foundry VTT (browser)
    ↓  Chrome DevTools Protocol (localhost:9222)
capture.py  ←→  CDP polling every N seconds
    ↓  atomic JSON write after each poll
session JSON  (Documents\FLC Captures\...)
    ↓  manual run
distiller.py
    ↓
distilled JSON  (same folder, _distilled.json suffix)
    ↓  future / separate repos
Postgres + pgvector  |  Notion  |  AI agent  |  Voice pipeline
```

### lib.rs changes (minimal)
One added line exposes the CDP debug port:
```
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9222"
```
Two added calls in `run()` spawn capture.py on startup and kill it on exit via `capture_launcher.rs`.

### capture.py
Polls Foundry's JS runtime via CDP. Each cycle:
1. `game.ready` — skip if not loaded
2. `JS_META` — world title, system, user → create/continue Session
3. `JS_MESSAGES` — all chat messages, dedup by `_id`
4. `JS_WEALTH_PC`, `JS_WEALTH_PARTY` — wealth snapshot
5. `JS_SCENE` + `JS_PARTY` — party state timeline (PF2e only, if enabled)
6. `JS_ENEMY_CONDITIONS` — enemy condition timeline (PF2e, if enabled)
7. `JS_COMBAT` — combat tracker (any system, if enabled)
8. `JS_END_DATA` — character snapshot cached for end-of-session
9. Atomic write to disk

### distiller.py
Three-pass pipeline over a raw session JSON:
- **Pass 1** — `distill_message()` per message: extracts event_type, actor, target, item, outcome, roll, dc, summary, mitigation, timestamp
- **Pass 1b/2** — `build_actor_roster()`: classifies actors as `pc | companion | eidolon | familiar | summon | npc`, resolves `controller` for minions via author→PC mapping. Backfills `actor_role` and `controller` onto events.
- **Pass 3** — `process_condition_timeline()`: diffs enemy condition timeline entries, attributes new conditions to the nearest prior party roll targeting the same token (±10 s window)

Then `build_stats()` computes `session_stats` from distilled events.

---

## Architecture boundary

**distiller.py is the only PF2e-aware component.** Everything downstream consumes the distilled shape only. Swapping distiller.py for a dnd5e_distiller.py is the full cost of adding a new system.

**Litmus test for distiller output fields:** Would this field name make sense to a D&D 5e distiller? If no, normalise or drop it.

**event_type** is a closed, system-agnostic enum:
`attack-roll | saving-throw | damage-roll | damage-taken | healing | spell-cast | ability-posted | action | chat`

---

## Current implementation status

| Feature | Status | Notes |
|---------|--------|-------|
| Chat capture + dedup | ✅ Tested | Core feature, several sessions of data |
| Party state timeline (PF2e) | ✅ Tested | HP, conditions, hero points, saves, skills |
| Wealth tracking | ✅ Tested | Per-PC + party stash |
| World-change split boundaries | ✅ Tested | Used as audio-split signal for voice pipeline |
| Distiller: event classification | ✅ Tested | attack-roll, damage-roll, saving-throw, spell, etc. |
| Distiller: actor roster | ✅ Tested | PC/NPC/minion classification, controller resolution |
| Distiller: session stats | ✅ Tested | rolls, damage, healing, mitigation, saves, combat |
| Distiller: target {name, id} | ✅ Tested | Both display name and UUID preserved |
| Distiller: full item names | ✅ Tested | h4 flavor → origin.name → h3 → slug priority |
| Damage mitigation (IWR, shield, temp HP) | ✅ Tested | resistance_absorbed, shield_absorbed, etc. |
| Per-game subfolders | ✅ Built, untested in production | `use_game_subfolders: false` |
| Game schedule labels | ✅ Built, untested | `game_schedules: {}` |
| Combat tracker | ✅ Built, untested | `capture_combat: true` — **first priority to verify** |
| Enemy condition timeline | ✅ Built, untested | `capture_enemy_conditions: false` — off by default |
| Condition attribution stats | ✅ Built, untested | `condition_apps` in distilled output |
| distiller.py in MSI | ❌ Not done | Currently manual script only |
| SIGTERM grace period | ❌ Not done | capture.py killed without warning on FLC close |
| Condition → outcome analysis | ❌ Deferred | Needs token AC/save capture first |

---

## Key files and what to touch

### Adding a config option
1. Add to `capture_config.json` with the default value
2. Read it in `poll_once()` at the top with `config.get("key", default)`
3. Update the config table in `README.md` and `CHANGES.md`

### Adding a new JS query
1. Add a `JS_SOMETHING = """JSON.stringify(...)"""` constant near the top of `capture.py`
2. Add state to `Session.__init__` if needed
3. Add a `record_something()` method to Session
4. Include the data in `Session.write()`
5. Call it in `poll_once()` guarded by a config flag

### Changing the distilled event schema
Edit `distill_message()` in `distiller.py`. Check the architecture boundary — if the field is PF2e-specific, keep it inside the distiller or drop it; don't let it leak to downstream consumers without a system-agnostic name.

### Adding a stat to session_stats
Add to `build_stats()` in `distiller.py`. The function takes `events` (backfilled with actor_role), `session_end`, and `condition_apps`. All stats should exclude `deleted: true` events.

### Adding support for a new game system
- Add new `JS_*` constants in `capture.py` for the system's party/wealth data
- Add a branch in `poll_once()` alongside the existing PF2e block
- Write a new `yoursystem_distiller.py` (or extend distiller.py with a system branch)
- The output schema contract does not change

---

## Things that trip up new contributors

**`game.combat.combatants.contents` (not `.entries`)** — Foundry uses a Collection class whose iterable is `.contents`. Older Foundry versions may differ.

**PF2e saving throw outcomes are null on the PC's roll** — PF2e does not write the outcome back to the character's saving throw message. Only enemy saves (where the GM has applied the result) have outcomes. This is documented in `session_stats.saving_throws._note`.

**Author IDs and controller resolution** — The GM playing their own PC shares an author ID with GM rolls. `resolve_controller()` in `build_actor_roster()` is restricted to minion-type actors only (companion, eidolon, familiar, summon). NPCs never get a controller even if the author matches a PC.

**Sparse timelines** — Both `state_timeline` (party) and `enemy_conditions` are change-only. An entry is only appended when the fingerprint changes. If nothing changes between polls, nothing is written. This means gaps represent "no change," not "data missing."

**Poll timing** — All timestamps are ±5 s accurate (the poll interval). The `time` field on combat events and condition entries reflects when the poll detected the change, not when it happened.

**Session end race condition** — FLC kills capture.py with no grace period. The final CDP query may not complete. End-of-session data (wealth, characters) is cached on *every* poll cycle so the last successful poll's data is always written. See `session.cache_end_data()`.

**Atomic writes** — `safe_write()` writes to a `.tmp` file and uses `os.replace()` to swap it in. This prevents corrupt JSON if the process is killed mid-write.

---

## Tested campaigns (as of 2026-06-27)

- **Fist of the Ruby Phoenix (PF2e)** — primary test campaign, several sessions captured
- **Kingmaker (PF2e)** — secondary test, used to verify actor roster and shield block

Combat tracker and enemy conditions are built but have not yet been tested against a live session.

---

## What's genuinely next

In rough priority order:
1. **Test combat tracker** in a real session — check `combat_log` in raw JSON; verify `game.combat.combatants.contents` works in this PF2e version
2. **Test enemy conditions** — flip `capture_enemy_conditions: true`, run a session, check `enemy_conditions` and `condition_apps` in distilled output
3. **Bundle distiller.py in MSI** — include in `tauri.conf.json` bundle resources, add a post-session auto-run option
4. **Token AC/save capture** — prerequisite for condition → outcome analysis
5. **SIGTERM grace period** — add a short delay in `capture_launcher.rs` between SIGTERM and SIGKILL
