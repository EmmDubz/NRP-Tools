# NRP-Tools

Public utilities for **nation-style roleplay** and related map workflows. This repository is a **monorepo**: two independent tools live in separate folders; each has its own README and dependencies.

## What’s included

| Folder | What it does |
|--------|----------------|
| **[`NationRP Helper Bot/`](NationRP%20Helper%20Bot/README.md)** | Discord application: resolutions, voting, RP calendar time, DM reminders, optional HTTP API. |
| **[`resource_overlay/`](resource_overlay/README.md)** | Desktop tooling: align political and resource maps, tune legends, run pixel analysis, export reports. |

## Requirements

- **Git** (to clone).
- **Python 3.10+** on the machine where you run each tool (see each subfolder’s README).
- On Windows, the resource overlay includes **`.bat`** launchers; other platforms can use the same Python commands documented in that README.

## Getting started

1. **Clone** this repository (replace the URL with yours or the upstream you use):

   ```bash
   git clone https://github.com/<owner>/NRP-Tools.git
   cd NRP-Tools
   ```

2. Open the folder for the tool you need and follow **that folder’s `README.md`** end to end (virtual environment, `pip install`, copying `config.example.*` → local config, and `.env.example` → `.env` for the bot).

3. **Do not commit secrets or machine-local files.** The root [`.gitignore`](.gitignore) excludes common cases (`.env`, local `config.json` / `config.yaml`, databases, Python caches, overlay `output/`). Keeping `.gitignore` in the repository is **normal and recommended** so every clone behaves the same.

## Contributing or forking

Use issues or pull requests on GitHub if you extend a tool and want to share changes. Fork the repository if you are adapting it for another community.

## License

Unless otherwise noted in a subfolder, add or choose a **LICENSE** file at the repository root when you publish; downstream users then know what they may do with the code.
