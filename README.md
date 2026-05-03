# NRP-Tools

Small toolkit for nation-style roleplay: a Discord bot and a resource overlay helper.

## Contents

| Folder | Description |
|--------|-------------|
| [`NationRP Helper Bot/`](NationRP%20Helper%20Bot/README.md) | Discord bot: resolutions, votes, RP time, reminders, optional HTTP API. |
| [`resource_overlay/`](resource_overlay/README.md) | Resource overlay / analysis tooling (see that folder’s README and batch files). |

## First-time setup

1. Clone this repository.
2. Open either subfolder and follow its **README** (Python venv, `pip install`, copy `config.example.*` → local config, copy `.env.example` → `.env` where used).
3. Do **not** commit `.env`, `*.db`, or local `config.json` / `config.yaml`; they are listed in the root [`.gitignore`](.gitignore).

## Git remote (GitHub)

If this folder is not linked yet:

```bash
cd path/to/NRP-Tools
git remote add origin https://github.com/YOUR_USERNAME/NRP-Tools.git
git branch -M main
git add .
git commit -m "Initial commit: NationRP bot and resource overlay"
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username or org.
