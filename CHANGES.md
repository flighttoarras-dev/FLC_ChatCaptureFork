# CHANGES — FLC_ChatCaptureFork

Changes made to the upstream FLC (Foundry Lightweight Client) repo for the Chronicler / FLC_ChatCapture project.

All paths relative to the repo root (`M:\VSProjects\FLC_ChatCaptureFork\FLC`).

---

## Upstream merge strategy

This fork is **not** intended to be upstreamed. It is a private tooling fork. When upstream FLC releases an update:

1. `git fetch upstream`
2. `git merge upstream/main`
3. Resolve the single expected conflict in `src-tauri/src/lib.rs` (the `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` line and the two launcher calls in `run()`)
4. Rebuild

All fork-specific code lives in isolated files (`capture.py`, `capture_launcher.rs`, `capture_config.json`). The only touches to existing FLC source are in `lib.rs` and `tauri.conf.json`. This minimises merge surface intentionally.

---

## [1.0.0] — 2026-06-25

### Modified — existing FLC source files

**`src-tauri/src/lib.rs`**

- Added `--remote-debugging-port=9222` to `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` so the WebView2 Chrome DevTools Protocol endpoint is exposed on `localhost:9222`.
- Added `mod capture_launcher` and `use std::sync::Arc`.
- Added `capture_launcher::new_handle()`, `.manage()`, `.setup()` to spawn capture.py on startup.
- Changed `.run()` to `.build().expect().run()` with a `RunEvent::Exit` handler that calls `capture_launcher::kill()` for clean shutdown.

**`src-tauri/tauri.conf.json`**

- Added `bundle.resources` to include `capture.py` and `capture_config.json` in the MSI installer so they are deployed alongside `flc.exe`.
- Set `createUpdaterArtifacts: false` — this is a private fork and does not participate in the upstream FLC auto-update channel.

### Added — new files (fork-specific, not upstream)

| File | Purpose |
|------|---------|
| `capture.py` | Companion polling script — CDP polling, session capture, JSON output |
| `capture_config.json` | Runtime config for capture.py and the Rust launcher |
| `src-tauri/src/capture_launcher.rs` | Rust module for spawning/killing capture.py |
| `FLC_CHATCAPTURE_PLAN.md` | Full architecture and implementation plan |
| `CHANGES.md` | This file |

> `test_capture.py` was a throwaway verification script used during development. Not part of the production build.

---

## capture.py — feature summary

