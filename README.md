# NRP-Tools

Public utilities for **nation-style roleplay** and related map workflows. This repository is a **monorepo**: two independent tools live in separate folders; each has its own README and dependencies.

## What’s included

| Folder | What it does |
|--------|----------------|
| **[`NationRP Helper Bot/`](NationRP%20Helper%20Bot/README.md)** | Discord application: resolutions, voting, RP calendar time, DM reminders, optional HTTP API. |
| **[`NationRP Resource & EEZ Calculator/`](NationRP%20Resource%20%26%20EEZ%20Calculator/README.md)** | Map overlay: nation land masks, commodity ΔE matching, offshore **EEZ-style halo**, CSV/JSON and optional markdown reports. |

## Requirements

- **Git** (to clone).
- **Python 3.10+** on the machine where you run each tool (see each subfolder’s README).
- On Windows, **NationRP Resource & EEZ Calculator** includes **`.bat`** launchers; other platforms can use the same Python commands documented in that tool’s README.

## Getting started

1. **Clone** this repository (replace the URL with yours or the upstream you use):

   ```bash
   git clone https://github.com/<owner>/NRP-Tools.git
   cd NRP-Tools
   ```

2. Open the folder for the tool you need and follow **that folder’s `README.md`** end to end (virtual environment, `pip install`, copying `config.example.*` → local config, and `.env.example` → `.env` for the bot).

3. **Do not commit secrets or machine-local files.** The root [`.gitignore`](.gitignore) excludes common cases (`.env`, local `config.json` / `config.yaml`, databases, Python caches, **`NationRP Resource & EEZ Calculator/output/`**). Keeping `.gitignore` in the repository is **normal and recommended** so every clone behaves the same.
