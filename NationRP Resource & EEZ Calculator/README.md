# NationRP Resource & EEZ Calculator

**Political map + resource map overlay:** classify land by nation from fill colours, classify **commodities** on the resource layer using **CIELAB ΔE**, and compute an **offshore halo** (EEZ-style band in km) so ocean pixels are attributed to nations **without** painting halos over foreign land. Outputs are **CSV / JSON** (pixel counts and km²) plus optional **markdown** summaries.

| | |
|--|--|
| **Monorepo location** | This package is the **`NationRP Resource & EEZ Calculator/`** folder inside the parent **[NRP-Tools](../README.md)** repository (clone that repo, then open this subfolder). |
| **Start here** | Read **`00_READ_ME_FIRST.txt`**, then copy **`config.example.yaml`** → **`config.yaml`**, add maps under **`maps/`**, and run **`1_Run_GUI.bat`** (Windows GUI) or **`2_Run_Analysis.bat`** (batch pipeline). |
| **Paths in this doc** | Unless stated otherwise, paths are relative to **`NationRP Resource & EEZ Calculator/`** (this folder). |

## What you get

Matches a **political map** (who owns which land pixel) with a **resource map** (commodity colours) and reports **pixel counts** and **area (km²)** for:

- resources on **national land**
- resources in a **coastal halo** that extends **only into ocean** (configurable width in km; converted using **1 px = 6 km** on your canon)

**How offshore gets into econ reports:** the CSV/JSON rows already split **`pixels_on_land`** vs **`pixels_offshore`** (and km²) per nation and commodity using that halo. Optional **`deposit_sampler_zones`** in `config.yaml` produce **`output/deposit_zones_attribution.json`** after each analyze run, for lasso-sized stories (who owns the water under a polygon, vs **beyond halo** ocean).

## Installation & Setup (For New Users)

If you are downloading this tool for the first time or sharing it with another group, setup is completely automated:

1. Ensure you have **Python 3.10+** installed on your computer. (During installation, make sure to check the box that says **"Add Python to PATH"**).
2. Download or clone this entire **`NationRP Resource & EEZ Calculator`** folder (or clone the full **NRP-Tools** repo and open that subfolder).
3. Double-click **`1_Run_GUI.bat`**.
4. The script will automatically create a virtual environment (`.venv`) and install all required dependencies (like `numpy`, `Pillow`, `pyyaml`, `scipy`). It will then launch the GUI.

## End-to-end workflow (picker → tuner → analyze → prose)

Keep **one** working `config.yaml` in **this folder** and merge YAML fragments into it when you can — that way the **deposit tuner**, **Run analyze…**, and the **headless `.bat`** all read the same file without juggling paths. **Two GUIs:** colour picker and deposit tuner cross-launch each other. **Two `.bat` files** cover double-click workflows (see below).

| Step | Tool | You do |
|------|------|--------|
| 0. Open tools | **`1_Run_GUI.bat`** | Starts **deposit_tuner_gui.py**. **Workflow** menu **1 · Nation colours** opens the picker; same app holds resources, lassos, **3 · Run pixel analysis**. |
| 1. Nation fills | **deposit_tuner_gui.py** | Select **Political Eyedropper** tool. Click map to sample → type Name in Step 0 → **Add Nation**. Or **Set as Ocean**. Click **Manage Nations…** to view/delete. |
| 1b. Get nations into config | Tuner | Click **File → Save Project (full config.yaml)**. |
| 2. Resource / ΔE anchors | Deposit tuner | Load `config.yaml` + PNGs → eyedropper / lasso → **Save resource_legend fragment…** or **Save full config.yaml…** |
| 3. Named offshore patches (optional) | Deposit tuner **Saved sampler zones** | Append lassos → **Tag zones with EEZ / halo stats** → merge `deposit_sampler_zones` into `config.yaml` |
| 4. Numbers + commodity view MD | **Workflow → 3 · Run pixel analysis** in tuner, or **`2_Run_Analysis.bat`** | Tuner writes CSV/JSON only under **`output/`**. The **`.bat`** runs **`analyze_resources.py`** then **`build_commodity_view_md.py`** (updates **`PROVISIONAL_commodity_view.md`**). Optional fragment path prompt = same as old **`--nations-yaml`** trial runs. |