- Polls Foundry VTT's JS runtime via Chrome DevTools Protocol (CDP) every N seconds (configurable).
- Captures chat messages with dedup, timestamps (UTC ISO-8601), and whisper filtering.
- Captures party member state (HP, SP, hero points, conditions, saves, skills) as a sparse change-only timeline.
- Captures session start/end wealth (per-PC coins + party stash coins).
- Caches end-of-session data (class, ancestry, level, XP, saves, skills, wealth) on every poll to survive the race condition where FLC kills the page before the final query can run.
- Detects world changes mid-session and writes a closed session file with `end_reason: "world_change"` and `next_world` — a precise audio-split boundary for the downstream voice pipeline.
- Incoming session after a world change records `split_from` and `split_at` (same UTC timestamp as the outgoing `end_time`).
- Atomic file writes (temp file + `os.replace()`) prevent corrupt JSON on crash or kill.
- Logs to `%USERPROFILE%\Documents\FLC Captures\capture_log.txt` by default (configurable).
- Output JSON files written to `%USERPROFILE%\Documents\FLC Captures\` by default (configurable).

## capture_launcher.rs — feature summary

- `find_companion_dir()`: checks Tauri resource dir first (production), then walks up 6 directory levels from the exe (dev mode).
- `read_config()`: reads `auto_launch` and `show_console` from `capture_config.json`. Defaults: `auto_launch=true`, `show_console=false`.
- `spawn()`: checks not already running, finds Python, shows a dialog if Python is missing, spawns capture.py.
- On Windows: applies `CREATE_NO_WINDOW` flag when `show_console` is false — no terminal window appears during normal use.
- `kill()`: kills child process and waits to prevent zombie processes.

## capture_config.json — fields

| Field | Default | Purpose |
|-------|---------|---------|
| `poll_interval_seconds` | `5` | How often to query Foundry |
| `debug_port` | `9222` | CDP port (must match `--remote-debugging-port` in lib.rs) |
| `log_directory` | `%USERPROFILE%\Documents\FLC Captures` | Where session JSON files are written |
| `log_file` | `%USERPROFILE%\Documents\FLC Captures\capture_log.txt` | capture.py log file |
| `capture_private_gm_rolls` | `false` | Include blind/private GM rolls in chat capture |
| `party_data_system` | `"pf2e"` | Game system for party data. Set to `"none"` to disable party capture |
| `auto_launch` | `true` | Whether the Rust launcher starts capture.py on FLC startup |
| `show_console` | `false` | Show the Python console window. Set to `true` for debugging |

---

## Distiller architecture contract

**The distiller is the only PF2e-aware component.** Everything downstream — Postgres schema, pgvector, the AI agent, Notion, GPT — consumes the distilled event shape and never touches a Foundry/PF2e field. Swapping `distiller.py` for a `dnd5e_distiller.py` is the full cost of supporting a new system. Nothing else moves.

### What belongs inside the distiller (PF2e-specific, quarantined)

- `flags.pf2e.*` parsing — `context`, `origin`, `appliedDamage`, slug arrays
- HTML class parsing — `item-card`, `action-content`, `data-applications`
- `rolls[]` escaped-JSON shape and the `flattenDice` walker (Foundry roll-term classes: `Die`, `Grouping`, `ArithmeticExpression`)
- Trait slugs (`self:trait:eidolon`, etc.) and `actor_role` classification
- IWR / `data-applications` mitigation parsing
- `system_version` / `core_version` stamping

### The output contract (system-agnostic — this is the seam)

- `actor`, `controller`, `actor_role`, `event_type`, `outcome`, `summary`
- `roll { total, dice, formula, breakdown }`
- `mitigation` fields: `resistance_absorbed`, `weakness_triggered`, `temp_hp_absorbed`, `shield_absorbed`, `immunity_triggers`, `net_mitigation`
- Grepped timeline fields: `round`, `turn`, `hp_remaining`, `hp_percent`, `init_rank`, `init_roll`

### event_type is a closed generic enum

`attack-roll | saving-throw | damage-roll | damage-taken | healing | spell-cast | ability-posted | action | chat`

These are TTRPG-universal concepts, not PF2e ones. A D&D 5e or SWADE distiller emits the same enum. A new `event_type` value should only be added if it maps cleanly to a cross-system concept.

### Litmus test for new output fields

> Would this field name make sense to a D&D 5e distiller?

If no, it either gets normalised to a generic name or stays as an internal intermediate — it must not appear on the output event object.

**Examples of what NOT to leak into the output schema:**
- `fortitude_dc` → use `dc` (already generic)
- `degreeOfSuccess: 3` (PF2e integer) → use `outcome: "criticalSuccess"` (already normalised)
- `pf2e_origin_uuid` → drop or keep as internal only
- `systemVersion` on individual events → already lifted to file-level `system_version`

### Future: per-system distiller module structure

When adding a second game system, the recommended structure is a thin shared entrypoint with system-specific modules alongside:

```
distiller.py          ← shared CLI entrypoint + output schema
pf2e/classify.py      ← event_type classification
pf2e/rolls.py         ← flattenDice, parse_roll
pf2e/mitigation.py    ← IWR, shield, temp HP
pf2e/actors.py        ← trait grep, actor_role, controller resolution
dnd5e/classify.py     ← future drop-in sibling
```

Downstream code imports the schema shape, never the system-specific modules.

---

## [1.1.0] — 2026-06-27

### Added — `capture.py`

**Per-game subfolders** (`use_game_subfolders: false`)
When enabled, session files are written to `{log_dir}/{title_slug}/` instead of flat into `log_dir`. Subfolder is created automatically on first write. Disabled by default to preserve existing behaviour.

**Per-world schedule labels** (`game_schedules: {}`)
Optional config map from world title to `{day, start_time}`. When matched, session filename becomes `YYYY-MM-DD_DayName_HHMM_Title.json` instead of `YYYY-MM-DD_HH-MM_Title.json`. Adds `scheduled_day` and `scheduled_start` to `session_meta` (null when no match). Entirely optional — omitting the key changes no existing behaviour.

**Combat tracker** (`capture_combat: true`)
Records `game.combat` as a `combat_log` array in the session JSON. Each encounter entry contains: `encounter_id`, `started_at`, `ended_at`, `rounds`, full `initiative_order` (including hidden combatants flagged as such), and an `events` array of round changes, initiative order changes (probable delay), combatant defeats, and combat end. Enabled by default. **System-agnostic** — uses Foundry core fields only, no PF2e data.

**Enemy condition timeline** (`capture_enemy_conditions: false`)
Sparse change-only timeline of status effects on enemy tokens. Each entry records the full condition state (slug, name, graded value) for all enemy tokens that currently have at least one condition. Only appended when the condition set changes. Disabled by default — PF2e-specific and adds data volume. Use in combination with the distiller's `condition_apps` to get per-player condition attribution.

**New config fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `use_game_subfolders` | `false` | Organise session files into per-game subfolders |
| `game_schedules` | `{}` | Map world title to scheduled day/time |
| `capture_combat` | `true` | Track combat encounters in `combat_log` |
| `capture_enemy_conditions` | `false` | Track enemy condition state (PF2e) |

### Added — `distiller.py` (new file)

A standalone post-processing script that takes a raw session JSON and produces a compact distilled file for downstream consumers (Postgres+pgvector, Notion, AI agent).

**Usage:** `python distiller.py <session.json> [output.json]`  
Output defaults to `<session>_distilled.json`. Typical size reduction 60–80%.

**Key features:**
- **Pass 1** — `distill_message()` per chat message: extracts `event_type`, `actor`, `target {name, id}`, `item`, `outcome`, `roll`, `dc`, `summary`, `timestamp`, mitigation fields
- **Pass 1b/2** — `build_actor_roster()`: classifies every actor as `pc | companion | eidolon | familiar | summon | npc`, resolves `controller` for minion-type actors via author→PC mapping
- **Pass 3** — `process_condition_timeline()`: diffs consecutive enemy condition entries to find new/escalated conditions; attributes each to the most recent party roll targeting the same token within 10 s
- **`session_stats`** — pre-computed roll stats, damage dealt/taken, healing, mitigation, saving throws, combat metrics, condition application per player
- **`condition_apps`** — list of condition application records with `attributed_to` actor name

**Distiller architecture contract** (quarantine boundary):
- All PF2e-specific logic (flags.pf2e.*, IWR, trait slugs, actor_role traits, roll term classes) is inside distiller.py
- Output schema is system-agnostic — swapping distiller.py for a dnd5e_distiller.py is the full cost of supporting a new system
- Litmus test: would this field name make sense to a D&D 5e distiller?

**event_type enum (closed, system-agnostic):**  
`attack-roll | saving-throw | damage-roll | damage-taken | healing | spell-cast | ability-posted | action | chat`

**Output keys in distilled JSON:**

| Key | Description |
|-----|-------------|
| `session_meta` | From raw, plus `scheduled_day`/`scheduled_start` if set |
| `session_start` | Wealth at session start |
| `session_end` | Wealth + character data at session end |
| `state_timeline` | Party HP/condition timeline (from raw) |
| `combat_log` | Encounter log (from raw) |
| `condition_apps` | Attributed condition applications (distiller-generated) |
| `system_version` | PF2e system version (lifted from first message) |
| `core_version` | Foundry core version |
| `actor_roster` | All actors seen, with role + controller |
| `session_stats` | Pre-computed stats (see below) |
| `events` | Distilled events array |

**`session_stats` sections:** `session`, `rolls`, `damage`, `damage_taken`, `healing`, `mitigation`, `saving_throws`, `combat`, `conditions`

All stats exclude `deleted: true` events. Rolls/damage/healing split by `party / minions / gm / per_player / per_controller`. Minion rolls attributed to their `controller` PC.

**Bugs fixed during distiller development:**
- Action glyphs (e.g. `<span class="action-glyph">2</span>`) were leaking into item names — stripped before text extraction
- Parenthetical suffixes like `(Critical Hit)` were appearing in item names — stripped from h4 flavor text
- Origin slug was taking priority over h4 flavor HTML for item names — reordered: `origin.name → h4 → h3 → slug`
- Saving throw outcomes for party always null in PF2e (system does not write results back to the PC's roll message) — documented in `_note`; enemy saves have full outcomes
- Healing events misclassified as damage-taken — now detected via `appliedDamage.isHealing` flag (primary) and negative hp delta (fallback)
- GM-plays-PC controller leak — author-based controller resolution was incorrectly assigning NPC enemies to the GM's PC. Fixed by restricting `resolve_controller()` to minion-type actors only

---

## Not yet implemented

- **`distiller.py` in MSI** — currently a standalone script; not bundled in the installer
- **Condition → outcome analysis** — whether a condition caused a miss to become a hit; requires token AC/save capture (not yet implemented)
- **SIGTERM grace period** — `capture.py` is killed by FLC with no warning; a short delay would allow a final write on clean shutdown

---

## Distiller architecture contract (reference)

**The distiller is the only PF2e-aware component.** Downstream consumers (Postgres, pgvector, AI agent, Notion) consume the distilled shape only.

### What belongs inside the distiller (PF2e-specific, quarantined)

- `flags.pf2e.*` parsing — `context`, `origin`, `appliedDamage`, slug arrays
- HTML class parsing — `item-card`, `action-content`, `data-applications`
- `rolls[]` escaped-JSON shape and the `flattenDice` walker (Foundry roll-term classes: `Die`, `Grouping`, `ArithmeticExpression`)
- Trait slugs (`self:trait:eidolon`, etc.) and `actor_role` classification
- IWR / `data-applications` mitigation parsing
- `system_version` / `core_version` stamping

### Future: per-system distiller module structure

When adding a second game system:

```
distiller.py          ← shared CLI entrypoint + output schema
pf2e/classify.py      ← event_type classification
pf2e/rolls.py         ← flattenDice, parse_roll
pf2e/mitigation.py    ← IWR, shield, temp HP
pf2e/actors.py        ← trait grep, actor_role, controller resolution
dnd5e/classify.py     ← future drop-in sibling
```

---

## [1.0.0] — 2026-06-25

Initial release. Capture tool only: CDP polling, chat dedup, party state timeline, wealth tracking, world-change split boundaries.

---

## Upstream baseline

Forked from FLC at the commit present when cloned (June 2026). No upstream changes merged since fork.

---

## Upstream baseline

Forked from FLC at the commit present when cloned (June 2026). No upstream changes merged since fork.
