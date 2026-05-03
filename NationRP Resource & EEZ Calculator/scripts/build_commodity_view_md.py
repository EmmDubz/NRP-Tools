"""One-off: regenerate cycles/IRP_2008/PROVISIONAL_commodity_view.md from results.json."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
rows = json.loads((Path(__file__).parent.parent / "output" / "results.json").read_text(encoding="utf-8"))
by_c: dict[str, list] = defaultdict(list)
for r in rows:
    by_c[r["commodity"]].append(r)

lines: list[str] = [
    "# IRP 2008 — Provisional commodity view (NationRP Resource & EEZ Calculator)",
    "",
    "Generated from `output/results.json` in this tool. Re-run "
    "`scripts/analyze_resources.py` after any map or `config.yaml` change.",
    "",
    "**Agents:** treat as provisional. When commodity totals imply tier changes, sync "
    "`reference/resource_endowments.md` and `PROVISIONAL_nirvalistat_detailed_view.md` "
    "resource lines.",
    "",
    "## Workflow note",
    "",
    "- **Tool:** see `README.md` in **NationRP Resource & EEZ Calculator** — aligned political + resource PNGs, "
    "local `config.yaml` (`offshore_halo_px` / CLI `--halo-px` or `--halo-km`).",
    "- **`pct_of_global_commodity`:** share of that commodity's summed `area_km2_total` "
    "across all nations in this config (land + offshore halo).",
    "- **Colours:** `python deposit_tuner_gui.py` — click map, register nation RGB, save project "
    "fragment; set `ocean_colors` via the same samples. Duplicate nation fills: "
    "`duplicate_fill_splits` with `seeds_xy` (Voronoi), or omit for Lloyd auto-split "
    "(largest blob = first nation in YAML for that RGB).",
    "- **Brush / relabel UI:** not included; use GIMP layer masks or extend the picker if needed.",
    "- **Deposit hull attribution:** `output/deposit_zones_attribution.json` — "
    "per-lasso `on_land_px`, `eez_offshore_px`, `beyond_halo_ocean_px` (same halo rule as analysis). "
    "Cross-check with Stage 2 “Deposit hull checks” in `cycles/IRP_2008/PROVISIONAL_stage1_stage2.md`.",
    "",
]

for c in sorted(by_c.keys()):
    lines.append(f"## {c}")
    lines.append("")
    lines.append(
        "| Nation | km² land | km² offshore | km² total | % of global (commodity) |"
    )
    lines.append(
        "|--------|---------:|-------------:|----------:|------------------------:|"
    )
    blk = sorted(by_c[c], key=lambda x: -float(x["area_km2_total"]))
    for r in blk:
        if float(r["area_km2_total"]) <= 0:
            continue
        lines.append(
            f"| {r['nation']} | {r['area_km2_land']} | {r['area_km2_offshore']} | "
            f"{r['area_km2_total']} | {r.get('pct_of_global_commodity', '')} |"
        )
    lines.append("")

out = ROOT / "cycles" / "IRP_2008" / "PROVISIONAL_commodity_view.md"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {out}")