Use **`deposit_zones_attribution.json`** for sentences like *“Most of this lasso sits in **Ardland**’s halo band; **N** px are outside every nation’s halo”* (convert pixels → km² with `km_per_pixel`). The main table already credits offshore resources to nations via **`pixels_offshore`**.

## When this is doable

Yes, with these assumptions:

1. **Same canvas** — political and resource PNGs are **identical width × height** and aligned (same projection / export). If one map is cropped differently, overlay is wrong.
2. **Stable legend** — resource colours are documented in `config.yaml` (sample with an eyedropper; we can add a helper script later to dump frequent colours).
3. **Ocean colour(s)** — you list RGB(s) for water on the **political** map so the halo never paints over **foreign land** (only water pixels).

## Resource gradients (shaded deposits)

The resource map uses **smooth shading** — one hex code is not enough.

- Matching uses **CIELAB ΔE** (CIE76): each pixel is compared to **every anchor** you list for that commodity; the **minimum** ΔE must be ≤ that commodity’s **`max_delta_e`** (or the global **`resource_max_delta_e`** default).
- Add **several RGB samples** along a deposit’s gradient (dark + light brown for oil, etc.) via `anchors:` in YAML, or use a short list of triplets for that commodity.
- If a pixel is within range of **two** commodities, it is assigned to whichever has the **lower** ΔE (ties → earlier key in `resource_legend`).
- **`resource_propagation`** (in `config.yaml`, on by default in the bundled file): **(1)** seeds pixels that are clearly inside a commodity’s spectral gate (tight **inner** fraction of each `max_delta_e`, plus optional **confidence** picks where the runner-up is farther in ΔE), **(2)** **grows** labels to unknown **8-neighbours** only when that neighbour’s commodity still passes the **loose** ΔE gate at the new pixel, **(3)** if **oil** and **natural_gas** are both in-range and ΔE is within **`oil_gas_ambiguity_delta`**, propagation does **not** force either (reduces sloppy flips on offshore teal/brown blends), **(4)** anything still unknown but spectrally valid gets the legacy per-pixel winner. Set **`enabled: false`** to revert to pure per-pixel classification for A/B checks.
- Tuning: raise `resource_max_delta_e` or per-commodity `max_delta_e` if deposits look “hollow” in the CSV; lower if different legend colours start merging.
- **Copper / precious look “missing” in commodity tables:** map pixels are often **closer in Lab** to **iron** or **oil** than to your copper/precious anchors. Add **light + dark** anchors sampled from real deposits ([**deposit tuner**](#deposit-tuner-gui-preview-masks--sample-lassos)), raise that commodity’s `max_delta_e`, and put **narrower commodities before iron** in `resource_legend` so equal-ΔE **ties** resolve correctly (YAML key order = tie-break order).
- **Teal natural_gas “missing” while eyedropper looks correct:** (1) `max_delta_e` for gas was often **too small** (e.g. 18) — shaded pixels one step from the anchor can sit **19–22** ΔE away and fail the gas mask entirely. (2) A **single mid-grey** `precious_metals` anchor plus a **wide** `max_delta_e` can be **closer in Lab** to **desaturated teal** than your gas anchors are, so those pixels become “precious” instead. Fix: raise gas `max_delta_e` into the low‑20s/24 range, **tighten** precious `max_delta_e`, and add **light (and if needed dark) precious anchors** sampled from actual ore on the resource map — not only neutral grey.

## Deposit tuner GUI (preview masks & sample lassos)

**`1_Run_GUI.bat`** (Windows) or, from **this folder** in a terminal: **`python scripts/deposit_tuner_gui.py`**.

- **Menu bar (File / Maps / Workflow / View / Help):** file operations, numbered workflow steps, preview mode, blend stack, ocean mask, shortcuts **Ctrl+O** / **Ctrl+S** / **Ctrl+Q** match the labels.
- **Strip under the menus:** zoom readout, **Preview** mode (one dropdown instead of many radio buttons), **Deposit** commodity, live **ΔE** trial, **Show both maps** + blend % slider.
- **View → Strict Ocean Mask:** when enabled (default), **standard** white/black mask previews (`Mask — commodity`, `Hits only`) **zero out hits on sea pixels** so offshore deposits on the cyan ocean look empty — use **`Mask — commodity (+ offshore on water)`** or **`Hits only (+ offshore on water)`**, or turn the checkbox off, to see those pixels. **Diagnostic — void + oil/gas tie hatch** tints **magenta** where **no** commodity is within its ΔE gate (anchors/thresholds), and **red diagonal hatch** where oil and natural_gas are both in-range and ΔE is within `oil_gas_ambiguity_delta` (same idea as the propagation guard).
- **Lasso:** click polygon corners, **Close lasso → log stats** — prints bucket/blob breakdown, ΔE lines, and a **YAML anchor line** to paste (or use Eyedropper below).
- **Eyedropper + anchors:** sidebar **Map clicks: Eyedropper → list** — click the map (deposit tint or **legend swatch** on the PNG). **Apply anchors → memory** writes the current commodity’s list into in-memory `resource_legend`. **Save Everything** and **Run full analysis** also auto-merge pending anchor lists for *every* commodity you opened in this session (so oil + gas edits both land in `config.yaml` even if you only clicked Apply once). The **Commodity max ΔE** field applies only to the **selected** commodity; switching commodities without clearing it no longer overwrites the other’s threshold.
- **Merge nations from YAML** (File menu): loads `nation_colours_fragment.yaml` (or any YAML with `nations:`) into the **in-memory** `config.yaml` — same keys the batch can take via `--nations-yaml`, but merged here so the tuner sees your picker output without hand-editing `config.yaml`. Also applies `color_tolerance`, and if present `ocean_colors`, `ignore_land_colors`, `duplicate_fill_splits` from the fragment (overwrites those keys).
- **Save full config** / **Run pixel analysis** / **Colour picker** live under the **File** and **Workflow** menus; analysis saves to the path from **Open config** first, then runs the same pipeline as `analyze_resources.py` into **`output/`** (background thread + log lines prefixed `[analyze]`).
- **EEZ / offshore halo preview (sidebar):** tinted ocean assigned to each nation using **`assign_offshore`** (same routine as `analyze_resources`). By default **solid nation fills only** — map lettering matched by `nation_title_colors` is **not** used as land, so labels like “Septenez Sea” no longer grow fake EEZ blobs. Turn on **Match CSV** to include title attach (then matches batch/CSV halos; sea names near coast can inflate bands). Halo uses a **smooth alpha falloff** from the coast (not chunky discrete rings). Computation runs in a **background thread** with a **progress bar** so the window should not go “Not responding”. Set **Halo radius (px)** to match your batch: **`--halo-km` = px × `km_per_pixel`**.
- **From scratch:** **File → New minimal config** (stub in memory) → **Add commodity…** → Eyedropper samples → Apply → Save fragment → merge → run analysis. Or start from **File → Open config** and edit anchors the same way.
- **Calibration wizard (sidebar):** **Start wizard** walks **Step 1** — every commodity in `resource_legend` in order (eyedropper on, **Next** saves anchors); **Step 2** — tune global max ΔE and per-commodity ΔE with Mask/ΔE views; **Step 3** — optional lasso for blob/hint logging; **Step 4** — save YAML fragment and merge. **Cancel wizard** exits guided mode. Works for **new** or **existing** configs (existing: you re-sample/replace anchors per commodity as you go).

**Offshore beyond the halo:** analysis only attributes **water** inside **`--halo-km`** of your land. Deposits in basins farther out (e.g. narrative UKGS volumes “just beyond” fill) stay **unassigned** to you until halo/EEZ rules expand or manual attribution is added — the tuner still lets you **see** and **sample** those pixels.

**How you can help:** lasso obvious **copper** / **precious** patches and share the logged anchor lines (or screenshots + RGB); paste into `config.yaml` and re-run `analyze_resources.py`.

## Exact fills & duplicate colours

- Use **tight** `color_tolerance` (e.g. **3**) when map exports are stable; increase only if nation edges vanish from masks.
- If **two nations share the same fill RGB**, list both under `nations:` with **identical** `[r,g,b]`. Then either:
  - **`duplicate_fill_splits`**: `nations:` + **`polygon_xy`** (image coordinates **x, y**) — draw a lasso around one nation in the GUI and click **Set Lasso as Nation Split…** to assign that area to it, **or**
  - **`duplicate_fill_splits`**: `nations:` + **`seeds_xy`** (image coordinates **x, y**) — Voronoi partition of that colour (one seed per polity), **or**
  - omit seeds/polygons — **Lloyd** k-means on pixel positions; **largest** region → **first** nation in YAML among those sharing the RGB.

**Human workflow:** Open **`1_Run_GUI.bat`**. Use the **Tool** dropdown to select **Political Eyedropper**. Click the map, type a name in the sidebar, and click **Add Nation**. Use **Set as Ocean** for `ocean_colors`. Click **Manage Nations…** to view or delete them. Finally, use **File → Save Everything (config.yaml)** to write to `config.yaml`.

### Nation fragment vs `config.yaml`

By default the picker writes a small file (`nation_colours_fragment.yaml`). You can also use **Save + apply to config.yaml…** to update `nations:` and `color_tolerance` in one action.

*(Note: The separate colour picker script has been removed and its functionality is now fully integrated into the main `deposit_tuner_gui.py`. You no longer need to deal with fragments.)*

### EEZ preview vs `analyze_resources` CSV

- **CSV / JSON** use **effective** land masks = solid fills **plus** `nation_title_colors` merged near territory (`effective_nation_masks`).
- **Tuner default (Match CSV off)** uses **solid fills only** for the EEZ preview so typography and sea names are not treated as territory (fixes spurious purple “EEZ” around labels).
- Turn **Match CSV** on when you need **pixel parity** with the batch halos.

### Troubleshooting EEZ preview

- **Whole countries missing (e.g. islands):** `nations:` probably doesn’t list that polity or **duplicate RGB** needs `duplicate_fill_splits` + seeds — merge an updated fragment or edit `config.yaml`, then **Merge nations fragment…** or reload config.
- **Sea looks like land:** tighten `ocean_colors` / `color_tolerance` so open water matches `ocean_colors`.
- **UI froze:** EEZ runs in a **worker thread**; if a dialog still appears stuck, reduce map size or halo px temporarily.

Advanced **brush / relabel** tools are **not** bundled (use an image editor or extend the picker).

## Progress & outputs

`analyze_resources.py` prints **step messages** (Windows-safe ASCII) and a **tqdm** bar over nations. Use `--no-progress` for CI/silent runs.

Outputs include **`pct_of_global_commodity`**: each row’s share of that commodity’s summed `area_km2_total` across **all nations in the config** (land + offshore halo).

If **`deposit_sampler_zones`** is present in the YAML passed to `analyze_resources`, the run also writes **`deposit_zones_attribution.json`** next to the CSV/JSON (per-zone halo overlap and **beyond halo** ocean pixel counts).

Regenerate **`cycles/IRP_YYYY/PROVISIONAL_commodity_view.md`** with **`python build_commodity_view_md.py`** after updating `output/results.json`.

## Halo overlap between countries

Offshore pixels can be within halo distance of **two** coasts. The tool assigns each contested water pixel to the nation whose **land is strictly closer** (Euclidean distance in image space, in pixels). **Ties** go to the nation that appears **earlier** in the `nations:` block in `config.yaml` — reorder that list if you need a mod policy (e.g. alphabetical).

The halo **never** overwrites another country’s **land** or extend **into** it — only **ocean** pixels from your `ocean_colors` list.

Nation **title / label** text often erases fill colour. Set `nation_title_colors` (RGB list, same tolerance as other layers) and optional `title_land_attach_px` (default 8). Title pixels near solid territory attach to the **nearest** nation’s land (same tie-break as offshore). That extends **offshore halos** from the label edges and restores resource counts under labels.

## Windows batch helpers

Read **`00_READ_ME_FIRST.txt`** in this folder for order. Short version:

| File | When |
|------|------|
| **`1_Run_GUI.bat`** | **Interactive:** deposit tuner (colour picker link, lassos, **Run analyze…**). |
| **`2_Run_Analysis.bat`** | **Headless:** `analyze_resources.py` + `build_commodity_view_md.py` in one go (CSV/JSON + `PROVISIONAL_commodity_view.md`). |

Echo lines are ASCII-only (avoids mojibake in `cmd.exe`). **`python`** must be on PATH.

**`.exe`:** not shipped; PyInstaller would bundle a full Python — the `.bat` files stay easy to edit.

## Quick start (command line)

From **this folder** (for example `…/NRP-Tools/NationRP Resource & EEZ Calculator` after cloning the monorepo):

```bash
pip install -r scripts/requirements.txt
cp config.example.yaml config.yaml
# Add aligned PNGs under maps/ and edit config.yaml (paths, halo, nations, etc.)

python scripts/analyze_resources.py --political "maps/Political Map.png" --resources "maps/Resource Map aligned.png" --config config.yaml --halo-km 80 --out output/results.csv --json output/results.json
python scripts/build_commodity_view_md.py
# On Windows you can instead double-click 2_Run_Analysis.bat after config and maps are ready
```

### Arguments

| Argument | Meaning |
|----------|---------|
| `--political` | Political map image (PNG recommended) |
| `--resources` | Resource map (same dimensions) |
| `--config` | YAML config path |
| `--halo-km` | Offshore halo radius in **km** (converted with `km_per_pixel`) |
| `--out` | Output CSV path |
| `--json` | Optional second export path for JSON |
| `--no-progress` | Disable tqdm + stderr step prints |

## Output CSV columns

`nation`, `commodity`, `pixels_on_land`, `pixels_offshore`, `area_km2_land`, `area_km2_offshore`, `area_km2_total`, `pct_of_global_commodity`

## Updating when the political map changes

1. Open new political map → sample **one RGB per nation** (inside solid territory, not borders).
2. Update `nations:` in `config.yaml` (add/remove/rename keys).
3. Re-run. Resource map config rarely changes.

## Where to keep the map files

**Best practice:** keep aligned political and resource **PNGs** under **`maps/`** next to `config.yaml`, and reference those paths from `config.yaml`. Map files are often large; some teams omit them from git and document required filenames for collaborators instead.

**Chat / Cursor image links** from an old message are **not** a durable source — a new session may not resolve them. Re-attach or copy files into `maps/` when you rerun the tool.

## OCR / chat images

Cursor can’t reliably **auto-OCR** nation labels from a screenshot in a repeatable way inside this repo. Practical workflow:

- You paste maps in chat; the agent helps you **pick colours** and **patch `config.yaml`** — then **save the same files** into `maps/` for the next run.
- Or use any **eyedropper** / **GIMP colour picker** and paste RGB triplets.

Optional next step: a small `sample_colors.py` that prints the **most frequent colours** in a map crop (excluding legend boxes) to bootstrap `config.yaml`.

## Limitations

- **Straight Euclidean halo** in image space, not true maritime law / EEZ geometry.
- **Resource shading** handled via **Lab ΔE** + **multi-anchor** config, not a single hex.
- **Jagged borders** on the **political** map still use **`color_tolerance`** (RGB box).
- **Resource map legend overlap** with land: crop legends out or mask ignore rectangles (future enhancement).
- **Offshore deposits** far from coast but still “yours” by RP: may need a **larger halo** or manual mod flags — this tool only does distance-from-coast in water.
