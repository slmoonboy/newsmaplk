# Rare UI, MCP and agent tooling

LankaPulse uses three optional layers of design/agent tooling. None of them is required at
runtime, and none introduces a paid API, account or API key.

| Layer | What it provides | Where it lives |
| --- | --- | --- |
| **Skill Issue MCP** | Searchable catalogue of agent skills (design, frontend, motion, testing) | `opencode.json` |
| **Installed agent skills** | Local `SKILL.md` instructions used while building/reviewing the UI | `.agents/skills/` (git-ignored), `skills-lock.json` |
| **Rare UI registry** | Metadata index of rare shadcn-style components and interaction patterns | `scripts/rare_registry.py`, `data/rare-ui.db` (git-ignored) |

The shipped UI is still **plain HTML, CSS and JavaScript with no build step**. Rare UI is a
React/Tailwind/Motion/shadcn registry; its components were **not vendored**. The patterns were
re-implemented in vanilla JS/CSS for this project.

---

## 1. Skill Issue MCP

The MCP endpoint is a **skill catalogue**, not a news or map data source:

```bash
opencode mcp add skill-issue --url https://skill-issue.in/api/mcp
opencode mcp list
# ✓ skill-issue  connected
```

The generated project configuration is:

```json
{
  "mcp": {
    "servers": {
      "skill-issue": {
        "type": "remote",
        "url": "https://skill-issue.in/api/mcp"
      }
    }
  }
}
```

Available tools:

| Tool | Purpose |
| --- | --- |
| `list_domains` | Browse every domain and niche in the catalogue |
| `recommend_skills` | Rank skills against a plain-language project description |
| `search_skills` | Fuzzy keyword search across names, use cases and creators |
| `list_domain_skills` | List the skills in one domain |
| `get_skill` | Full record, source link and install command for one skill |

The MCP server is remote and read-only. It stores no project data and needs no credentials.

---

## 2. Installed agent skills

Twenty skills were installed into the project with the `skills` CLI:

```bash
npx skills add anthropics/skills --skill frontend-design -a opencode -y
npx skills add Leonxlnx/taste-skill -a opencode -y
npx skills add emilkowalski/skills -a opencode -y
npx skills add jakubkrehel/make-interfaces-feel-better -a opencode -y
npx skills add vercel-labs/agent-skills --skill web-design-guidelines -a opencode -y
npx skills add addyosmani/web-quality-skills -a opencode -y
```

Installed set:

| Area | Skills |
| --- | --- |
| Visual design / taste | `frontend-design`, `design-taste-frontend`, `emil-design-eng`, `make-interfaces-feel-better`, `web-design-guidelines` |
| Motion | `animate`, `apple-design`, `animation-vocabulary`, `animation-performance`, `improve-animations`, `find-animation-opportunities`, `review-animations` |
| Quality / audit | `accessibility`, `best-practices`, `core-web-vitals`, `performance`, `seo`, `web-quality-audit`, `break-ui` |
| Mobile | `mobile-native` |

They are discovered by OpenCode from `.agents/skills/`. The `.agents/` directory is
git-ignored because it is a local tool cache; `skills-lock.json` records the sources and
content hashes so the set can be restored:

```bash
npx skills experimental_install   # restore from skills-lock.json
```

The skills are build-time guidance only. Removing `.agents/` does not affect the app.

---

## 3. Rare UI registry index

Rare UI publishes a machine-readable registry:

* Registry: <https://raw.githubusercontent.com/swamimalode07/rare-ui/main/public/r/registry.json>
* Item example: <https://raw.githubusercontent.com/swamimalode07/rare-ui/main/public/r/animated-counter.json>
* Site: <https://rareui.com>
* Source: <https://github.com/swamimalode07/rare-ui>

`scripts/rare_registry.py` downloads the registry and stores **metadata only** in SQLite:
name, title, type, description, dependencies, file paths, docs URL and install command. It
never stores component source code and has no third-party Python dependencies.

```bash
python scripts/rare_registry.py sync          # download + rebuild the index
python scripts/rare_registry.py stats         # counts, licence, sync time
python scripts/rare_registry.py list          # all components
python scripts/rare_registry.py list --type registry:ui
python scripts/rare_registry.py search motion
python scripts/rare_registry.py show animated-counter
python scripts/rare_registry.py show animated-counter --json
```

The database is `data/rare-ui.db`, alongside the application database and git-ignored like
`data/`. Syncing again replaces the component rows and updates the metadata.

Current index: **29 items** (28 `registry:ui`, 1 `registry:lib`).

### Patterns adapted into LankaPulse

| Rare UI pattern | LankaPulse use | Vanilla implementation |
| --- | --- | --- |
| Animated Counter | Header `Events 24h` / `Districts` stat chips | Digit-wheel odometer; each wheel is a translated column of 0–9 faces with a `760ms` ease-out transition |
| Notification Bell | Header bell + unread badge + new-event popover | Live WebSocket `event_update` messages feed a capped list; opening the bell clears the badge; items open the event drawer |
| Gooey Nav | Category filter chips | A single absolutely positioned `.chip-glide` pill measures the active chip and glides between rows |
| Scroll Progress | Top of the live feed panel | `scaleX()` progress bar driven by `scrollTop / (scrollHeight - clientHeight)` |
| Step Player | `How It Happened` timeline in the drawer | Play/pause/replay reveals timeline steps with a progress track; clicking a step jumps to it |

All five implementations live in `frontend/app.js` and `frontend/styles.css`. They use the
existing monochrome + gold tokens, respect `prefers-reduced-motion`, and have no framework,
runtime dependency or build step.

---

## 4. Licence and attribution

Rare UI is licensed **MIT + Commons Clause 1.0 + Attribution**. The Commons Clause forbids
selling, sublicensing or redistributing the components themselves, including as a ported
version. This repository therefore:

1. does **not** vendor, bundle or redistribute Rare UI component source;
2. stores only registry metadata in a local, git-ignored database;
3. credits Rare UI with a visible link.

The visible credit appears in two places:

* the feed panel footer — **“UI patterns by Rare UI · source”**;
* the MapLibre attribution control — **“UI patterns: Rare UI”** linking to
  <https://rareui.com>.

Keep both links when redistributing or deploying the UI. The upstream copyright notice is:

> Copyright (c) 2026 Swami Malode

See the upstream [`LICENSE`](https://github.com/swamimalode07/rare-ui/blob/main/LICENSE) for
the full text. No Rare UI code is covered by LankaPulse's MIT licence.
