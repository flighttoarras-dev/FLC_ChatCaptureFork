# FLC — ChatCapture Fork

A fork of [FLC (Foundry Lightweight Client)](https://github.com/phenomen/flc) extended with automatic session capture for the **Chronicler** TTRPG session tracking system.

> **Personal passion project.** This fork is maintained on a best-effort basis and will receive infrequent updates. Issues and PRs are welcome but response time is not guaranteed.

---

## What this is

FLC is a lightweight desktop client for Foundry VTT. This fork adds a companion script (`capture.py`) that silently polls the running Foundry game via Chrome DevTools Protocol and writes structured JSON session logs to your local machine — no Foundry modules, no server-side changes required.

This fork is designed as part of **Chronicler**, a TTRPG session tracking system. All components after the first are optional:

- **Chat + game state** (this repo) — runs standalone, no other components required
- **Voice transcription** (separate, optional) — Discord bot captures per-speaker audio, split by session boundary, transcribed with per-speaker attribution, mergeable with chat logs by timestamp
- **AI agent** (separate, optional) — session summaries, PC dossiers, and party stats generated via GPT, stored in a local Postgres+pgvector database; Notion as optional display layer
- **Reference library** (separate, optional) — RAG pipeline ingesting rulebooks, adventure path guides, and wiki sources (e.g. Archives of Nethys) for AI agent context

A shared **primer config** (player-to-PC name mapping and campaign metadata) is used across all Chronicler components.

---

## What gets captured

### Any game system
- All chat messages — timestamp (UTC), speaker, flavor text, roll results
- Scene/map name at time of each message
- World/game changes — recorded as discrete split boundaries with UTC timestamps (used by the voice pipeline to know where to cut audio)
- Combat encounters — initiative order at start (including hidden combatants), round progression, combatant defeats, encounter duration (`capture_combat: true` by default)

### Pathfinder 2e only
- Party member state over time: HP, stamina, hero points, active conditions, saving throw modifiers, skill proficiencies and modifiers — recorded as a sparse change-only timeline
- Session start and end wealth: coins per PC and party stash total
- Enemy condition timeline — sparse log of which conditions were on which enemy tokens and when, for downstream condition-application stats (`capture_enemy_conditions: false` by default)

Party data capture can be disabled entirely by setting `party_data_system` to `"none"` in `capture_config.json`. Chat capture is unaffected.

Private GM rolls are excluded by default (configurable).

---

## Requirements

- Windows 10/11 x64
- [Python 3.8+](https://python.org) installed and on PATH
- A running Foundry VTT server (local or remote)
- Pathfinder 2e (PF2e) system (other systems: party data capture will be skipped; chat capture still works)

---

## Installation

1. Download `FLC_7.9.2_x64_en-US.msi` from the releases page
2. Run the installer — it will replace any existing stock FLC installation
3. Your saved servers carry over automatically
4. Launch FLC from the Start Menu — `capture.py` starts automatically in the background

Session files appear in `Documents\FLC Captures\` after you connect to a Foundry game.

> **Update notifications:** FLC may display an "update available" notification. This refers to the upstream FLC release, not this fork. You can safely ignore it.

---

## Configuration

Edit `capture_config.json` in the FLC install directory (usually `C:\Program Files\FLC\`):

| Field | Default | Description |
|-------|---------|-------------|
| `poll_interval_seconds` | `5` | How often to query Foundry |
| `debug_port` | `9222` | CDP port — must match the value in lib.rs |
| `log_directory` | `%USERPROFILE%\Documents\FLC Captures` | Where session JSON files are written |
| `log_file` | `%USERPROFILE%\Documents\FLC Captures\capture_log.txt` | capture.py log |
| `capture_private_gm_rolls` | `false` | Include blind/private GM rolls |
| `party_data_system` | `"pf2e"` | Game system for party data. Set to `"none"` to disable |
| `auto_launch` | `true` | Start capture.py automatically with FLC |
| `show_console` | `false` | Show the Python console window (set to `true` for debugging) |
| `capture_combat` | `true` | Record combat encounters in a `combat_log` array (works for any system) |
| `capture_enemy_conditions` | `false` | Record a sparse condition timeline for enemy tokens (PF2e; adds data volume) |
| `use_game_subfolders` | `false` | Organise session files into per-game subfolders within the log directory |
| `game_schedules` | `{}` | Map world title to `{day, start_time}` to enrich filenames and session_meta |

Changes to `capture_config.json` take effect the next time FLC is launched.

---

## Session JSON format

Each session produces a file named `YYYY-MM-DD_HH-MM_WorldTitle.json` in the log directory. If `game_schedules` is configured for the world, the filename becomes `YYYY-MM-DD_DayName_HHMM_WorldTitle.json`.

World changes during a single FLC session produce separate files. The outgoing file records `"end_reason": "world_change"` and `"next_world"` with a precise UTC timestamp; the incoming file records `"split_from"` and `"split_at"` with the same timestamp. This is the audio-split boundary used by the voice pipeline.

**Top-level keys in the session JSON:**

| Key | Description |
|-----|-------------|
| `session_meta` | Title, world ID, system, start/end times, tool version, optional schedule fields |
| `session_start` | Wealth snapshot at session start |
| `session_end` | Wealth + character data at session end (cached on every poll) |
| `messages` | Raw Foundry chat messages (sorted by timestamp) |
| `state_timeline` | Sparse HP/condition timeline for party members |
| `combat_log` | Array of encounter objects (initiative order, rounds, events) |
| `enemy_conditions` | Sparse condition timeline for enemy tokens (when enabled) |

### Distiller

`distiller.py` is a standalone post-processing script that collapses a raw session JSON into a compact distilled file:

```
python distiller.py <session.json>
```

The distilled file adds `session_stats`, `actor_roster`, `condition_apps`, and compressed per-event records. See [PRIMER.md](PRIMER.md) for the full output schema and architecture contract.

---

## Building from source

```
git clone <this repo>
cd FLC
pnpm install
pnpm tauri build
```

Requires Rust, Node.js, and pnpm. See the [Tauri v2 prerequisites](https://tauri.app/start/prerequisites/).

---

## Merging upstream FLC updates

```
git fetch upstream
git merge upstream/main
```

Resolve the expected conflict in `src-tauri/src/lib.rs` (the `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` line and the two launcher calls in `run()`). All other fork-specific code is in isolated files and should merge cleanly. See [CHANGES.md](CHANGES.md) for a full list of what was modified.

---

## Notes for the AI agent consuming this data

These are things that cannot be inferred from the session JSON alone and should be passed to the LLM as context (e.g. via the shared primer config):

- **Not all rolls are meaningful.** Players or GMs may make joke rolls, test rolls, accidental rolls, or out-of-turn rolls that do not reflect actual gameplay. The AI should use surrounding message context and flavor text to assess whether a roll is narratively significant before treating it as a real event.
- **Chat may contain out-of-character messages.** Foundry's chat log mixes in-character roleplay, mechanical output, and casual out-of-character table talk. The AI should not assume all messages are in-character or intentional.
- **Deleted messages are retained but flagged.** If a GM or player deletes a chat message in Foundry, it remains in the session JSON with `"deleted": true` and a `"deleted_at"` UTC timestamp. The AI should treat these with appropriate scepticism — they may have been removed for any reason (mistake, spoiler, out-of-character content). A single deletion is typically intentional removal of a specific message. If many or all messages share the same `deleted_at` timestamp, this indicates the GM used Foundry's "Clear Chat Log" function — the content was not individually curated out, the slate was simply wiped. Messages captured before a clear are still valid session history.
- **Some messages may be accidental or tests.** Players occasionally send messages by mistake, out of turn, or while testing something. Low-context messages with no clear narrative thread should be treated with lower confidence.

---

## What's not yet implemented

- **`distiller.py` not bundled in the MSI** — currently a standalone script you run manually. Future: include in the installer and run automatically after each session.
- **Condition → outcome analysis** — tracking whether a condition (e.g. Frightened, Flat-Footed) caused a miss to become a hit. Requires capturing token AC/save values at each poll cycle, which isn't done yet.
- **SIGTERM grace period** — `capture.py` is killed by FLC with no warning, so the final poll may not complete. A short delay between SIGTERM and SIGKILL would allow a final write on clean shutdown.

See [PRIMER.md](PRIMER.md) for full architecture details and a continuation guide for developers.

---

## Notes for future maintainers

### Adding support for another game system
Party data capture is gated behind `party_data_system` in config. Currently the only supported value is `"pf2e"`. To add a new system:
1. Add new JS expression constants in `capture.py` (see `JS_PARTY`, `JS_WEALTH_PC`, `JS_WEALTH_PARTY`)
2. Add a branch in `poll_once()` alongside the existing `if capture_party and config.get("party_data_system") == "pf2e"` block
3. Add the new system name to the `party_data_system` config docs

### If PF2e data paths break after a system update
All JS expressions that read Foundry's runtime are constants at the top of `capture.py`. If a PF2e update renames a field, find the broken expression there. The paths to check first:
- Wealth: `game.actors.party?.system?.inventory?.coins` (per-PC) and `game.actors.party?.inventory?.coins` (party stash)
- Party members: `game.actors.party?.members`
- HP/SP: `m.system?.attributes?.hp`, `m.system?.attributes?.sp`
- Conditions: `m.conditions?.active`

### Session end race condition
FLC kills `capture.py` when the app closes, which means the final CDP query may never complete. This is handled by caching all end-of-session data (class, ancestry, level, XP, saves, skills, wealth) on **every** poll cycle. On disconnect, the last cached values are written. The cache lives in `session.session_end` on the `Session` object.

### Voice pipeline interface contract
When the GM switches Foundry worlds mid-session, `capture.py` writes the outgoing session file with:
- `session_meta.end_time` — precise UTC timestamp of the detected world change
- `session_meta.end_reason` — `"world_change"`
- `session_meta.next_world` — title of the incoming world

The incoming session file records:
- `session_meta.split_from` — title of the outgoing world
- `session_meta.split_at` — same UTC timestamp as `end_time` above

The voice pipeline should key on `end_reason == "world_change"` to find split boundaries. Regular FLC closes do not set `end_reason` (it remains `null`).

### Known limitations
- `end_time` is `null` when FLC is closed normally (FLC kills `capture.py` before the final write completes). Only world-change boundaries get a precise `end_time`.
- `capture.py` has no SIGTERM grace period — a future improvement could add a short delay between SIGTERM and SIGKILL to allow a final write on clean shutdown.

---

## Disclaimer — LLM-assisted development

The majority of the code in this fork — including `capture.py`, `capture_launcher.rs`, and the configuration system — was written by [Claude](https://claude.ai) (Anthropic) with direction and testing by the project author. The upstream FLC codebase is the work of its original authors and is unmodified except where noted in [CHANGES.md](CHANGES.md).

This project is experimental personal tooling. Use at your own risk.

---

## Credits

- [FLC](https://github.com/phenomen/flc) by phenomen — upstream client this fork is based on
- [Foundry VTT](https://foundryvtt.com) — the VTT platform being captured
- [Pathfinder 2e](https://paizo.com/pathfinder) — the game system targeted by party data capture
- [Tauri](https://tauri.app/) — Rust-powered desktop framework
- [Svelte](https://svelte.dev/) — frontend UI framework (upstream)
