#!/usr/bin/env python3
"""
Deposit / commodity colour tuner — preview resource-map classification and sample lasso regions.

Load config.yaml: reads resource_legend, resource_max_delta_e, resource_exclude_*, and
ocean_colors (no images). Refreshes the commodity list, rebuilds the political ocean-colour mask
if a political PNG is already loaded, and clears classification caches so previews match the file.

Heavy work (full commodity classification, LAB, per-commodity ΔE distance field) is cached;
pan/zoom/overlay only re-blend/resize. Warm all caches precomputes LAB, winner masks, and
ΔE distance rasters for every legend commodity (progress bar).

Saving: nothing overwrites config.yaml automatically except **File → Save full config** and **Workflow → Run pixel analysis**
(which saves in-memory state to the loaded config path first). Save resource_legend fragment writes a
YAML file you merge (Apply only updates memory until you save).

Lasso: samples resource-map pixels inside the polygon (see Lasso section). Multi-select
commodities for ΔE; finished outlines stay on the map (orange) after Close; snap clicks to
corners of orange outlines / saved zones. Tune Colour merge and Min blob px.

NumPy rasters are shape (height, width, 3) — display math uses width/height correctly.

Offshore EEZ / halo preview: optional semi-transparent tint on ocean pixels assigned
to a nation by the same assign_offshore logic as analyze_resources (width in px;
default 62 px matches typical project tuning; equals --halo-km / km_per_pixel when you
align those with config).

Run: python deposit_tuner_gui.py — menu bar: File, Maps, Workflow, View, Help; compact strip for preview/deposit/blend.
"""

from __future__ import annotations

import colorsys
import os
import subprocess
import sys
import threading
import tkinter as tk
from copy import deepcopy
from pathlib import Path
from typing import Any
from tkinter import colorchooser, filedialog, messagebox, scrolledtext, simpledialog, ttk

import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageTk

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Lasso: treat as background white when R,G,B are all >= this (plain map padding).
_LASSO_WHITE_RGB_MIN = 250

import analyze_resources as ar  # noqa: E402
from scipy import ndimage  # noqa: E402

_LASSO_CC_STRUCT = np.ones((3, 3), dtype=bool)  # 8-connected deposit blobs

# Preview mode: internal key + options-bar / menu label
_PREVIEW_MODE_ROWS: tuple[tuple[str, str], ...] = (
    ("resource", "Resource map (raw)"),
    ("mask_wb", "Mask — commodity (white/black)"),
    ("pitch_black", "Hits only (black)"),
    ("grey", "Luminance"),
    ("delta_e", "ΔE heat"),
    ("diag_spectral", "Diagnostic — void + oil/gas tie hatch"),
)
# Previews that need a selected commodity in resource_legend
_PREVIEW_COMMODITY_MODES: frozenset[str] = frozenset(
    {"delta_e", "mask_wb", "pitch_black"}
)


def _preview_label_for_key(key: str) -> str:
    for k, lab in _PREVIEW_MODE_ROWS:
        if k == key:
            return lab
    return _PREVIEW_MODE_ROWS[0][1]


def _preview_key_for_label(label: str) -> str | None:
    for k, lab in _PREVIEW_MODE_ROWS:
        if lab == label:
            return k
    return None


def _lasso_color_buckets(
    rgb: np.ndarray, pm: np.ndarray, merge_shift: int, top_k: int
) -> list[tuple[int, np.ndarray]]:
    """Largest quantized RGB buckets inside pm: [(count, mean_rgb_u8), ...]."""
    ms = max(0, min(8, int(merge_shift)))
    r = rgb[..., 0].astype(np.uint32) >> ms
    g = rgb[..., 1].astype(np.uint32) >> ms
    b = rgb[..., 2].astype(np.uint32) >> ms
    tags = (r << 16) | (g << 8) | b
    vals = tags[pm]
    if len(vals) == 0:
        return []
    uniq, inv = np.unique(vals, return_inverse=True)
    cnt = np.bincount(inv)
    order = np.argsort(-cnt)
    top_k = max(1, int(top_k))
    out: list[tuple[int, np.ndarray]] = []
    for j in order[:top_k]:
        u = int(uniq[j])
        sel = pm & (tags == u)
        m = rgb[sel].mean(axis=0)
        out.append((int(cnt[j]), m.astype(np.uint8)))
    return out


def _lasso_spatial_blobs(
    rgb: np.ndarray,
    pm: np.ndarray,
    merge_shift: int,
    top_colour_tags: int,
    min_blob_px: int,
    max_blobs_logged: int,
) -> list[tuple[int, np.ndarray]]:
    """
    For the most frequent quantized colours, find connected blobs (8-neighbour).
    Returns [(area_px, mean_rgb_u8), ...] sorted by area descending.
    """
    ms = max(0, min(8, int(merge_shift)))
    r = rgb[..., 0].astype(np.uint32) >> ms
    g = rgb[..., 1].astype(np.uint32) >> ms
    b = rgb[..., 2].astype(np.uint32) >> ms
    tags = (r << 16) | (g << 8) | b
    vals = tags[pm]
    if len(vals) == 0:
        return []
    uniq, inv = np.unique(vals, return_inverse=True)
    cnt = np.bincount(inv)
    order = np.argsort(-cnt)
    tag_list = [int(uniq[j]) for j in order[: max(1, int(top_colour_tags))]]

    all_blobs: list[tuple[int, np.ndarray]] = []
    for t in tag_list:
        mask = (tags == t) & pm
        if not np.any(mask):
            continue
        labeled, nfeat = ndimage.label(mask, structure=_LASSO_CC_STRUCT)
        for fi in range(1, nfeat + 1):
            bm = labeled == fi
            area = int(np.count_nonzero(bm))
            if area < int(min_blob_px):
                continue
            mrgb = rgb[bm].mean(axis=0)
            all_blobs.append((area, mrgb.astype(np.uint8)))

    all_blobs.sort(key=lambda x: -x[0])
    return all_blobs[: max(1, int(max_blobs_logged))]


def _overlay_root() -> Path:
    return _ROOT


def _maps_dir() -> Path:
    d = _ROOT / "maps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _walk_tk_widgets(root: tk.Misc):
    yield root
    for c in root.winfo_children():
        yield from _walk_tk_widgets(c)


def _compute_eez_overlay_layers(
    pol: np.ndarray,
    cfg: dict,
    halo_px: int,
    use_title_attach: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str], dict[str, np.ndarray]]:
    """
    Float32 RGB + alpha (H, W) for EEZ tint. Alpha falls off from coast → outer edge
    (one smooth EDT-based band, not discrete dilation “passes”).
    Returns (rgb, alpha, offshore_owner, nation_order, land_masks).
    """
    nation_masks, _wctx, _land_u, nation_order, ocean = ar.nation_masks_and_context(
        pol, cfg
    )
    if not nation_order:
        raise ValueError("config has no nations")
    if use_title_attach:
        land_masks = ar.effective_nation_masks(
            pol, cfg, nation_masks, nation_order, ocean
        )
    else:
        land_masks = nation_masks
    land_union = np.zeros_like(ocean, dtype=bool)
    for m in land_masks.values():
        land_union |= m
    water_mask = ocean & ~land_union
    offshore_owner, dist_hit = ar.assign_offshore(
        land_masks, water_mask, float(halo_px), nation_order
    )
    h, w = offshore_owner.shape[:2]
    nnat = len(nation_order)
    palette = np.zeros((max(nnat, 1), 3), dtype=np.float32)
    for i in range(nnat):
        hue = (i * 0.618033988749895) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.5, 0.95)
        palette[i] = (r * 255.0, g * 255.0, b * 255.0)
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    alpha = np.zeros((h, w), dtype=np.float32)
    mask = offshore_owner >= 0
    if not np.any(mask):
        return rgb, alpha, offshore_owner, nation_order, land_masks
    
    # Create wire outlines instead of a gradient
    # A pixel is a border if it belongs to a country AND at least one 4-neighbor
    # has a different owner. BUT we don't want to draw borders against the country's own land.
    
    # We only want borders between different EEZs, or between EEZ and unclaimed water.
    # We do NOT want borders against land (any land, whether own or other country).
    # This avoids AA artifacts causing yellow lines along the coast.
    
    up_wo = np.roll(offshore_owner, 1, axis=0)
    down_wo = np.roll(offshore_owner, -1, axis=0)
    left_wo = np.roll(offshore_owner, 1, axis=1)
    right_wo = np.roll(offshore_owner, -1, axis=1)
    
    up_w = np.roll(water_mask, 1, axis=0)
    down_w = np.roll(water_mask, -1, axis=0)
    left_w = np.roll(water_mask, 1, axis=1)
    right_w = np.roll(water_mask, -1, axis=1)
    
    border = mask & (
        (up_w & (offshore_owner != up_wo)) |
        (down_w & (offshore_owner != down_wo)) |
        (left_w & (offshore_owner != left_wo)) |
        (right_w & (offshore_owner != right_wo))
    )
    
    # Draw yellow wire outlines
    rgb[border] = (255.0, 255.0, 0.0)
    alpha[border] = 1.0
    
    return rgb, alpha, offshore_owner, nation_order, land_masks


def ocean_mask_exact(rgb: np.ndarray, colors: list[list[int]]) -> np.ndarray:
    if not colors:
        return np.zeros(rgb.shape[:2], dtype=bool)
    mask = np.zeros(rgb.shape[:2], dtype=bool)
    for c in colors:
        t = np.array(c, dtype=np.uint8)
        mask |= np.all(rgb == t, axis=-1)
    return mask


def rgb_greyscale(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = (0.299 * r + 0.587 * g + 0.114 * b).astype(np.uint8)
    return np.stack([y, y, y], axis=-1)


def _normalize_deposit_zone(raw: Any) -> dict[str, Any] | None:
    """YAML zone entry → {id, points_xy, commodity?, nation?} or None."""
    if not isinstance(raw, dict):
        return None
    zid = raw.get("id")
    pts = raw.get("points_xy") if raw.get("points_xy") is not None else raw.get("points")
    if not zid or not isinstance(pts, list) or len(pts) < 3:
        return None
    points_xy: list[list[int]] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            points_xy.append([int(p[0]), int(p[1])])
    if len(points_xy) < 3:
        return None
    com = raw.get("commodity")
    nat = raw.get("nation")
    out: dict[str, Any] = {
        "id": str(zid).strip(),
        "points_xy": points_xy,
        "commodity": str(com).strip() if com else "",
        "nation": str(nat).strip() if nat else "",
    }
    for k in (
        "eez_offshore_px",
        "beyond_halo_ocean_px",
        "on_land_px",
        "halo_px_used",
        "attribute_mode",
        "notes",
    ):
        if k in raw:
            out[k] = raw[k]
    return out


def _legend_with_de_override(
    cfg: dict, commodity: str, de_override: float | None
) -> dict:
    """Shallow cfg copy with one resource_legend entry patched — no full deepcopy."""
    leg = cfg.get("resource_legend") or {}
    if commodity not in leg:
        raise KeyError(commodity)
    if de_override is None:
        return cfg
    raw = leg[commodity]
    default_de = float(cfg.get("resource_max_delta_e", 18.0))
    if isinstance(raw, dict):
        patched = {**raw, "max_delta_e": float(de_override)}
    else:
        anchors, _ = ar.parse_commodity_spec(raw, default_de)
        patched = {"anchors": anchors, "max_delta_e": float(de_override)}
    new_leg = {**leg, commodity: patched}
    return {**cfg, "resource_legend": new_leg}


class DepositTunerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("NirvaliStat — deposit / commodity tuner")
        self.geometry("1280x820")

        self._cfg: dict | None = None
        self._pol: np.ndarray | None = None
        self._res: np.ndarray | None = None
        self._ocean_pol: np.ndarray | None = None

        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._pan_drag: tuple[float, float] | None = None
        self._photo: ImageTk.PhotoImage | None = None

        self._lasso_pts: list[tuple[int, int]] = []
        self._lasso_closed_polys: list[list[tuple[int, int]]] = []

        self.lasso_omit_white = tk.BooleanVar(value=True)
        self.lasso_omit_sea_political = tk.BooleanVar(value=False)
        self.lasso_merge_shift = tk.IntVar(value=4)
        self.lasso_min_blob_px = tk.IntVar(value=80)
        self.lasso_top_colours = tk.IntVar(value=8)
        self.lasso_snap_corners = tk.BooleanVar(value=True)
        self.lasso_snap_radius_canvas = tk.IntVar(value=14)

        self._eez_cache_key: tuple | None = None
        self._eez_overlay_rgb: np.ndarray | None = None
        self._eez_overlay_alpha: np.ndarray | None = None
        self.show_eez_overlay = tk.BooleanVar(value=False)
        self.eez_halo_px = tk.IntVar(value=62)
        self.eez_use_title_attach = tk.BooleanVar(value=False)
        self._eez_async_busy = False
        self._eez_compute_token = 0
        # Caches: use _invalidate_all_caches on res/config; lighter invalidation on commodity/de.
        self._masks_cache_key: tuple | None = None
        self._masks_cache: dict[str, np.ndarray] | None = None
        self._preview_key: tuple | None = None
        self._preview_u8: np.ndarray | None = None
        self._lab_cache_id: int | None = None
        self._lab_cache_arr: np.ndarray | None = None
        self._de_dist_cache: dict[tuple[int, int, str], np.ndarray] = {}

        self._draw_job: str | None = None
        self._configure_job: str | None = None
        self._overlay_job: str | None = None
        self._warm_busy = False
        self._warm_step = 0
        self._warm_total = 0
        self._warm_commodities: list[str] = []
        self._cfg_revision = 0
        self.map_tool = tk.StringVar(value="lasso")
        self._anchor_session: dict[str, list[list[int]]] = {}
        self.commodity_max_de = tk.StringVar(value="")
        self.wizard_global_maxde = tk.StringVar(value="22")
        self._wiz_phase = "idle"
        self._wiz_idx = 0
        self._wiz_list: list[str] = []
        self._zones: list[dict[str, Any]] = []
        self._selected_zone_ix: int | None = None
        self._config_path: Path | None = None
        self._political_path: Path | None = None
        self._resource_path: Path | None = None
        self._analyze_busy = False
        self.analysis_halo_km = tk.IntVar(value=80)

        # Nation list view
        self._nation_list_window: tk.Toplevel | None = None
        self._last_pol_rgb: tuple[int, int, int] | None = None

        # Shared with menu bar / options bar (declare before _build_menubar).
        self.preview_mode = tk.StringVar(value="resource")
        self.mask_ocean = tk.BooleanVar(value=True)
        self.show_overlay = tk.BooleanVar(value=False)
        self.overlay_stack = tk.StringVar(value="resource_on_political")
        self.overlay_alpha = tk.IntVar(value=35)

        self._build_menubar()

        top = ttk.Frame(self, padding=(6, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Tool").pack(side=tk.LEFT, padx=(10, 2))
        self.map_tool_display = tk.StringVar(value="Lasso")
        self.map_tool_combo = ttk.Combobox(
            top,
            textvariable=self.map_tool_display,
            width=18,
            state="readonly",
            values=["Lasso", "Resource Eyedropper", "Political Eyedropper"]
        )
        self.map_tool_combo.pack(side=tk.LEFT)
        self.map_tool_combo.bind("<<ComboboxSelected>>", self._on_map_tool_change)

        ttk.Label(top, text="Zoom").pack(side=tk.LEFT, padx=(10, 2))
        self.zoom_label = ttk.Label(top, text="1.00×", width=7)
        self.zoom_label.pack(side=tk.LEFT)
        
        self.zoom_var = tk.DoubleVar(value=1.0)
        self.zoom_slider = ttk.Scale(
            top,
            from_=0.25,
            to=16.0,
            orient=tk.HORIZONTAL,
            variable=self.zoom_var,
            length=100,
            command=self._on_zoom_slider
        )
        self.zoom_slider.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Separator(top, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )

        ttk.Label(top, text="View Mode").pack(side=tk.LEFT, padx=(0, 2))
        self._preview_mode_display = tk.StringVar(
            value=_preview_label_for_key(self.preview_mode.get())
        )
        self.preview_mode_combo = ttk.Combobox(
            top,
            textvariable=self._preview_mode_display,
            width=25,
            state="readonly",
            values=[row[1] for row in _PREVIEW_MODE_ROWS],
        )
        self.preview_mode_combo.pack(side=tk.LEFT)
        self.preview_mode_combo.bind(
            "<<ComboboxSelected>>", self._on_preview_mode_combo_selected
        )

        ttk.Label(top, text="Resource").pack(side=tk.LEFT, padx=(10, 2))
        self.commodity_var = tk.StringVar(value="copper")
        self.commodity_combo = ttk.Combobox(
            top, textvariable=self.commodity_var, width=14, state="readonly"
        )
        self.commodity_combo.pack(side=tk.LEFT)
        self.commodity_combo.bind("<<ComboboxSelected>>", self._on_commodity_change)

        ttk.Label(top, text="Tolerance").pack(side=tk.LEFT, padx=(8, 2))
        self.de_override = tk.StringVar(value="")
        ent_de = ttk.Entry(top, textvariable=self.de_override, width=5)
        ent_de.pack(side=tk.LEFT)
        ent_de.bind("<KeyRelease>", self._on_de_keyrelease)

        ttk.Separator(top, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )

        ttk.Checkbutton(
            top,
            text="Blend Maps",
            variable=self.show_overlay,
            command=self._schedule_redraw_fast,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(top, text="Opacity").pack(side=tk.LEFT, padx=(2, 0))
        tk.Scale(
            top,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.overlay_alpha,
            length=90,
            showvalue=1,
            command=lambda _v: self._schedule_overlay_redraw(),
        ).pack(side=tk.LEFT, padx=2)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(mid, bg="#222", cursor="crosshair", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)
        self.canvas.bind("<MouseWheel>", self._on_canvas_mousewheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_canvas_mousewheel_linux(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._on_canvas_mousewheel_linux(e, -1))
        self.canvas.bind("<Enter>", self._canvas_enter)
        self.canvas.bind("<Button-2>", self._pan_start)
        self.canvas.bind("<B2-Motion>", self._pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._pan_end)

        side_outer = ttk.Frame(mid)
        side_outer.pack(side=tk.RIGHT, fill=tk.BOTH, expand=False)
        side_scroll = ttk.Scrollbar(side_outer, orient=tk.VERTICAL)
        self._side_canvas = tk.Canvas(
            side_outer,
            highlightthickness=0,
            width=400,
        )
        self._side_canvas.configure(yscrollcommand=side_scroll.set)
        side_scroll.configure(command=self._side_canvas.yview)
        side_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._side_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        side = ttk.Frame(self._side_canvas, padding=6)
        self._side_inner_window = self._side_canvas.create_window(
            (0, 0), window=side, anchor=tk.NW
        )

        def _on_side_inner_configure(_event: tk.Event) -> None:
            self._side_canvas.configure(scrollregion=self._side_canvas.bbox("all"))

        def _on_side_canvas_configure(event: tk.Event) -> None:
            iw = int(event.width)
            if iw > 1:
                self._side_canvas.itemconfigure(self._side_inner_window, width=iw)

        side.bind("<Configure>", _on_side_inner_configure)
        self._side_canvas.bind("<Configure>", _on_side_canvas_configure)

        nf = ttk.LabelFrame(side, text="Step 0: Nations & Ocean", padding=4)
        nf.pack(anchor=tk.W, fill=tk.X, pady=(0, 10))
        ttk.Label(nf, text="Use 'Political Eyedropper' tool to sample map.", wraplength=320).pack(anchor=tk.W)
        
        self.pol_rgb_label = ttk.Label(nf, text="Sampled RGB: —")
        self.pol_rgb_label.pack(anchor=tk.W, pady=(4, 0))
        
        nrow = ttk.Frame(nf)
        nrow.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        ttk.Label(nrow, text="Name:").pack(side=tk.LEFT)
        self.nation_name_var = tk.StringVar()
        self.nation_name_entry = ttk.Combobox(nrow, textvariable=self.nation_name_var, width=22)
        self.nation_name_entry.pack(side=tk.LEFT, padx=4)
        self.nation_name_entry.bind("<Return>", lambda e: self._add_nation_from_ui())
        
        self.nation_add_btn = ttk.Button(nrow, text="Add Nation", command=self._add_nation_from_ui)
        self.nation_add_btn.pack(side=tk.LEFT)
        
        self.nation_add_extra_btn = ttk.Button(nrow, text="Add Colour", command=lambda: self._add_nation_from_ui(add_extra=True))
        # Pack this dynamically in _update_nation_ui_state
        
        self.nation_name_var.trace_add("write", self._update_nation_ui_state)
        
        orow = ttk.Frame(nf)
        orow.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Button(orow, text="Set as Ocean", command=self._add_ocean_from_ui).pack(side=tk.LEFT)
        ttk.Button(orow, text="Manage Nations…", command=self._show_nation_list_window).pack(side=tk.LEFT, padx=4)

        wf = ttk.LabelFrame(side, text="Step 1: Calibration Wizard", padding=6)
        wf.pack(anchor=tk.W, fill=tk.X, pady=(8, 0))
        self._wiz_progress = ttk.Label(wf, text="")
        self._wiz_progress.pack(anchor=tk.W)
        self._wiz_help = ttk.Label(wf, text="", wraplength=320, justify=tk.LEFT)
        self._wiz_help.pack(anchor=tk.W, pady=(4, 0))
        self._wiz_tune_row = ttk.Frame(wf)
        ttk.Label(self._wiz_tune_row, text="Global default max ΔE:").pack(side=tk.LEFT)
        ttk.Entry(self._wiz_tune_row, textvariable=self.wizard_global_maxde, width=6).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(
            self._wiz_tune_row, text="Apply global", command=self._wizard_apply_global_de
        ).pack(side=tk.LEFT, padx=4)
        wiz_btns = ttk.Frame(wf)
        wiz_btns.pack(anchor=tk.W, pady=(8, 0))
        ttk.Button(wiz_btns, text="Start wizard", command=self._wizard_start).pack(
            side=tk.LEFT, padx=2
        )
        self._btn_wiz_back = ttk.Button(wiz_btns, text="Back", command=self._wizard_back)
        self._btn_wiz_back.pack(side=tk.LEFT, padx=2)
        self._btn_wiz_next = ttk.Button(wiz_btns, text="Next", command=self._wizard_next)
        self._btn_wiz_next.pack(side=tk.LEFT, padx=2)
        ttk.Button(wiz_btns, text="Cancel wizard", command=self._wizard_cancel).pack(
            side=tk.LEFT, padx=2
        )
        ezf = ttk.LabelFrame(side, text="Step 2: Offshore EEZ / Halo", padding=4)
        ezf.pack(anchor=tk.W, fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            ezf,
            text="Show EEZ overlay (ocean only; same assign_offshore math as batch)",
            variable=self.show_eez_overlay,
            command=self._on_eez_overlay_toggle,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            ezf,
            text="Match CSV: include nation_title_colors in land (adds halos from labels)",
            variable=self.eez_use_title_attach,
            command=self._on_eez_title_attach_toggle,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(
            ezf,
            text=(
                "Halo width in image pixels (Euclidean from land into ocean_colors). "
                "Default 62 px — align batch: --halo-km = (px) × km_per_pixel."
            ),
            wraplength=320,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))
        erow = ttk.Frame(ezf)
        erow.pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(erow, text="Halo radius (px):").pack(side=tk.LEFT)
        tk.Spinbox(
            erow,
            from_=1,
            to=500,
            width=5,
            textvariable=self.eez_halo_px,
        ).pack(side=tk.LEFT, padx=4)
        self._eez_km_hint = ttk.Label(erow, text="")
        self._eez_km_hint.pack(side=tk.LEFT, padx=8)
        self.eez_halo_px.trace_add("write", lambda *_: self._eez_halo_px_trace())
        self._eez_prog = ttk.Progressbar(ezf, mode="indeterminate", length=280)
        self._eez_prog.pack(anchor=tk.W, pady=(4, 0))
        self._eez_prog.pack_forget()
        self._eez_status_label = ttk.Label(ezf, text="")
        self._eez_status_label.pack(anchor=tk.W)

        af = ttk.LabelFrame(side, text="Anchors & saving (resource_legend)", padding=4)
        af.pack(anchor=tk.W, fill=tk.X, pady=(10, 0))
        tfr = ttk.Frame(af)
        tfr.pack(anchor=tk.W, fill=tk.X)
        ttk.Label(tfr, text="Map clicks:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            tfr, text="Lasso polygon", variable=self.map_tool, value="lasso"
        ).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(
            tfr, text="Resource Eyedropper", variable=self.map_tool, value="eyedropper"
        ).pack(side=tk.LEFT)
        ttk.Label(af, text="Anchors (current commodity):").pack(anchor=tk.W, pady=(4, 0))
        self.anchor_listbox = tk.Listbox(af, height=5, width=36, selectmode=tk.EXTENDED)
        self.anchor_listbox.pack(anchor=tk.W, fill=tk.X)
        row_de = ttk.Frame(af)
        row_de.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Label(row_de, text="Commodity max ΔE:").pack(side=tk.LEFT)
        ttk.Entry(row_de, textvariable=self.commodity_max_de, width=7).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Label(row_de, text="(blank = global default)").pack(side=tk.LEFT)
        row_ab = ttk.Frame(af)
        row_ab.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Button(row_ab, text="Remove selected", command=self._anchor_remove_selected).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(
            row_ab, text="Apply anchors → memory", command=self._apply_anchors_to_cfg
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(
            af,
            text=(
                "The max ΔE field applies only to the commodity selected above. "
                "Save Everything or Run full analysis also writes pending anchors for "
                "any commodity whose list you edited."
            ),
            wraplength=320,
            font=("", 8),
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(
            af, text="Save resource_legend fragment…", command=self._save_resource_fragment
        ).pack(anchor=tk.W, pady=(4, 0))
        row_nf = ttk.Frame(af)
        row_nf.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        ttk.Button(
            row_nf, text="Add commodity…", command=self._add_commodity_dialog
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_nf, text="New minimal config", command=self._new_minimal_config
        ).pack(side=tk.LEFT)

        bf = ttk.LabelFrame(side, text="Batch analysis (output CSV / JSON)", padding=4)
        bf.pack(anchor=tk.W, fill=tk.X, pady=(10, 0))
        ttk.Label(
            bf,
            text=(
                "Writes output/results.csv and output/results.json (same as analyze_resources / headless .bat step 1). "
                "If deposit_sampler_zones is in the saved config, also writes deposit_zones_attribution.json. "
                "Run analyze saves your in-memory config to the path from Load config (or prompts once)."
            ),
            wraplength=320,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        hrow = ttk.Frame(bf)
        hrow.pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(hrow, text="Offshore halo (km, --halo-km):").pack(side=tk.LEFT)
        tk.Spinbox(
            hrow, from_=1, to=2000, width=6, textvariable=self.analysis_halo_km
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(bf, text="Open output folder", command=self._open_output_folder).pack(
            anchor=tk.W, pady=(6, 0)
        )
        self._analyze_prog = ttk.Progressbar(bf, mode="indeterminate", length=280)
        self._analyze_status = ttk.Label(bf, text="")
        self._analyze_status.pack(anchor=tk.W, pady=(2, 0))

        lf = ttk.LabelFrame(side, text="Step 3: Lasso & Stats", padding=4)
        lf.pack(anchor=tk.W, fill=tk.X, pady=(10, 0))
        ttk.Label(
            lf,
            text=(
                "Select commodities below for stats. "
                "If none selected, the Deposit field above the map is used."
            ),
            wraplength=320,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        self.lasso_commodity_listbox = tk.Listbox(
            lf, height=6, width=40, selectmode=tk.EXTENDED, exportselection=False
        )
        self.lasso_commodity_listbox.pack(anchor=tk.W, fill=tk.X, pady=(2, 4))
        
        lasso_sel_row = ttk.Frame(lf)
        lasso_sel_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 4))
        ttk.Button(
            lasso_sel_row, 
            text="Select All", 
            command=lambda: self.lasso_commodity_listbox.selection_set(0, tk.END)
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            lasso_sel_row, 
            text="Clear Selection", 
            command=lambda: self.lasso_commodity_listbox.selection_clear(0, tk.END)
        ).pack(side=tk.LEFT)
        
        snap_row = ttk.Frame(lf)
        snap_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        ttk.Checkbutton(
            snap_row,
            text="Snap new clicks to corners (orange / green / saved zones)",
            variable=self.lasso_snap_corners,
        ).pack(side=tk.LEFT)
        ttk.Label(snap_row, text="radius (screen px):").pack(side=tk.LEFT, padx=(8, 2))
        tk.Spinbox(
            snap_row,
            from_=4,
            to=80,
            width=4,
            textvariable=self.lasso_snap_radius_canvas,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            lf,
            text=(
                f"Omit near-white (all RGB ≥{_LASSO_WHITE_RGB_MIN}, resource map)"
            ),
            variable=self.lasso_omit_white,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            lf,
            text="Omit sea (political pixels matching ocean_colors)",
            variable=self.lasso_omit_sea_political,
        ).pack(anchor=tk.W)
        row_q = ttk.Frame(lf)
        row_q.pack(anchor=tk.W, fill=tk.X, pady=(6, 0))
        ttk.Label(row_q, text="Colour merge (RGB >> n):").pack(side=tk.LEFT)
        tk.Spinbox(
            row_q,
            from_=3,
            to=6,
            width=4,
            textvariable=self.lasso_merge_shift,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(row_q, text="larger=lumpier").pack(side=tk.LEFT)
        row_b = ttk.Frame(lf)
        row_b.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        ttk.Label(row_b, text="Min blob px:").pack(side=tk.LEFT)
        tk.Spinbox(
            row_b,
            from_=20,
            to=5000,
            width=6,
            textvariable=self.lasso_min_blob_px,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(row_b, text="Top colours:").pack(side=tk.LEFT, padx=(8, 0))
        tk.Spinbox(
            row_b,
            from_=3,
            to=16,
            width=4,
            textvariable=self.lasso_top_colours,
        ).pack(side=tk.LEFT, padx=4)
        row_lasso_btns = ttk.Frame(lf)
        row_lasso_btns.pack(anchor=tk.W, fill=tk.X, pady=(8, 0))
        ttk.Button(
            row_lasso_btns, text="Clear active (green)", command=self._clear_lasso
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            row_lasso_btns,
            text="Clear finished (orange)",
            command=self._clear_closed_lassos,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            lf,
            text="Close lasso",
            command=self._close_lasso,
        ).pack(anchor=tk.W, pady=(6, 0))

        zf = ttk.LabelFrame(
            side,
            text="Step 4: Saved Zones (Lassos)",
            padding=4,
        )
        zf.pack(anchor=tk.W, fill=tk.X, pady=(10, 0))
        self.zone_listbox = tk.Listbox(zf, height=5, width=44, selectmode=tk.BROWSE)
        self.zone_listbox.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        self.zone_listbox.bind("<<ListboxSelect>>", self._on_zone_select)
        zrow1 = ttk.Frame(zf)
        zrow1.pack(anchor=tk.W, pady=(4, 0))
        ttk.Button(zrow1, text="Load zones YAML…", command=self._zones_load_file).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(zrow1, text="Save zones YAML…", command=self._zones_save_file).pack(
            side=tk.LEFT, padx=2
        )
        zrow2 = ttk.Frame(zf)
        zrow2.pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(zrow2, text="Append current lasso", command=self._zones_append_lasso).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(zrow2, text="Delete selected", command=self._zones_delete_selected).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(zrow2, text="Log stats (selected)", command=self._zones_log_stats_selected).pack(
            side=tk.LEFT, padx=2
        )
        zrow3 = ttk.Frame(zf)
        zrow3.pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(
            zrow3,
            text="Append all orange outlines…",
            command=self._zones_append_all_closed_lassos,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            zrow3,
            text="Tag zones with EEZ / halo stats…",
            command=self._zones_tag_eez_attribution,
        ).pack(side=tk.LEFT, padx=2)
        
        zrow4 = ttk.Frame(zf)
        zrow4.pack(anchor=tk.W, pady=(2, 0))
        ttk.Button(
            zrow4,
            text="Set Lasso as Nation Split…",
            command=self._lasso_as_nation_split,
        ).pack(side=tk.LEFT, padx=2)

        self._bind_sidebar_scroll_handlers(side)

        bot = ttk.Frame(self, padding=4)
        bot.pack(fill=tk.BOTH)
        prog_row = ttk.Frame(bot)
        prog_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prog_row, text="Analysis Progress:").pack(side=tk.LEFT, padx=(0, 6))
        self._warm_progress = ttk.Progressbar(
            prog_row, mode="determinate", length=420, maximum=100, value=0
        )
        self._warm_progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self._warm_status = ttk.Label(prog_row, text="Ready", width=18)
        self._warm_status.pack(side=tk.LEFT)
        self.log = scrolledtext.ScrolledText(bot, height=8, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.bind_all("<KeyPress>", self._global_shortcuts, add="+")

        default_cfg = _overlay_root() / "config.yaml"
        if default_cfg.is_file():
            self._cfg = yaml.safe_load(default_cfg.read_text(encoding="utf-8"))
            self._config_path = default_cfg
            self._anchor_session = {}
            self._refresh_commodities()
            self._sync_commodity_max_de_from_cfg()
            self._refresh_anchor_listbox()
            self._refresh_nation_combobox()
            self._update_nation_ui_state()
            self._log(f"Loaded config {default_cfg}")
            self._reload_zones_from_cfg()

        self._update_eez_km_hint()
        self._wizard_refresh_ui()

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self, tearoff=0)
        self.config(menu=menubar)

        file_m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_m, underline=0)
        file_m.add_command(
            label="Open Project (config.yaml)…",
            command=self._load_config,
            accelerator="Ctrl+O",
        )
        file_m.add_command(
            label="Save Everything (config.yaml)…",
            command=self._save_full_config_yaml,
            accelerator="Ctrl+S",
        )
        file_m.add_separator()
        file_m.add_command(
            label="Export Political + EEZ Map…",
            command=self._export_political_eez_map,
        )
        file_m.add_separator()
        file_m.add_command(
            label="Import Nations Fragment (legacy)…",
            command=self._merge_nations_fragment,
        )
        file_m.add_command(
            label="Export Resource Legend Fragment…",
            command=self._save_resource_fragment,
        )
        file_m.add_separator()
        file_m.add_command(
            label="New Project (minimal, in memory)…",
            command=self._new_minimal_config,
        )
        file_m.add_separator()
        file_m.add_command(
            label="Exit",
            command=self.destroy,
            accelerator="Ctrl+Q",
        )

        maps_m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Maps", menu=maps_m, underline=0)
        maps_m.add_command(
            label="Load Political Map (PNG)…",
            command=self._load_political,
        )
        maps_m.add_command(
            label="Load Resource Map (PNG)…",
            command=self._load_resource,
        )
        maps_m.add_separator()
        maps_m.add_command(
            label="Update EEZ (Recalculate)",
            command=self._kick_eez_async_compute,
        )
        maps_m.add_command(
            label="Reload EEZ (Clear Cache)",
            command=self._reload_eez_full,
        )

        workflow_m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Workflow", menu=workflow_m, underline=0)
        workflow_m.add_command(
            label="1 · View & Manage Nations…",
            command=self._show_nation_list_window,
        )
        workflow_m.add_separator()
        workflow_m.add_command(
            label="2 · Precompute Previews (slow, updates caches)…",
            command=self._warm_all_start,
        )
        workflow_m.add_separator()
        workflow_m.add_command(
            label="3 · Run Full Analysis (CSV/JSON output)…",
            command=self._run_full_analysis,
        )
        workflow_m.add_command(
            label="Open Results Folder…",
            command=self._open_output_folder,
        )

        view_m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="View", menu=view_m, underline=0)
        prev_sub = tk.Menu(view_m, tearoff=0)
        view_m.add_cascade(label="Map View Mode", menu=prev_sub)
        for val, ptitle in _PREVIEW_MODE_ROWS:
            prev_sub.add_radiobutton(
                label=ptitle,
                variable=self.preview_mode,
                value=val,
                command=self._on_preview_mode_menu,
            )
        view_m.add_separator()
        view_m.add_checkbutton(
            label="Show Map Blend (Political + Resource)",
            variable=self.show_overlay,
            command=self._schedule_redraw_fast,
        )
        stack_sub = tk.Menu(view_m, tearoff=0)
        view_m.add_cascade(label="Layer Stack (which is on top)", menu=stack_sub)
        stack_sub.add_radiobutton(
            label="Resource on top (see deposits on borders)",
            variable=self.overlay_stack,
            value="resource_on_political",
            command=self._on_overlay_stack_change,
        )
        stack_sub.add_radiobutton(
            label="Political on top (terrain lines over deposits)",
            variable=self.overlay_stack,
            value="political_on_resource",
            command=self._on_overlay_stack_change,
        )
        view_m.add_separator()
        view_m.add_checkbutton(
            label="Strict Ocean Mask (dim sea in Luminance / ΔE only)",
            variable=self.mask_ocean,
            command=self._schedule_redraw_heavy,
        )
        view_m.add_separator()
        view_m.add_command(
            label="Reset Zoom & Pan",
            command=self._reset_view,
            accelerator="0",
        )

        help_m = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_m, underline=0)
        help_m.add_command(
            label="How to use…",
            command=self._help_workflow_dialog,
        )

    def _populate_nation_list(self) -> None:
        if not hasattr(self, "_nation_inner_frame") or not self._nation_inner_frame.winfo_exists():
            return
        
        for child in self._nation_inner_frame.winfo_children():
            child.destroy()
            
        if self._cfg is None:
            return
            
        nations = self._cfg.get("nations") or {}
        
        # Tolerance row
        tol_row = ttk.Frame(self._nation_inner_frame, padding=(0, 0, 0, 10))
        tol_row.pack(fill=tk.X, anchor=tk.W)
        ttk.Label(tol_row, text="Political Fill Tolerance (± per channel):").pack(side=tk.LEFT)
        
        self._tol_var = tk.IntVar(value=self._cfg.get("color_tolerance", 3))
        
        def _on_tol_change(*args):
            try:
                val = self._tol_var.get()
                if self._cfg is not None:
                    self._cfg["color_tolerance"] = val
                    self._bump_cfg_revision()
                    self._invalidate_all_caches()
            except tk.TclError:
                pass
                
        self._tol_var.trace_add("write", _on_tol_change)
        tk.Spinbox(tol_row, from_=0, to=30, textvariable=self._tol_var, width=6).pack(side=tk.LEFT, padx=4)

        # Nations list
        sorted_names = sorted(nations.keys())
        for name in sorted_names:
            col_or_cols = nations[name]
            if not col_or_cols:
                continue
            if isinstance(col_or_cols[0], (int, float)):
                rgbs = [col_or_cols]
            else:
                rgbs = col_or_cols
                
            row = ttk.Frame(self._nation_inner_frame, padding=2)
            row.pack(fill=tk.X, anchor=tk.W)
            
            # Delete button
            def make_delete_cmd(n=name):
                return lambda: self._delete_nation(n)
                
            ttk.Button(row, text="Delete", width=6, command=make_delete_cmd()).pack(side=tk.LEFT)
            
            # Edit button
            def make_edit_cmd(n=name):
                return lambda: self._edit_nation_color(n)
                
            ttk.Button(row, text="Replace", width=7, command=make_edit_cmd()).pack(side=tk.LEFT, padx=(4, 0))
            
            for rgb in rgbs:
                hex_col = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                swatch = tk.Frame(row, width=24, height=16, bg=hex_col, highlightbackground="black", highlightthickness=1)
                swatch.pack(side=tk.LEFT, padx=(10, 0))
            
            # Text
            ttk.Label(row, text=f"  {name}").pack(side=tk.LEFT)
            
        # Ocean colours section
        ocean_colors = self._cfg.get("ocean_colors") or []
        if ocean_colors:
            ttk.Separator(self._nation_inner_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
            ttk.Label(self._nation_inner_frame, text="Ocean Colours", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))
            
            for i, rgb in enumerate(ocean_colors):
                row = ttk.Frame(self._nation_inner_frame, padding=2)
                row.pack(fill=tk.X, anchor=tk.W)
                
                def make_delete_ocean_cmd(idx=i):
                    return lambda: self._delete_ocean_color(idx)
                    
                ttk.Button(row, text="Delete", width=6, command=make_delete_ocean_cmd()).pack(side=tk.LEFT)
                
                def make_edit_ocean_cmd(idx=i):
                    return lambda: self._edit_ocean_color(idx)
                    
                ttk.Button(row, text="Edit", width=4, command=make_edit_ocean_cmd()).pack(side=tk.LEFT, padx=(4, 0))
                
                hex_col = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                swatch = tk.Frame(row, width=24, height=16, bg=hex_col, highlightbackground="black", highlightthickness=1)
                swatch.pack(side=tk.LEFT, padx=(10, 10))
                
                ttk.Label(row, text=f"Ocean {i+1}: {rgb}").pack(side=tk.LEFT)

    def _delete_nation(self, name: str) -> None:
        if self._cfg and "nations" in self._cfg and name in self._cfg["nations"]:
            del self._cfg["nations"][name]
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            self._log(f"Deleted nation: {name}")
            self._populate_nation_list()
            self._refresh_nation_combobox()
            self._update_nation_ui_state()
            self._schedule_redraw_heavy()
            
    def _edit_nation_color(self, name: str) -> None:
        if self._cfg and "nations" in self._cfg and name in self._cfg["nations"]:
            if self._last_pol_rgb is not None:
                r, g, b = [int(c) for c in self._last_pol_rgb]
                self._cfg["nations"][name] = [r, g, b]
                self._bump_cfg_revision()
                self._invalidate_all_caches()
                self._log(f"Changed colour for {name} to {[r, g, b]} (from eyedropper)")
                self._populate_nation_list()
                self._schedule_redraw_heavy()
            else:
                messagebox.showinfo("Edit Nation", "Please use the Political Eyedropper to sample a new colour from the map first.")
            
    def _delete_ocean_color(self, idx: int) -> None:
        if self._cfg and "ocean_colors" in self._cfg and 0 <= idx < len(self._cfg["ocean_colors"]):
            removed = self._cfg["ocean_colors"].pop(idx)
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            if self._pol is not None:
                self._ocean_pol = ocean_mask_exact(self._pol, self._cfg["ocean_colors"])
            self._log(f"Deleted ocean colour: {removed}")
            self._populate_nation_list()
            self._schedule_redraw_heavy()

    def _edit_ocean_color(self, idx: int) -> None:
        if self._cfg and "ocean_colors" in self._cfg and 0 <= idx < len(self._cfg["ocean_colors"]):
            if self._last_pol_rgb is not None:
                r, g, b = [int(c) for c in self._last_pol_rgb]
                self._cfg["ocean_colors"][idx] = [r, g, b]
                self._bump_cfg_revision()
                self._invalidate_all_caches()
                if self._pol is not None:
                    self._ocean_pol = ocean_mask_exact(self._pol, self._cfg["ocean_colors"])
                self._log(f"Changed ocean colour {idx+1} to {[r, g, b]} (from eyedropper)")
                self._populate_nation_list()
                self._schedule_redraw_heavy()
            else:
                messagebox.showinfo("Edit Ocean", "Please use the Political Eyedropper to sample a new colour from the map first.")

    def _show_nation_list_window(self) -> None:
        if self._nation_list_window is not None:
            try:
                self._nation_list_window.lift()
                return
            except tk.TclError:
                self._nation_list_window = None

        if self._cfg is None:
            messagebox.showinfo("Nations", "Load a project (config.yaml) first.")
            return

        win = tk.Toplevel(self)
        win.title("Manage Nations & Ocean")
        win.geometry("450x600")
        self._nation_list_window = win

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Nations & Ocean Colours", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 10))

        # Scrollable list
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        
        canvas = tk.Canvas(list_frame, highlightthickness=0)
        vbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self._nation_inner_frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self._nation_inner_frame, anchor=tk.NW)

        def _on_cfg(e: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
        self._nation_inner_frame.bind("<Configure>", _on_cfg)

        # Mouse wheel for the nation list window
        def _on_mousewheel(event: tk.Event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        win.bind_all("<MouseWheel>", _on_mousewheel)

        self._populate_nation_list()

        ttk.Button(frame, text="Close", command=win.destroy).pack(pady=(10, 0))

    def _on_preview_mode_menu(self) -> None:
        self._sync_preview_mode_combo()
        self._on_preview_mode()

    def _sync_preview_mode_combo(self) -> None:
        self._preview_mode_display.set(_preview_label_for_key(self.preview_mode.get()))

    def _on_preview_mode_combo_selected(self, _event: tk.Event | None = None) -> None:
        lab = self._preview_mode_display.get()
        key = _preview_key_for_label(lab)
        if key:
            self.preview_mode.set(key)
        self._on_preview_mode()

    def _help_workflow_dialog(self) -> None:
        messagebox.showinfo(
            "How to use",
            "Suggested order:\n\n"
            "1. File → Open Project\n"
            "2. Maps → Load Political + Resource Maps\n"
            "3. Sidebar: Use Political Eyedropper to add nations/ocean\n"
            "4. Workflow → View & Manage Nations to check/delete\n"
            "5. Sidebar: Use Lasso or Resource Eyedropper\n"
            "6. File → Save Project when ready\n"
            "7. Workflow → Run Full Analysis to get CSV/JSON results\n"
            "8. View → Change View Mode or Blend to check the map\n\n"
            "Shortcuts: 0 = reset zoom; arrows = pan; +/- = zoom.\n"
            "Ctrl+O / Ctrl+S / Ctrl+Q = open, save, exit.",
            parent=self,
        )

    def _bind_sidebar_scroll_handlers(self, inner: tk.Misc) -> None:
        """Wheel over any sidebar control scrolls the sidebar (listboxes don't eat the wheel)."""

        def wheel(event: tk.Event) -> str:
            d = getattr(event, "delta", 0) or 0
            if d:
                self._side_canvas.yview_scroll(int(-1 * (d / 120)), "units")
            return "break"

        def wheel_up(_event: tk.Event) -> str:
            self._side_canvas.yview_scroll(-1, "units")
            return "break"

        def wheel_dn(_event: tk.Event) -> str:
            self._side_canvas.yview_scroll(1, "units")
            return "break"

        for w in _walk_tk_widgets(inner):
            w.bind("<MouseWheel>", wheel)
            w.bind("<Button-4>", wheel_up)
            w.bind("<Button-5>", wheel_dn)
        self._side_canvas.bind("<MouseWheel>", wheel)
        self._side_canvas.bind("<Button-4>", wheel_up)
        self._side_canvas.bind("<Button-5>", wheel_dn)

    def _on_preview_mode(self) -> None:
        self._schedule_redraw_heavy()

    def _wizard_refresh_ui(self) -> None:
        ph = self._wiz_phase
        if ph == "idle":
            self._wiz_progress.config(text="")
            self._wiz_help.config(
                text=(
                    "Start walks through every commodity: eyedrop samples, Next saves each. "
                    "Then tune global/per-commodity ΔE, optional lasso for extra hints, then save YAML. "
                    "Works on an existing config (refine anchors) or minimal + Add commodity first."
                )
            )
            try:
                self._wiz_tune_row.pack_forget()
            except tk.TclError:
                pass
            self.commodity_combo.config(state="readonly")
            self._btn_wiz_back.state(["disabled"])
            self._btn_wiz_next.state(["disabled"])
            return

        self._btn_wiz_back.state(["!disabled"])
        self._btn_wiz_next.state(["!disabled"])

        if ph == "eye":
            try:
                self._wiz_tune_row.pack_forget()
            except tk.TclError:
                pass
            self.commodity_combo.config(state="disabled")
            c = self._wiz_list[self._wiz_idx]
            self._wiz_progress.config(
                text=f"Step 1 of 4 — Sample colours ({self._wiz_idx + 1}/{len(self._wiz_list)})"
            )
            self._wiz_help.config(
                text=(
                    f"Commodity: {c}. Eyedropper is on — click deposits or legend swatches. "
                    "Next saves this commodity (skip if list empty). "
                    "On an existing config, sampled anchors replace the in-memory list when you Next."
                )
            )
            self.commodity_var.set(c)
            self._on_commodity_change()
            self.map_tool.set("eyedropper")
            self._schedule_redraw_heavy()
            return

        if ph == "tune":
            self.commodity_combo.config(state="readonly")
            self._wiz_tune_row.pack(anchor=tk.W, fill=tk.X, pady=(6, 0))
            dg = self._cfg.get("resource_max_delta_e", 22) if self._cfg else 22
            self.wizard_global_maxde.set(
                str(int(dg)) if float(dg) == int(dg) else str(dg)
            )
            self._wiz_progress.config(text="Step 2 of 4 — Tune tolerances")
            self._wiz_help.config(
                text=(
                    "Apply global sets resource_max_delta_e. Per commodity: use the main "
                    "Commodity dropdown, edit Commodity max ΔE, Apply anchors → memory "
                    "(list reloads from config; you can add eyedropper samples first if needed). "
                    "Check Mask / ΔE views. Next when ready."
                )
            )
            self._schedule_redraw_heavy()
            return

        if ph == "lasso_opt":
            try:
                self._wiz_tune_row.pack_forget()
            except tk.TclError:
                pass
            self.commodity_combo.config(state="readonly")
            self._wiz_progress.config(text="Step 3 of 4 — Optional lasso")
            self._wiz_help.config(
                text=(
                    "Optional: set Map clicks to Lasso, choose a commodity, "
                    "draw polygons, Close lasso → log for bucket/blob YAML hints. "
                    "Or stay on eyedropper. Next when finished or to skip."
                )
            )
            self.map_tool.set("lasso")
            self._schedule_redraw_heavy()
            return

        if ph == "done":
            try:
                self._wiz_tune_row.pack_forget()
            except tk.TclError:
                pass
            self.commodity_combo.config(state="readonly")
            self._wiz_progress.config(text="Step 4 of 4 — Finish")
            self._wiz_help.config(
                text=(
                    "Save resource_legend fragment… (below), merge into config.yaml, "
                    "run analyze_resources or your batch. Cancel wizard when finished."
                )
            )
            self._schedule_redraw_heavy()

    def _wizard_start(self) -> None:
        if self._res is None:
            messagebox.showinfo("Wizard", "Load the resource PNG first.")
            return
        if self._cfg is None:
            if not messagebox.askyesno(
                "Wizard",
                "No config in memory. Create a minimal stub (resource_* keys only) now? "
                "Then use Add commodity… if resource_legend is empty.",
            ):
                return
            self._cfg = {
                "resource_max_delta_e": 22.0,
                "resource_legend": {},
                "resource_exclude_colors": [],
                "resource_exclude_tolerance": 0,
                "ocean_colors": [],
            }
            self._anchor_session = {}
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            self._refresh_commodities()
            self._sync_commodity_max_de_from_cfg()
            self._refresh_anchor_listbox()
            self._log("Wizard: created minimal config in memory.")
        keys = list((self._cfg.get("resource_legend") or {}).keys())
        if not keys:
            messagebox.showinfo(
                "Wizard",
                "resource_legend has no commodities. Use Add commodity… then Start wizard again.",
            )
            return
        self._wiz_list = keys
        self._wiz_idx = 0
        self._wiz_phase = "eye"
        self._wizard_refresh_ui()
        self._log(
            f"Wizard: {len(keys)} commodities — eyedrop each, Next to save and continue."
        )

    def _wizard_cancel(self) -> None:
        self._wiz_phase = "idle"
        self._wiz_list = []
        self._wiz_idx = 0
        self._wizard_refresh_ui()
        self._log("Wizard cancelled.")

    def _wizard_back(self) -> None:
        if self._wiz_phase == "idle":
            return
        if self._wiz_phase == "eye":
            if self._wiz_idx > 0:
                self._wiz_idx -= 1
                self._wizard_refresh_ui()
            return
        if self._wiz_phase == "tune":
            self._wiz_phase = "eye"
            self._wiz_idx = max(0, len(self._wiz_list) - 1)
            self._wizard_refresh_ui()
            return
        if self._wiz_phase == "lasso_opt":
            self._wiz_phase = "tune"
            self._wizard_refresh_ui()
            return
        if self._wiz_phase == "done":
            self._wiz_phase = "lasso_opt"
            self._wizard_refresh_ui()

    def _wizard_next(self) -> None:
        if self._wiz_phase == "idle":
            return
        if self._wiz_phase == "eye":
            c = self._wiz_list[self._wiz_idx]
            try:
                if self._get_session_anchors(c):
                    if self._flush_session_to_cfg_commodity(c):
                        self._bump_cfg_revision()
                        self._invalidate_all_caches()
                        self._log(f"Wizard: saved anchors for {c!r}.")
                    else:
                        self._log(f"Wizard: nothing to flush for {c!r}.")
                else:
                    self._log(f"Wizard: skipped {c!r} (no samples in list).")
            except (ValueError, TypeError) as e:
                messagebox.showerror("Wizard", str(e))
                return
            self._wiz_idx += 1
            if self._wiz_idx >= len(self._wiz_list):
                self._wiz_phase = "tune"
                self._wiz_idx = 0
            self._wizard_refresh_ui()
            self._schedule_redraw_heavy()
            return
        if self._wiz_phase == "tune":
            self._wiz_phase = "lasso_opt"
            self._wizard_refresh_ui()
            return
        if self._wiz_phase == "lasso_opt":
            self._wiz_phase = "done"
            self._wizard_refresh_ui()
            return
        if self._wiz_phase == "done":
            messagebox.showinfo(
                "Wizard",
                "Use Save resource_legend fragment… in the sidebar when you are ready.",
            )

    def _wizard_apply_global_de(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Wizard", "No config loaded.")
            return
        try:
            v = float(self.wizard_global_maxde.get().strip())
        except ValueError:
            messagebox.showerror("Wizard", "Global max ΔE must be a number.")
            return
        self._cfg["resource_max_delta_e"] = float(v)
        self._bump_cfg_revision()
        self._invalidate_all_caches()
        self._log(f"Wizard: resource_max_delta_e = {v}")
        self._schedule_redraw_heavy()

    def _on_overlay_stack_change(self) -> None:
        """Map stack only affects the display when blend is on — auto-enable if possible."""
        if self._pol is not None:
            self.show_overlay.set(True)
        self._schedule_redraw_fast()

    def _invalidate_eez_overlay(self) -> None:
        self._eez_cache_key = None
        self._eez_overlay_rgb = None
        self._eez_overlay_alpha = None
        self._eez_compute_token += 1

    def _update_eez_km_hint(self) -> None:
        if not getattr(self, "_eez_km_hint", None):
            return
        try:
            px = int(self.eez_halo_px.get())
        except tk.TclError:
            self._eez_km_hint.config(text="")
            return
        if not self._cfg:
            self._eez_km_hint.config(text="")
            return
        k = float(self._cfg.get("km_per_pixel", 6.0))
        km = px * k
        self._eez_km_hint.config(text=f"≈ {km:.0f} km at km_per_pixel={k:g}")

    def _eez_halo_px_trace(self, *_a: Any) -> None:
        try:
            int(self.eez_halo_px.get())
        except tk.TclError:
            return
        self._invalidate_eez_overlay()
        self._update_eez_km_hint()
        self._schedule_redraw_fast()

    def _on_eez_title_attach_toggle(self) -> None:
        self._invalidate_eez_overlay()
        self._schedule_redraw_fast()

    def _on_eez_overlay_toggle(self) -> None:
        if not self.show_eez_overlay.get():
            self._invalidate_eez_overlay()
            self._schedule_redraw_fast()
            return
        if self._pol is None:
            messagebox.showinfo("EEZ overlay", "Load political PNG first.")
            self.show_eez_overlay.set(False)
            return
        if self._cfg is None:
            messagebox.showinfo("EEZ overlay", "Load config.yaml (nations + ocean_colors) first.")
            self.show_eez_overlay.set(False)
            return
        self.show_overlay.set(True)
        self._schedule_redraw_fast()

    def _eez_async_done(
        self,
        start_token: int,
        key: tuple,
        rgb: np.ndarray | None,
        alpha: np.ndarray | None,
        offshore_owner: np.ndarray | None,
        nation_order: list[str] | None,
        land_masks: dict[str, np.ndarray] | None,
        err: BaseException | None,
    ) -> None:
        self._eez_async_busy = False
        try:
            self._eez_prog.stop()
            self._eez_prog.pack_forget()
        except tk.TclError:
            pass
        self._eez_status_label.configure(text="")
        if start_token != self._eez_compute_token:
            self._schedule_redraw_fast()
            return
        if not self.show_eez_overlay.get():
            self._schedule_redraw_fast()
            return
        if err is not None:
            self._log(f"EEZ overlay failed: {err}")
            self._schedule_redraw_fast()
            return
        assert rgb is not None and alpha is not None
        self._eez_overlay_rgb = rgb
        self._eez_overlay_alpha = alpha
        self._eez_offshore_owner = offshore_owner
        self._eez_nation_order = nation_order
        self._eez_land_masks = land_masks
        self._eez_cache_key = key
        self._schedule_redraw_fast()

    def _kick_eez_async_compute(self, key: tuple, halo_px: int, use_title: bool) -> None:
        if self._eez_async_busy or self._pol is None or self._cfg is None:
            return
        self._eez_async_busy = True
        start_token = self._eez_compute_token
        pol = self._pol
        cfg = self._cfg

        self._eez_status_label.configure(text="Computing EEZ overlay (background)…")
        self._eez_prog.pack(anchor=tk.W, pady=(4, 0))
        self._eez_prog.start(10)

        def work() -> None:
            try:
                # Always calculate on the FULL political map, not a crop
                rgb, alpha, offshore_owner, nation_order, land_masks = _compute_eez_overlay_layers(pol, cfg, halo_px, use_title)

                def finish() -> None:
                    self._eez_async_done(start_token, key, rgb, alpha, offshore_owner, nation_order, land_masks, None)

                self.after(0, finish)
            except BaseException as e:

                def finish_err() -> None:
                    self._eez_async_done(start_token, key, None, None, None, None, None, e)

                self.after(0, finish_err)

        threading.Thread(target=work, daemon=True).start()

    def _get_eez_overlay_for_draw(
        self,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        if (
            not self.show_eez_overlay.get()
            or self._pol is None
            or self._cfg is None
        ):
            return None, None
        try:
            halo_px = int(self.eez_halo_px.get())
        except tk.TclError:
            return None, None
        if halo_px <= 0:
            return None, None
        use_title = self.eez_use_title_attach.get()
        key = (id(self._pol), id(self._cfg), self._cfg_revision, halo_px, use_title)
        
        if (
            self._eez_cache_key == key
            and self._eez_overlay_rgb is not None
            and self._eez_overlay_alpha is not None
        ):
            return self._eez_overlay_rgb, self._eez_overlay_alpha
        self._kick_eez_async_compute(key, halo_px, use_title)
        return None, None

    def _lasso_de_info(
        self, mean_u8: np.ndarray, commodity: str
    ) -> tuple[float, list[int]]:
        if (
            not self._cfg
            or commodity not in (self._cfg.get("resource_legend") or {})
        ):
            t = mean_u8.astype(int).tolist()
            return 0.0, [int(t[0]), int(t[1]), int(t[2])]
        default_de = float(self._cfg.get("resource_max_delta_e", 18.0))
        anchors, _ = ar.parse_commodity_spec(
            self._cfg["resource_legend"][commodity], default_de
        )
        lab_px = ar.rgb_to_lab(mean_u8.reshape(1, 1, 3).astype(np.uint8)).reshape(3)
        lab_a = ar.rgb_to_lab(
            np.asarray(anchors, dtype=np.uint8).reshape(-1, 1, 1, 3)
        ).reshape(-1, 3)
        d2 = np.sum((lab_a - lab_px) ** 2, axis=1)
        dmin = float(np.sqrt(np.min(d2)))
        t = mean_u8.astype(int).tolist()
        return dmin, [int(t[0]), int(t[1]), int(t[2])]

    def _on_commodity_change(self, _event=None) -> None:
        self._preview_key = None
        self._preview_u8 = None
        if self.de_override.get().strip():
            self._masks_cache_key = None
            self._masks_cache = None
        self._sync_commodity_max_de_from_cfg()
        self._refresh_anchor_listbox()
        self._schedule_redraw_heavy()

    def _on_de_keyrelease(self, _event=None) -> None:
        self._invalidate_masks_and_preview()
        self._schedule_redraw_heavy_debounced()

    def _invalidate_all_caches(self) -> None:
        self._masks_cache_key = None
        self._masks_cache = None
        self._preview_key = None
        self._preview_u8 = None
        self._lab_cache_id = None
        self._lab_cache_arr = None
        self._de_dist_cache.clear()
        self._invalidate_eez_overlay()
        self._eez_offshore_owner = None
        self._eez_nation_order = None
        self._eez_land_masks = None

    def _reload_eez_full(self) -> None:
        """Clear EEZ cache and force a fresh background compute."""
        if self._cfg:
            self._cfg.pop("eez_cache", None)
        self._invalidate_eez_overlay()
        self._eez_offshore_owner = None
        self._eez_nation_order = None
        self._eez_land_masks = None
        self._log("EEZ cache cleared. Recalculating...")
        self._kick_eez_async_compute()

    def _invalidate_masks_and_preview(self) -> None:
        self._masks_cache_key = None
        self._masks_cache = None
        self._preview_key = None
        self._preview_u8 = None

    def _mask_cache_identity(self) -> tuple | None:
        """Masks depend on full cfg, except when max ΔE override patches one commodity."""
        if self._res is None or self._cfg is None:
            return None
        de = self._get_de_override()
        if de is None:
            return (id(self._res), id(self._cfg), self._cfg_revision, None)
        return (
            id(self._res),
            id(self._cfg),
            self._cfg_revision,
            self.commodity_var.get(),
            float(de),
        )

    def _ensure_masks(self) -> dict[str, np.ndarray]:
        key = self._mask_cache_identity()
        assert key is not None
        if self._masks_cache_key == key and self._masks_cache is not None:
            return self._masks_cache
        de = self._get_de_override()
        com = self.commodity_var.get()
        cfg_eff = (
            self._cfg if de is None else _legend_with_de_override(self._cfg, com, de)
        )
        self._masks_cache = ar.resource_masks(self._res, cfg_eff)
        self._masks_cache_key = key
        return self._masks_cache

    def _get_lab(self) -> np.ndarray:
        rid = id(self._res)
        if self._lab_cache_id == rid and self._lab_cache_arr is not None:
            return self._lab_cache_arr
        self._lab_cache_arr = ar.rgb_to_lab(self._res.astype(np.uint8))
        self._lab_cache_id = rid
        return self._lab_cache_arr

    def _get_delta_e_dist(self, commodity: str) -> np.ndarray:
        assert self._res is not None and self._cfg is not None
        key = (id(self._res), id(self._cfg), self._cfg_revision, commodity)
        hit = self._de_dist_cache.get(key)
        if hit is not None:
            return hit
        default_de = float(self._cfg.get("resource_max_delta_e", 18.0))
        anchors, _ = ar.parse_commodity_spec(
            self._cfg["resource_legend"][commodity], default_de
        )
        lab = self._get_lab()
        d = ar.min_delta_e_to_anchors(lab, anchors)
        self._de_dist_cache[key] = d
        return d

    def _warm_all_start(self) -> None:
        if self._warm_busy:
            return
        if self._res is None or self._cfg is None:
            messagebox.showinfo(
                "Warm all caches",
                "Load a resource PNG and config.yaml first.",
            )
            return
        commodities = list((self._cfg.get("resource_legend") or {}).keys())
        if not commodities:
            messagebox.showinfo("Warm all caches", "resource_legend is empty.")
            return
        self._warm_busy = True
        self._warm_commodities = commodities
        self._warm_total = 2 + len(commodities)
        self._warm_step = 0
        self._warm_progress.configure(maximum=self._warm_total, value=0)
        self._warm_status.configure(text="starting…")
        self._log(
            f"Warm cache: LAB + masks + ΔE fields for {len(commodities)} commodities…"
        )
        self.after(1, self._warm_all_tick)

    def _warm_all_tick(self) -> None:
        if not self._warm_busy:
            return
        try:
            s = self._warm_step
            if s == 0:
                self._warm_status.configure(text="LAB…")
                self._get_lab()
            elif s == 1:
                self._warm_status.configure(text="masks…")
                self._ensure_masks()
            elif s - 2 < len(self._warm_commodities):
                c = self._warm_commodities[s - 2]
                self._warm_status.configure(text=f"ΔE {c[:12]}…")
                self._get_delta_e_dist(c)
            else:
                self._warm_busy = False
                self._warm_status.configure(text="done")
                self._warm_progress.configure(value=self._warm_total)
                self._log(
                    "Warm cache: done (switch View/Commodity should be faster)."
                )
                self._schedule_redraw_heavy()
                return
        except Exception as e:
            self._warm_busy = False
            self._warm_status.configure(text="error")
            self._log(f"Warm cache failed: {e}")
            messagebox.showerror("Warm cache", str(e))
            return

        self._warm_step += 1
        self._warm_progress.configure(value=min(self._warm_step, self._warm_total))
        self.update_idletasks()
        self.after(1, self._warm_all_tick)

    def _build_preview_u8(self) -> np.ndarray | None:
        if self._res is None or self._cfg is None:
            return None
        rgb = self._res
        mode = self.preview_mode.get()
        commodity = self.commodity_var.get()
        ocean = self._ocean_pol if self.mask_ocean.get() else None
        key2 = (
            self._mask_cache_identity(),
            commodity,
            mode,
            self.mask_ocean.get(),
            id(ocean) if ocean is not None else None,
        )
        leg = self._cfg.get("resource_legend") or {}
        if commodity not in leg and mode in _PREVIEW_COMMODITY_MODES:
            if self._preview_key == key2 and self._preview_u8 is not None:
                return self._preview_u8
            out = rgb.copy()
            self._preview_u8 = out
            self._preview_key = key2
            return out

        if self._preview_key == key2 and self._preview_u8 is not None:
            return self._preview_u8

        excl = ar.color_close(
            rgb,
            list(self._cfg.get("resource_exclude_colors", [])),
            int(self._cfg.get("resource_exclude_tolerance", 0)),
        )

        if mode == "resource":
            out = rgb.copy()
        elif mode == "grey":
            out = rgb_greyscale(rgb)
            if ocean is not None:
                out = out.copy()
                out[ocean] = (out[ocean] * 0.35).astype(np.uint8)
        elif mode == "delta_e":
            default_de = float(self._cfg.get("resource_max_delta_e", 18.0))
            _, de_spec = ar.parse_commodity_spec(
                self._cfg["resource_legend"][commodity], default_de
            )
            de = self._get_de_override()
            if de is None:
                de = de_spec
            t = float(de)
            d = self._get_delta_e_dist(commodity)
            norm = np.clip(d / max(t, 1.0), 0, 1)
            rch = (255 * norm).astype(np.uint8)
            bch = (255 * (1 - norm)).astype(np.uint8)
            gch = np.zeros_like(rch)
            out = np.stack([rch, gch, bch], axis=-1)
            if ocean is not None:
                out = out.copy()
                out[ocean] //= 3
        elif mode == "diag_spectral":
            diag = ar.resource_spectral_diagnostics(rgb, self._cfg)
            out = rgb.astype(np.float32)
            if diag:
                void = diag["spectral_void"]
                amb = diag["oil_gas_ambiguous"]
                purp = np.array([170.0, 30.0, 200.0], dtype=np.float32)
                out = np.where(void[..., None], out * 0.55 + purp * 0.45, out)
                yy, xx = np.indices((rgb.shape[0], rgb.shape[1]))
                hatch = ((xx + yy) % 8) >= 4
                red = np.array([240.0, 35.0, 35.0], dtype=np.float32)
                amb_h = amb & hatch
                out = np.where(amb_h[..., None], out * 0.52 + red * 0.48, out)
            out = np.clip(out, 0, 255).astype(np.uint8)
        else:
            try:
                masks = self._ensure_masks()
            except KeyError:
                out = rgb.copy()
                self._preview_u8 = out
                self._preview_key = key2
                return out
            cmask = masks[commodity] & ~excl
            if mode == "mask_wb":
                out = np.zeros_like(rgb)
                out[cmask] = 255
            elif mode == "pitch_black":
                out = np.zeros_like(rgb)
                out[cmask] = rgb[cmask]
            else:
                out = rgb.copy()

        self._preview_u8 = out
        self._preview_key = key2
        return out

    def _stack_maps(self, resource_preview: np.ndarray) -> np.ndarray:
        """Composite political + resource preview. Top layer uses blend α."""
        if not self.show_overlay.get() or self._pol is None:
            return resource_preview
        a = int(self.overlay_alpha.get()) / 100.0
        if a <= 0:
            return resource_preview
        pol = self._pol.astype(np.float32)
        prev = resource_preview.astype(np.float32)
        if self.overlay_stack.get() == "resource_on_political":
            # Political under, resource semi-transparent on top (deposit-tuner default).
            return np.clip((1.0 - a) * pol + a * prev, 0, 255).astype(np.uint8)
        # Resource under, political on top (legacy behaviour).
        return np.clip((1.0 - a) * prev + a * pol, 0, 255).astype(np.uint8)

    def _schedule_redraw_fast(self, _event=None) -> None:
        if self._draw_job:
            self.after_cancel(self._draw_job)
        self._draw_job = self.after(1, self._draw_now)

    def _schedule_redraw_heavy(self) -> None:
        if self._draw_job:
            self.after_cancel(self._draw_job)
        self._draw_job = self.after(1, self._draw_now_heavy)

    def _schedule_redraw_heavy_debounced(self) -> None:
        if self._draw_job:
            self.after_cancel(self._draw_job)
        self._draw_job = self.after(250, self._draw_now_heavy)

    def _schedule_overlay_redraw(self) -> None:
        if self._overlay_job:
            self.after_cancel(self._overlay_job)
        self._overlay_job = self.after(90, self._overlay_tick)

    def _overlay_tick(self) -> None:
        self._overlay_job = None
        self._schedule_redraw_fast()

    def _draw_now_heavy(self) -> None:
        self._draw_job = None
        self._draw_now()

    def _draw_now(self) -> None:
        self._draw_job = None
        if self._res is None:
            self._map_vp = None
            return
        vp = self._display_viewport()
        if vp is None:
            self.canvas.delete("all")
            self._map_vp = None
            return
        (
            ox,
            oy,
            display_scale,
            ix_start,
            iy_start,
            ix_end,
            iy_end,
            draw_x,
            draw_y,
            disp_w,
            disp_h,
            crop_w,
            crop_h,
            iw,
            ih,
        ) = vp

        # Get the cropped preview
        prev_full = self._build_preview_u8()
        if prev_full is None:
            self._map_vp = None
            return

        prev_crop = prev_full[iy_start:iy_end, ix_start:ix_end]

        # Stack maps on the crop
        if self.show_overlay.get() and self._pol is not None:
            a = int(self.overlay_alpha.get()) / 100.0
            pol_crop = self._pol[iy_start:iy_end, ix_start:ix_end].astype(np.float32)
            prev_crop_f32 = prev_crop.astype(np.float32)
            if self.overlay_stack.get() == "resource_on_political":
                blended_crop = np.clip((1.0 - a) * pol_crop + a * prev_crop_f32, 0, 255).astype(np.uint8)
            else:
                blended_crop = np.clip((1.0 - a) * prev_crop_f32 + a * pol_crop, 0, 255).astype(np.uint8)
        else:
            blended_crop = prev_crop

        # Overlay EEZ on the crop
        rgb_eez, a_eez = self._get_eez_overlay_for_draw()
        if rgb_eez is not None and a_eez is not None:
            eez_crop_rgb = rgb_eez[iy_start:iy_end, ix_start:ix_end]
            eez_crop_a = a_eez[iy_start:iy_end, ix_start:ix_end, np.newaxis]

            blended_crop = np.clip(
                blended_crop.astype(np.float32) * (1.0 - eez_crop_a) + eez_crop_rgb * eez_crop_a,
                0,
                255,
            ).astype(np.uint8)

        if disp_w <= 0 or disp_h <= 0:
            self._map_vp = None
            return

        img = Image.fromarray(blended_crop, mode="RGB").resize(
            (disp_w, disp_h), Image.Resampling.BILINEAR
        )
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")

        self._map_vp = vp
        self._disp_scale = display_scale
        self._disp_ox = ox
        self._disp_oy = oy
        self.canvas.create_image(draw_x, draw_y, anchor=tk.NW, image=self._photo)
        self.zoom_label.config(text=f"{self._zoom:.2f}×")
        if len(self._lasso_pts) >= 2:
            cvs: list[int] = []
            for ix, iy in self._lasso_pts:
                pr = self._resource_ixiy_to_canvas_xy(ix, iy, vp)
                if pr is None:
                    continue
                cx, cy = pr
                cvs.extend([int(round(cx)), int(round(cy))])
            for i in range(0, len(cvs) - 2, 2):
                self.canvas.create_line(
                    cvs[i], cvs[i + 1], cvs[i + 2], cvs[i + 3], fill="#0f0", width=2
                )
        self._draw_closed_lassos(vp)
        self._draw_saved_zones(vp)

    def _draw_closed_lassos(self, vp: tuple) -> None:
        for poly in self._lasso_closed_polys:
            if len(poly) < 2:
                continue
            for j in range(len(poly)):
                x0, y0 = poly[j]
                x1, y1 = poly[(j + 1) % len(poly)]
                p0 = self._resource_ixiy_to_canvas_xy(int(x0), int(y0), vp)
                p1 = self._resource_ixiy_to_canvas_xy(int(x1), int(y1), vp)
                if p0 is None or p1 is None:
                    continue
                self.canvas.create_line(
                    int(round(p0[0])),
                    int(round(p0[1])),
                    int(round(p1[0])),
                    int(round(p1[1])),
                    fill="#ff8800",
                    width=2,
                )

    def _on_canvas_configure(self, _event: tk.Event) -> None:
        if self._configure_job:
            self.after_cancel(self._configure_job)
        self._configure_job = self.after(140, self._configure_done)

    def _configure_done(self) -> None:
        self._configure_job = None
        self._schedule_redraw_fast()

    def _draw_saved_zones(self, vp: tuple) -> None:
        for i, z in enumerate(self._zones):
            pts = z.get("points_xy") or []
            if len(pts) < 2:
                continue
            sel = self._selected_zone_ix == i
            col = "#ff00ff" if sel else "#00ffff"
            w = 3 if sel else 2
            for j in range(len(pts)):
                x0, y0 = pts[j]
                x1, y1 = pts[(j + 1) % len(pts)]
                p0 = self._resource_ixiy_to_canvas_xy(int(x0), int(y0), vp)
                p1 = self._resource_ixiy_to_canvas_xy(int(x1), int(y1), vp)
                if p0 is None or p1 is None:
                    continue
                self.canvas.create_line(
                    int(round(p0[0])),
                    int(round(p0[1])),
                    int(round(p1[0])),
                    int(round(p1[1])),
                    fill=col,
                    width=w,
                )

    def _reset_view(self) -> None:
        self._zoom = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._log("View reset (zoom 1, pan 0)")
        self._schedule_redraw_fast()

    def _zoom_at_center(self, factor: float) -> None:
        if self._res is None:
            return
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 400)
        self._zoom_toward(factor, cw // 2, ch // 2)

    def _zoom_toward(self, factor: float, mx: int, my: int) -> None:
        if self._res is None:
            return
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 400)
        ih, iw = self._res.shape[:2]
        sc_fit = min(cw / iw, ch / ih, 1.0)
        if sc_fit > 1.0:
            sc_fit = 1.0
        scale_old = sc_fit * self._zoom
        if scale_old <= 0:
            return
        nw_old = max(1, int(iw * scale_old))
        nh_old = max(1, int(ih * scale_old))
        ox_old = (cw - nw_old) // 2 + self._pan_x
        oy_old = (ch - nh_old) // 2 + self._pan_y
        ix = (mx - ox_old) / scale_old
        iy = (my - oy_old) / scale_old
        self._zoom = max(0.25, min(16.0, self._zoom * factor))
        self.zoom_var.set(self._zoom)
        scale_new = sc_fit * self._zoom
        nw_new = max(1, int(iw * scale_new))
        nh_new = max(1, int(ih * scale_new))
        self._pan_x = int(mx - ix * scale_new - (cw - nw_new) // 2)
        self._pan_y = int(my - iy * scale_new - (ch - nh_new) // 2)
        self._schedule_redraw_fast()

    def _on_zoom_slider(self, val: str) -> None:
        if self._res is None:
            return
        try:
            target_zoom = float(val)
        except ValueError:
            return
        factor = target_zoom / self._zoom
        self._zoom_at_center(factor)

    def _on_canvas_mousewheel(self, event: tk.Event) -> None:
        if self._res is None:
            return
        steps = getattr(event, "delta", 0) or 0
        if steps == 0:
            return
            
        # Control key held -> Zoom
        if event.state & 0x0004:
            factor = 1.1 ** (steps / 120.0)
            self._zoom_toward(factor, event.x, event.y)
        # Shift key held -> Horizontal pan
        elif event.state & 0x0001:
            self._pan_x += int(steps * 0.5)
            self._schedule_redraw_fast()
        # Otherwise -> Vertical pan
        else:
            self._pan_y += int(steps * 0.5)
            self._schedule_redraw_fast()

    def _on_canvas_mousewheel_linux(self, event: tk.Event, direction: int) -> None:
        if self._res is None:
            return
        steps = direction * 120
        
        if event.state & 0x0004:
            factor = 1.1 ** (steps / 120.0)
            self._zoom_toward(factor, event.x, event.y)
        elif event.state & 0x0001:
            self._pan_x += int(steps * 0.5)
            self._schedule_redraw_fast()
        else:
            self._pan_y += int(steps * 0.5)
            self._schedule_redraw_fast()

    def _canvas_enter(self, _event: tk.Event) -> None:
        if not self._in_log():
            self.canvas.focus_set()

    def _pan_start(self, event: tk.Event) -> None:
        self._pan_drag = (float(event.x), float(event.y))

    def _pan_move(self, event: tk.Event) -> None:
        if self._pan_drag is None:
            return
        px, py = self._pan_drag
        self._pan_x += int(event.x - px)
        self._pan_y += int(event.y - py)
        self._pan_drag = (float(event.x), float(event.y))
        self._schedule_redraw_fast()

    def _pan_end(self, _event: tk.Event) -> None:
        self._pan_drag = None

    def _pan_keys(self, dx: int, dy: int) -> None:
        if self._res is None:
            return
        cw = max(self.canvas.winfo_width(), 400)
        step = max(24, cw // 40)
        self._pan_x += dx * step
        self._pan_y += dy * step
        self._schedule_redraw_fast()

    def _in_log(self) -> bool:
        w = self.focus_get()
        return isinstance(w, scrolledtext.ScrolledText) or (
            isinstance(w, tk.Text) and w is self.log
        )

    def _in_any_text_field(self) -> bool:
        """True when focus is a widget where Ctrl+O/S/Q should not fire."""
        w = self.focus_get()
        if w is None:
            return False
        if isinstance(w, scrolledtext.ScrolledText):
            return True
        if isinstance(w, tk.Text):
            return True
        if isinstance(w, (tk.Entry, ttk.Entry)):
            return True
        if isinstance(w, (tk.Spinbox, ttk.Spinbox)):
            return True
        return False

    def _global_shortcuts(self, event: tk.Event) -> None:
        if self._in_log():
            return
        keysym = event.keysym or ""
        ctrl = (event.state & 0x4) != 0
        if ctrl and not self._in_any_text_field():
            lk = keysym.lower()
            if lk == "o":
                self._load_config()
                return
            if lk == "s":
                self._save_full_config_yaml()
                return
            if lk == "q":
                self.destroy()
                return
        if keysym in ("plus", "equal", "KP_Add"):
            self._zoom_at_center(1.1)
        elif keysym in ("minus", "underscore", "KP_Subtract"):
            self._zoom_at_center(1 / 1.1)
        elif keysym == "0":
            self._reset_view()
        elif keysym == "Left":
            self._pan_keys(-1, 0)
        elif keysym == "Right":
            self._pan_keys(1, 0)
        elif keysym == "Up":
            self._pan_keys(0, -1)
        elif keysym == "Down":
            self._pan_keys(0, 1)

    def _display_viewport(self) -> tuple | None:
        """
        Same geometry as _draw_now: crop in resource space + where it is drawn on canvas.
        Returns:
          ox, oy, ds, ix_start, iy_start, ix_end, iy_end,
          draw_x, draw_y, disp_w, disp_h, crop_w, crop_h, iw, ih
        or None if nothing to show.
        """
        if self._res is None:
            return None
        cw = max(self.canvas.winfo_width(), 400)
        ch = max(self.canvas.winfo_height(), 400)
        ih, iw = self._res.shape[:2]
        sc_fit = min(cw / iw, ch / ih, 1.0)
        if sc_fit > 1.0:
            sc_fit = 1.0
        ds = sc_fit * self._zoom
        nw = max(1, int(iw * ds))
        nh = max(1, int(ih * ds))
        ox = (cw - nw) // 2 + self._pan_x
        oy = (ch - nh) // 2 + self._pan_y
        ix_start = max(0, int(-ox / ds))
        iy_start = max(0, int(-oy / ds))
        ix_end = min(iw, int((cw - ox) / ds) + 1)
        iy_end = min(ih, int((ch - oy) / ds) + 1)
        crop_w = ix_end - ix_start
        crop_h = iy_end - iy_start
        if crop_w <= 0 or crop_h <= 0 or ix_start >= iw or iy_start >= ih:
            return None
        disp_w = max(1, int(crop_w * ds))
        disp_h = max(1, int(crop_h * ds))
        draw_x = ox + int(ix_start * ds)
        draw_y = oy + int(iy_start * ds)
        return (
            ox,
            oy,
            ds,
            ix_start,
            iy_start,
            ix_end,
            iy_end,
            draw_x,
            draw_y,
            disp_w,
            disp_h,
            crop_w,
            crop_h,
            iw,
            ih,
        )

    def _ensure_display_xform(self) -> bool:
        """Set legacy _disp_* fields for callers that still use scale+full-image origin."""
        vp = self._display_viewport()
        if vp is None:
            return False
        ox, oy, ds = vp[0], vp[1], vp[2]
        self._disp_scale = ds
        self._disp_ox = ox
        self._disp_oy = oy
        return True

    def _canvas_to_img(self, cx: int, cy: int) -> tuple[int, int] | None:
        """Canvas pixel -> full resource-map (ix, iy). Matches cropped/resized photo in _draw_now."""
        if self._res is None:
            return None
        vp = getattr(self, "_map_vp", None)
        if vp is None:
            vp = self._display_viewport()
        if vp is None:
            return None
        (
            _ox,
            _oy,
            ds,
            ix_start,
            iy_start,
            _ixe,
            _iye,
            draw_x,
            draw_y,
            disp_w,
            disp_h,
            crop_w,
            crop_h,
            iw,
            ih,
        ) = vp
        self._disp_scale = ds
        self._disp_ox = _ox
        self._disp_oy = _oy
        rx = cx - draw_x
        ry = cy - draw_y
        if rx < 0 or ry < 0 or rx >= disp_w or ry >= disp_h:
            return None
        ix_rel = int(rx * crop_w / disp_w)
        iy_rel = int(ry * crop_h / disp_h)
        ix = ix_start + min(max(ix_rel, 0), crop_w - 1)
        iy = iy_start + min(max(iy_rel, 0), crop_h - 1)
        if 0 <= ix < iw and 0 <= iy < ih:
            return ix, iy
        return None

    def _resource_ixiy_to_canvas_xy(
        self, ix: int, iy: int, vp: tuple | None = None
    ) -> tuple[float, float] | None:
        """Full-map (ix, iy) -> canvas coords for the displayed (cropped) photo."""
        if vp is None:
            vp = getattr(self, "_map_vp", None) or self._display_viewport()
        if vp is None:
            return None
        (
            _ox,
            _oy,
            ds,
            ix_start,
            iy_start,
            _ixe,
            _iye,
            draw_x,
            draw_y,
            disp_w,
            disp_h,
            crop_w,
            crop_h,
            iw,
            ih,
        ) = vp
        if not (0 <= ix < iw and 0 <= iy < ih):
            return None
        cx = draw_x + (ix - ix_start) * disp_w / max(crop_w, 1)
        cy = draw_y + (iy - iy_start) * disp_h / max(crop_h, 1)
        return cx, cy

    def _snap_lasso_vertex_ixiy(self, cx: int, cy: int) -> tuple[int, int] | None:
        if not self.lasso_snap_corners.get():
            return None
        vp = getattr(self, "_map_vp", None) or self._display_viewport()
        if vp is None:
            return None
        r = float(max(1, int(self.lasso_snap_radius_canvas.get())))
        r2 = r * r
        best: tuple[int, int] | None = None
        best_d2 = r2 + 1.0
        refs: list[tuple[int, int]] = []
        for poly in self._lasso_closed_polys:
            refs.extend(poly)
        refs.extend(self._lasso_pts)
        for z in self._zones:
            for xy in z.get("points_xy") or []:
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    refs.append((int(xy[0]), int(xy[1])))
        seen: set[tuple[int, int]] = set()
        for ix, iy in refs:
            key = (ix, iy)
            if key in seen:
                continue
            seen.add(key)
            pr = self._resource_ixiy_to_canvas_xy(ix, iy, vp)
            if pr is None:
                continue
            pcx, pcy = pr
            dd = (cx - pcx) ** 2 + (cy - pcy) ** 2
            if dd <= r2 and dd < best_d2:
                best_d2 = dd
                best = key
        return best

    def _lasso_commodities_for_stats(self) -> list[str]:
        lb = getattr(self, "lasso_commodity_listbox", None)
        out: list[str] = []
        if lb is not None:
            for i in lb.curselection():
                s = (lb.get(i) or "").strip()
                if s and s not in out:
                    out.append(s)
        if not out:
            c = (self.commodity_var.get() or "").strip()
            if c:
                out.append(c)
        return out

    def _lasso_vertices_for_zone_append(self) -> list[tuple[int, int]] | None:
        if len(self._lasso_pts) >= 3:
            return list(self._lasso_pts)
        if self._lasso_closed_polys:
            last = self._lasso_closed_polys[-1]
            if len(last) >= 3:
                return list(last)
        return None

    def _log(self, msg: str) -> None:
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def _refresh_commodities(self) -> None:
        if not self._cfg:
            if getattr(self, "lasso_commodity_listbox", None):
                self.lasso_commodity_listbox.delete(0, tk.END)
            return
        keys = list((self._cfg.get("resource_legend") or {}).keys())
        self.commodity_combo["values"] = keys
        if keys:
            if self.commodity_var.get() not in keys:
                self.commodity_var.set(keys[0])
        else:
            self.commodity_var.set("")
        self._refresh_lasso_commodity_listbox()

    def _refresh_lasso_commodity_listbox(self) -> None:
        lb = getattr(self, "lasso_commodity_listbox", None)
        if lb is None or not self._cfg:
            return
        prev_sel = {lb.get(i) for i in lb.curselection()}
        lb.delete(0, tk.END)
        keys = list((self._cfg.get("resource_legend") or {}).keys())
        for k in keys:
            lb.insert(tk.END, k)
        to_sel = {k for k in prev_sel if k in keys}
        if not to_sel:
            cur = (self.commodity_var.get() or "").strip()
            if cur in keys:
                to_sel.add(cur)
            for pref in ("oil", "natural_gas"):
                if pref in keys:
                    to_sel.add(pref)
        for i, k in enumerate(keys):
            if k in to_sel:
                lb.selection_set(i)

    def _load_config(self, path: Path | str | None = None) -> None:
        if path is None:
            p = filedialog.askopenfilename(
                title="config.yaml",
                initialdir=str(_overlay_root()),
                filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
            )
            if not p:
                return
            path = Path(p)
        else:
            path = Path(path)

        if not path.is_file():
            self._log(f"Config file not found: {path}")
            return

        try:
            self._cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            self._config_path = path
            self._anchor_session = {}
            self._refresh_commodities()
            self._sync_commodity_max_de_from_cfg()
            self._refresh_anchor_listbox()
            self._refresh_nation_combobox()
            self._update_nation_ui_state()
            self._log(f"Loaded config {path}")
            self._reload_zones_from_cfg()
            
            # Auto-load maps if paths are in config
            if self._cfg:
                pol_path = self._cfg.get("map_political_path")
                res_path = self._cfg.get("map_resource_path")
                if pol_path:
                    p = _overlay_root() / pol_path
                    if p.is_file():
                        self._load_political(p)
                if res_path:
                    p = _overlay_root() / res_path
                    if p.is_file():
                        self._load_resource(p)
            
            if self._pol is not None:
                ccols = list(self._cfg.get("ocean_colors") or [])
                self._ocean_pol = ocean_mask_exact(self._pol, ccols)
            self._invalidate_all_caches()
            self._update_eez_km_hint()
            self._schedule_redraw_heavy()
        except Exception as e:
            messagebox.showerror("Load Config", f"Failed to load YAML: {e}")

    def _load_political(self, path: Path | str | None = None) -> None:
        if path is None:
            p = filedialog.askopenfilename(
                title="Political PNG",
                initialdir=str(_maps_dir()),
                filetypes=[("PNG", "*.png"), ("All", "*.*")],
            )
            if not p:
                return
            path = Path(p)
        else:
            path = Path(path)

        if not path.is_file():
            return

        try:
            self._pol = ar.load_rgb(path)
            self._political_path = path
            self._log(f"Loaded political map: {path.name} ({self._pol.shape[1]}x{self._pol.shape[0]})")
            
            # Save path to config (relative to root if possible)
            if self._cfg is not None:
                try:
                    rel = path.relative_to(_overlay_root())
                    self._cfg["map_political_path"] = str(rel)
                except ValueError:
                    self._cfg["map_political_path"] = str(path)
            
            if self._res is not None and self._pol.shape != self._res.shape:
                messagebox.showerror("Shape", "Political and resource sizes must match.")
                self._pol = None
                self._political_path = None
                return
            if self._cfg:
                self._ocean_pol = ocean_mask_exact(self._pol, list(self._cfg.get("ocean_colors") or []))
            self._invalidate_eez_overlay()
            self.show_overlay.set(True)
            self._update_eez_km_hint()
            self._schedule_redraw_fast()
        except Exception as e:
            messagebox.showerror("Load Political", f"Failed to load image: {e}")

    def _load_resource(self, path: Path | str | None = None) -> None:
        if path is None:
            p = filedialog.askopenfilename(
                title="Resource PNG",
                initialdir=str(_maps_dir()),
                filetypes=[("PNG", "*.png"), ("All", "*.*")],
            )
            if not p:
                return
            path = Path(p)
        else:
            path = Path(path)

        if not path.is_file():
            return

        try:
            self._res = ar.load_rgb(path)
            self._resource_path = path
            self._log(f"Loaded resource map: {path.name} ({self._res.shape[1]}x{self._res.shape[0]})")
            
            # Save path to config (relative to root if possible)
            if self._cfg is not None:
                try:
                    rel = path.relative_to(_overlay_root())
                    self._cfg["map_resource_path"] = str(rel)
                except ValueError:
                    self._cfg["map_resource_path"] = str(path)
                    
            if self._pol is not None and self._pol.shape != self._res.shape:
                messagebox.showerror("Shape", "Political and resource sizes must match.")
                self._res = None
                self._resource_path = None
                return
            self._invalidate_all_caches()
            self._schedule_redraw_heavy()
        except Exception as e:
            messagebox.showerror("Load Resource", f"Failed to load image: {e}")

    def _get_de_override(self) -> float | None:
        s = self.de_override.get().strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _bump_cfg_revision(self) -> None:
        self._cfg_revision += 1

    def _sync_commodity_max_de_from_cfg(self) -> None:
        if not self._cfg:
            self.commodity_max_de.set("")
            return
        com = self.commodity_var.get()
        leg = self._cfg.get("resource_legend") or {}
        if not com or com not in leg:
            self.commodity_max_de.set("")
            return
        default_de = float(self._cfg.get("resource_max_delta_e", 22))
        try:
            _, de = ar.parse_commodity_spec(leg[com], default_de)
        except (KeyError, ValueError, TypeError):
            self.commodity_max_de.set("")
            return
        d = float(de)
        self.commodity_max_de.set(str(int(d)) if d == int(d) else str(d))

    def _get_session_anchors(self, com: str) -> list[list[int]]:
        if com not in self._anchor_session:
            if self._cfg and com in (self._cfg.get("resource_legend") or {}):
                default_de = float(self._cfg.get("resource_max_delta_e", 22))
                anchors, _ = ar.parse_commodity_spec(
                    self._cfg["resource_legend"][com], default_de
                )
                self._anchor_session[com] = [list(map(int, a)) for a in anchors]
            else:
                self._anchor_session[com] = []
        return self._anchor_session[com]

    def _refresh_anchor_listbox(self) -> None:
        self.anchor_listbox.delete(0, tk.END)
        if not self._cfg:
            return
        com = self.commodity_var.get()
        if not com:
            return
        for trip in self._get_session_anchors(com):
            self.anchor_listbox.insert(tk.END, str(trip))

    @staticmethod
    def _zone_to_yaml_dict(z: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": z["id"], "points_xy": z["points_xy"]}
        if z.get("commodity"):
            out["commodity"] = z["commodity"]
        if z.get("nation"):
            out["nation"] = z["nation"]
        for k in (
            "eez_offshore_px",
            "beyond_halo_ocean_px",
            "on_land_px",
            "halo_px_used",
            "attribute_mode",
            "notes",
        ):
            if k in z:
                out[k] = z[k]
        return out

    def _sync_zones_into_cfg(self) -> None:
        if self._cfg is None:
            return
        if not self._zones:
            self._cfg.pop("deposit_sampler_zones", None)
            return
        self._cfg["deposit_sampler_zones"] = [
            self._zone_to_yaml_dict(z) for z in self._zones
        ]

    def _reload_zones_from_cfg(self) -> None:
        self._zones = []
        self._selected_zone_ix = None
        if self._cfg:
            raw_list = self._cfg.get("deposit_sampler_zones")
            if isinstance(raw_list, list):
                for raw in raw_list:
                    z = _normalize_deposit_zone(raw)
                    if z:
                        self._zones.append(z)
        self._refresh_zone_listbox()
        try:
            self.zone_listbox.selection_clear(0, tk.END)
        except tk.TclError:
            pass
        self._schedule_redraw_fast()

    def _refresh_zone_listbox(self) -> None:
        self.zone_listbox.delete(0, tk.END)
        for z in self._zones:
            com = z.get("commodity") or "—"
            nat = z.get("nation") or "—"
            self.zone_listbox.insert(tk.END, f"{z['id']} — {com} / {nat}")

    def _on_zone_select(self, _event: tk.Event | None = None) -> None:
        sel = self.zone_listbox.curselection()
        if not sel:
            self._selected_zone_ix = None
        else:
            self._selected_zone_ix = int(sel[0])
        self._schedule_redraw_fast()

    def _zones_load_file(self) -> None:
        p = filedialog.askopenfilename(
            title="Zones YAML (deposit_sampler_zones)",
            initialdir=str(_overlay_root()),
            filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
        )
        if not p:
            return
        try:
            data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as e:
            messagebox.showerror("YAML", str(e))
            return
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = data.get("deposit_sampler_zones") or []
        else:
            messagebox.showerror("Zones", "File must be a list or a dict with deposit_sampler_zones.")
            return
        incoming: list[dict[str, Any]] = []
        for raw in raw_list:
            z = _normalize_deposit_zone(raw)
            if z:
                incoming.append(z)
        if not incoming:
            messagebox.showinfo(
                "Zones", "No valid zones (need id + ≥3 points_xy per entry)."
            )
            return
        merge = messagebox.askyesno(
            "Load zones",
            "Merge with current zones by id — Yes\nReplace all — No",
        )
        if merge:
            index_by_id = {z["id"]: i for i, z in enumerate(self._zones)}
            for z in incoming:
                ix = index_by_id.get(z["id"])
                if ix is not None:
                    self._zones[ix] = z
                else:
                    self._zones.append(z)
                    index_by_id[z["id"]] = len(self._zones) - 1
        else:
            self._zones = incoming
        self._sync_zones_into_cfg()
        self._selected_zone_ix = None
        self._refresh_zone_listbox()
        try:
            self.zone_listbox.selection_clear(0, tk.END)
        except tk.TclError:
            pass
        self._schedule_redraw_fast()
        self._log(
            f"Zones from {p}: {len(incoming)} entr(y/ies) loaded; "
            f"merge={'yes' if merge else 'replace'}."
        )

    def _zones_save_file(self) -> None:
        if not self._zones:
            messagebox.showinfo("Zones", "No saved zones — append a lasso first.")
            return
        p = filedialog.asksaveasfilename(
            title="Save deposit_sampler_zones",
            initialdir=str(_overlay_root()),
            initialfile="deposit_sampler_zones.yaml",
            defaultextension=".yaml",
            filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
        )
        if not p:
            return
        payload = {
            "deposit_sampler_zones": [self._zone_to_yaml_dict(z) for z in self._zones]
        }
        header = (
            "# Merge `deposit_sampler_zones` into config.yaml or reload here.\n"
            "# Points are resource-map pixel x,y (same frame as lasso clicks).\n\n"
        )
        body = yaml.dump(
            payload,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        Path(p).write_text(header + body, encoding="utf-8")
        self._log(f"Saved {len(self._zones)} zone(s) → {p}")

    def _zones_append_lasso(self) -> None:
        verts = self._lasso_vertices_for_zone_append()
        if not verts or len(verts) < 3:
            messagebox.showinfo(
                "Zones",
                "Draw a lasso (green, ≥3 clicks) or Close one (orange), then append — "
                "or use the last finished orange outline.",
            )
            return
        zid = simpledialog.askstring(
            "Zone id",
            "Unique id (e.g. copper_shelf_1):",
            parent=self,
        )
        if not zid or not str(zid).strip():
            return
        zid = str(zid).strip()
        if any(z["id"] == zid for z in self._zones):
            messagebox.showerror("Zones", f"id {zid!r} already exists — choose another.")
            return
        nation = simpledialog.askstring(
            "Nation (optional)",
            "Optional label (not used by batch pipeline unless you add support):",
            parent=self,
        )
        nation = (nation or "").strip()
        coms = self._lasso_commodities_for_stats()
        com = ",".join(coms) if coms else (self.commodity_var.get() or "").strip()
        self._zones.append(
            {
                "id": zid,
                "points_xy": [[int(x), int(y)] for x, y in verts],
                "commodity": com,
                "nation": nation,
            }
        )
        self._sync_zones_into_cfg()
        self._lasso_pts.clear()
        self._refresh_zone_listbox()
        self._schedule_redraw_fast()
        self._log(f"Appended zone {zid!r} ({com or 'no commodity'})")

    def _lasso_as_nation_split(self) -> None:
        verts = self._lasso_vertices_for_zone_append()
        if not verts or len(verts) < 3:
            messagebox.showinfo(
                "Nation Split",
                "Draw a lasso (green, ≥3 clicks) or Close one (orange), then use this button.",
            )
            return
            
        if self._cfg is None or "nations" not in self._cfg:
            messagebox.showinfo("Nation Split", "Load a project with nations first.")
            return
            
        nations = sorted(self._cfg["nations"].keys())
        
        win = tk.Toplevel(self)
        win.title("Select Nation for Lasso Area")
        win.geometry("350x150")
        win.transient(self)
        win.grab_set()
        
        ttk.Label(win, text="Which nation owns the area inside this lasso?").pack(pady=10)
        
        nation_var = tk.StringVar()
        combo = ttk.Combobox(win, textvariable=nation_var, values=nations, width=30)
        combo.pack(pady=5)
        
        def _on_keyrelease(event):
            val = nation_var.get().lower()
            if val == "":
                combo["values"] = nations
            else:
                filtered = [n for n in nations if val in n.lower()]
                combo["values"] = filtered
                
        combo.bind("<KeyRelease>", _on_keyrelease)
        
        if nations:
            combo.current(0)
            
        def on_ok():
            selected = nation_var.get()
            if not selected:
                return
            
            selected_cols = self._cfg["nations"][selected]
            if not selected_cols:
                return
            if isinstance(selected_cols[0], (int, float)):
                selected_rgbs = [selected_cols]
            else:
                selected_rgbs = selected_cols
                
            shared_rgbs = []
            for rgb in selected_rgbs:
                sharing = []
                for n, c in self._cfg["nations"].items():
                    if not c: continue
                    if isinstance(c[0], (int, float)):
                        rgbs = [c]
                    else:
                        rgbs = c
                    if rgb in rgbs:
                        sharing.append(n)
                if len(sharing) > 1:
                    shared_rgbs.append((rgb, sharing))
                    
            if not shared_rgbs:
                messagebox.showinfo("Nation Split", f"{selected} does not share any of its colours with any other nation. No split needed.")
                win.destroy()
                return
                
            if "duplicate_fill_splits" not in self._cfg:
                self._cfg["duplicate_fill_splits"] = []
                
            for rgb, sharing in shared_rgbs:
                existing = None
                for s in self._cfg["duplicate_fill_splits"]:
                    if set(s.get("nations", [])) == set(sharing):
                        existing = s
                        break
                        
                if existing is None:
                    existing = {"nations": sharing}
                    self._cfg["duplicate_fill_splits"].append(existing)
                    
                if "polygon_xy" not in existing:
                    existing["polygon_xy"] = {}
                    
                existing["polygon_xy"][selected] = [[int(x), int(y)] for x, y in verts]
            
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            self._lasso_pts.clear()
            self._schedule_redraw_heavy()
            self._log(f"Added polygon split for {selected} (shares colour with {', '.join(sharing)})")
            win.destroy()
            
        ttk.Button(win, text="OK", command=on_ok).pack(pady=10)
        self.wait_window(win)

    def _zones_append_all_closed_lassos(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Zones", "Load or create a config first.")
            return
        if not self._lasso_closed_polys:
            messagebox.showinfo(
                "Zones",
                "No orange (finished) lassos — use Close lasso on each polygon first.",
            )
            return
        prefix = simpledialog.askstring(
            "Append all orange lassos",
            "Base id (new zones become PREFIX_1, PREFIX_2, …):",
            parent=self,
        )
        if not prefix or not str(prefix).strip():
            return
        prefix = str(prefix).strip().replace(" ", "_")
        nation = simpledialog.askstring(
            "Nation (optional)",
            "Optional nation label applied to all appended zones (blank = none):",
            parent=self,
        )
        nation = (nation or "").strip()
        coms = self._lasso_commodities_for_stats()
        com = ",".join(coms) if coms else (self.commodity_var.get() or "").strip()
        existing: set[str] = {z["id"] for z in self._zones}
        added = 0
        skipped = 0
        for i, poly in enumerate(self._lasso_closed_polys):
            if len(poly) < 3:
                skipped += 1
                continue
            zid = f"{prefix}_{i + 1}"
            bump = 1
            while zid in existing:
                zid = f"{prefix}_{i + 1}_{bump}"
                bump += 1
            existing.add(zid)
            self._zones.append(
                {
                    "id": zid,
                    "points_xy": [[int(x), int(y)] for x, y in poly],
                    "commodity": com,
                    "nation": nation,
                }
            )
            added += 1
        self._sync_zones_into_cfg()
        self._refresh_zone_listbox()
        self._schedule_redraw_fast()
        self._log(
            f"Appended {added} zone(s) from {len(self._lasso_closed_polys)} orange outline(s); "
            f"skipped {skipped} (need ≥3 corners)."
        )
        messagebox.showinfo(
            "Zones",
            f"Added {added} zone(s). They are in memory under deposit_sampler_zones — "
            "use Save zones YAML… (or merge into config.yaml) to persist to disk.",
        )

    def _zones_tag_eez_attribution(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Zones", "Load config.yaml first.")
            return
        if self._pol is None:
            messagebox.showinfo("Zones", "Load political PNG first.")
            return
        if not self._zones:
            messagebox.showinfo("Zones", "No saved zones — append lasso outlines first.")
            return
        try:
            halo = int(self.eez_halo_px.get())
        except tk.TclError:
            return
        if halo <= 0:
            return
        use_title = self.eez_use_title_attach.get()
        pol = self._pol
        cfg = self._cfg
        zcopy = deepcopy(self._zones)

        def work() -> None:
            try:
                ar.attribute_deposit_sampler_zones(
                    pol,
                    cfg,
                    float(halo),
                    zcopy,
                    use_effective_land_for_halo=use_title,
                )

                def ok() -> None:
                    self._zones = zcopy
                    self._sync_zones_into_cfg()
                    self._refresh_zone_listbox()
                    self._log(
                        "Zones tagged: eez_offshore_px, beyond_halo_ocean_px, on_land_px — "
                        "Save zones YAML or merge config; analyze step also writes "
                        "deposit_zones_attribution.json when deposit_sampler_zones is in config."
                    )

                self.after(0, ok)
            except (KeyError, TypeError, ValueError) as e:

                def bad() -> None:
                    messagebox.showerror("EEZ zone tags", str(e))

                self.after(0, bad)

        self._log(
            "Tagging zones with EEZ / halo (background) — uses sidebar halo px and "
            "Match CSV title mode."
        )
        threading.Thread(target=work, daemon=True).start()

    def _zones_delete_selected(self) -> None:
        if self._selected_zone_ix is None:
            messagebox.showinfo("Zones", "Select a zone in the list first.")
            return
        if not (0 <= self._selected_zone_ix < len(self._zones)):
            return
        zid = self._zones[self._selected_zone_ix]["id"]
        del self._zones[self._selected_zone_ix]
        self._selected_zone_ix = None
        self._sync_zones_into_cfg()
        try:
            self.zone_listbox.selection_clear(0, tk.END)
        except tk.TclError:
            pass
        self._refresh_zone_listbox()
        self._schedule_redraw_fast()
        self._log(f"Deleted zone {zid!r}")

    def _zones_log_stats_selected(self) -> None:
        if self._selected_zone_ix is None or not (
            0 <= self._selected_zone_ix < len(self._zones)
        ):
            messagebox.showinfo("Zones", "Select a zone in the list first.")
            return
        z = self._zones[self._selected_zone_ix]
        pts = [(int(p[0]), int(p[1])) for p in z["points_xy"]]
        zcom = (z.get("commodity") or "").strip()
        commodities: list[str] = []
        if zcom:
            for part in zcom.split(","):
                p = part.strip()
                if p and p not in commodities:
                    commodities.append(p)
        if not commodities:
            commodities = self._lasso_commodities_for_stats()
        if not commodities:
            messagebox.showinfo(
                "Zones",
                "Zone has no commodity — select some in the lasso list or in Deposit above the map.",
            )
            return
        self._lasso_log_stats_for_polygon(pts, commodities, zone_label=z["id"])

    def _lasso_log_stats_for_polygon(
        self,
        vertices: list[tuple[int, int]],
        commodities: list[str],
        *,
        zone_label: str | None = None,
    ) -> None:
        if self._res is None:
            messagebox.showinfo("Lasso", "Load a resource PNG first.")
            return
        if self._cfg is None:
            messagebox.showinfo("Lasso", "Load config.yaml (or minimal config) first.")
            return
        if len(vertices) < 3:
            messagebox.showinfo("Lasso", "Need at least 3 corners.")
            return
        uniq: list[str] = []
        for c in commodities:
            s = (c or "").strip()
            if s and s not in uniq:
                uniq.append(s)
        commodities = uniq
        if not commodities:
            messagebox.showinfo(
                "Lasso",
                "Select at least one commodity (lasso list Ctrl+click or Deposit field).",
            )
            return
        h, w = self._res.shape[:2]
        poly = Image.new("L", (w, h), 0)
        flat = [c for p in vertices for c in p]
        ImageDraw.Draw(poly).polygon(flat, outline=1, fill=1)
        pm = np.asarray(poly) > 0
        rgb = self._res
        n_poly = int(np.count_nonzero(pm))
        applied: list[str] = []

        if self.lasso_omit_white.get():
            white = np.all(rgb >= _LASSO_WHITE_RGB_MIN, axis=-1)
            pm &= ~white
            applied.append(f"omit_white≥{_LASSO_WHITE_RGB_MIN}")

        if self.lasso_omit_sea_political.get():
            if self._ocean_pol is not None:
                pm &= ~self._ocean_pol
                applied.append("omit_sea_political")
            else:
                self._log(
                    "Lasso: omit sea checked but no ocean mask — load political PNG + "
                    "ocean_colors in config."
                )

        px = rgb[pm]
        title = zone_label if zone_label else "Lasso"
        com_label = ", ".join(commodities)
        if len(px) == 0:
            msg = (
                f"{title}: no pixels left after polygon + filters "
                f"({n_poly} px in polygon; filters: {', '.join(applied) or 'none'})."
            )
            self._log(msg)
            messagebox.showinfo("Lasso", msg)
            return
        mn = px.min(axis=0)
        mx = px.max(axis=0)
        mean_all = px.mean(axis=0).astype(int)
        merge = int(self.lasso_merge_shift.get())
        top_c = int(self.lasso_top_colours.get())
        min_blob = max(1, int(self.lasso_min_blob_px.get()))

        buckets = _lasso_color_buckets(rgb, pm, merge, top_c)
        blobs = _lasso_spatial_blobs(
            rgb, pm, merge, top_c, min_blob, max_blobs_logged=24
        )

        n_px = len(px)
        lines: list[str] = [
            f"--- {title} ({n_px} px of {n_poly} in polygon) commodities={com_label} ---",
            f"filters: {', '.join(applied) or 'none'}",
            f"Global RGB min {mn.tolist()} max {mx.tolist()} mean {mean_all.tolist()}",
            "",
            f"Colour buckets (RGB >> {merge}, top {top_c} histogram peaks):",
        ]
        for i, (cnt, mrgb) in enumerate(buckets, start=1):
            pct = 100.0 * cnt / max(n_px, 1)
            de_parts: list[str] = []
            for c in commodities:
                dmin, _t = self._lasso_de_info(mrgb, c)
                de_parts.append(f"{c}={dmin:.1f}")
            _, trip = self._lasso_de_info(mrgb, commodities[0])
            lines.append(
                f"  {i:2}. {pct:5.1f}%  {cnt:6d} px  "
                f"{'  '.join(de_parts)}  RGB {trip}"
            )

        lines.append("")
        lines.append(
            f"Spatial blobs (8-neighbour, min {min_blob} px, largest listed first):"
        )
        if not blobs:
            lines.append(
                "  (none — lower min blob px, lower merge, or widen polygon)"
            )
        for i, (area, mrgb) in enumerate(blobs, start=1):
            de_parts_b: list[str] = []
            for c in commodities:
                dmin, _t = self._lasso_de_info(mrgb, c)
                de_parts_b.append(f"{c}={dmin:.1f}")
            _, trip = self._lasso_de_info(mrgb, commodities[0])
            lines.append(
                f"  {i:2}. {area:6d} px  {'  '.join(de_parts_b)}  RGB {trip}"
            )

        if blobs:
            pick = blobs[0][1]
        elif buckets:
            pick = buckets[0][1]
        else:
            pick = np.clip(mean_all, 0, 255).astype(np.uint8)
        _, rgb_pick = self._lasso_de_info(pick, commodities[0])
        lines.append("")
        lines.append(
            "Primary colour (largest blob, else top bucket, else global mean) vs commodities:"
        )
        de_summary: list[str] = []
        for c in commodities:
            d_c, _rgb_c = self._lasso_de_info(pick, c)
            de_summary.append(f"{c} ΔE={d_c:.2f}")
        lines.append("  " + "  |  ".join(de_summary))
        lines.append("Suggested YAML anchor lines (same RGB for each key):")
        for c in commodities:
            d_c, _rgb_c = self._lasso_de_info(pick, c)
            lines.append(
                f"  {c}: ΔE {d_c:.2f}  →  "
                f"- [{rgb_pick[0]}, {rgb_pick[1]}, {rgb_pick[2]}]  # vs {title}\n"
            )

        self._log("\n".join(lines))
        self._log("(Full stats above — scroll the log panel if needed.)")

    def _anchor_remove_selected(self) -> None:
        com = self.commodity_var.get()
        if not com:
            return
        sel = sorted(self.anchor_listbox.curselection(), reverse=True)
        sess = self._get_session_anchors(com)
        for idx in sel:
            if 0 <= idx < len(sess):
                del sess[idx]
        self._refresh_anchor_listbox()

    def _flush_session_to_cfg_commodity(self, com: str) -> bool:
        if not com or self._cfg is None:
            return False
        anchors = self._get_session_anchors(com)
        if not anchors:
            return False
        s = self.commodity_max_de.get().strip()
        default_g = float(self._cfg.get("resource_max_delta_e", 22))
        # Only treat the sidebar ΔE box as authoritative for the *currently selected*
        # commodity. Otherwise switching oil→gas while the box still shows "24"
        # would incorrectly overwrite gas with oil's threshold.
        cur = (self.commodity_var.get() or "").strip()
        if s and cur == com:
            de = float(s)
        else:
            raw = self._cfg.get("resource_legend", {}).get(com)
            if isinstance(raw, dict) and raw.get("max_delta_e") is not None:
                de = float(raw["max_delta_e"])
            elif raw is not None:
                _, de = ar.parse_commodity_spec(raw, default_g)
            else:
                de = default_g
        self._cfg.setdefault("resource_legend", {})[com] = {
            "anchors": [list(map(int, a)) for a in anchors],
            "max_delta_e": float(de),
        }
        return True

    def _apply_anchors_to_cfg(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Apply", "Load or create a config first.")
            return
        com = self.commodity_var.get()
        if not com:
            messagebox.showinfo("Apply", "Select or add a commodity.")
            return
        try:
            ok = self._flush_session_to_cfg_commodity(com)
        except (ValueError, TypeError) as e:
            messagebox.showerror("Apply", f"Bad max ΔE or anchors: {e}")
            return
        if not ok:
            messagebox.showinfo(
                "Apply", "Add at least one anchor (Eyedropper → list) first."
            )
            return
        self._bump_cfg_revision()
        self._invalidate_all_caches()
        entry = self._cfg["resource_legend"][com]
        de = entry["max_delta_e"]
        n = len(entry["anchors"])
        self._log(f"Applied {n} anchor(s) → memory for {com!r} (max_ΔE={de}).")
        self._schedule_redraw_heavy()

    def _flush_all_pending_session_anchors_to_cfg(self) -> list[str]:
        """Write session anchor lists for every commodity that has a session entry."""
        updated: list[str] = []
        if self._cfg is None:
            return updated
        leg = self._cfg.get("resource_legend") or {}
        for com in list(self._anchor_session.keys()):
            if com not in leg:
                continue
            try:
                if self._flush_session_to_cfg_commodity(com):
                    updated.append(com)
            except (ValueError, TypeError) as e:
                self._log(f"Skip flush for {com!r}: {e}")
        return updated

    def _persist_cfg_for_disk_write(self) -> None:
        """Sync zones and merge any anchor-list edits before writing config.yaml."""
        if self._cfg is None:
            return
        flushed = self._flush_all_pending_session_anchors_to_cfg()
        if flushed:
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            self._log(
                "Auto-applied pending anchors for "
                + ", ".join(flushed)
                + " (before saving config to disk)."
            )
        self._sync_zones_into_cfg()

    def _merge_nations_fragment(self) -> None:
        """Merge nations (+ optional keys) from a YAML fragment into in-memory config."""
        if self._cfg is None:
            messagebox.showinfo(
                "Import nations fragment",
                "Load or create a project (config.yaml) first.",
            )
            return
        p = filedialog.askopenfilename(
            title="Import nations fragment (YAML)",
            initialdir=str(_overlay_root()),
            filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
        )
        if not p:
            return
        path = Path(p)
        try:
            raw_any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Import nations fragment", f"Failed to read YAML: {e}")
            return
        if not isinstance(raw_any, dict):
            messagebox.showerror("Import nations fragment", "Root must be a mapping.")
            return
        raw: dict[str, Any] = raw_any
        nations = raw.get("nations")
        if not isinstance(nations, dict) or not nations:
            messagebox.showerror(
                "Import nations fragment",
                "File needs a non-empty `nations:` mapping.",
            )
            return

        normalized: dict[str, Any] = {}
        try:
            for name, col_or_cols in nations.items():
                key = str(name)
                if not col_or_cols:
                    continue
                if isinstance(col_or_cols[0], (int, float)):
                    if len(col_or_cols) != 3:
                        raise ValueError(
                            f"nations[{key!r}] must be [R, G, B] or a list of [R, G, B]"
                        )
                    normalized[key] = [
                        int(col_or_cols[0]),
                        int(col_or_cols[1]),
                        int(col_or_cols[2]),
                    ]
                else:
                    normalized[key] = []
                    for col in col_or_cols:
                        if not isinstance(col, (list, tuple)) or len(col) != 3:
                            raise ValueError(
                                f"nations[{key!r}] must be [R, G, B] or a list of [R, G, B]"
                            )
                        normalized[key].append(
                            [int(col[0]), int(col[1]), int(col[2])]
                        )
        except (ValueError, TypeError, IndexError) as e:
            messagebox.showerror("Import nations fragment", f"Invalid nations data: {e}")
            return

        if not normalized:
            messagebox.showerror(
                "Import nations fragment",
                "After parsing, no nation colour entries remained.",
            )
            return

        self._cfg["nations"] = normalized
        ct = raw.get("color_tolerance")
        if isinstance(ct, int):
            self._cfg["color_tolerance"] = ct
        for opt_key in ("ocean_colors", "ignore_land_colors", "duplicate_fill_splits"):
            if opt_key in raw:
                self._cfg[opt_key] = deepcopy(raw[opt_key])

        self._bump_cfg_revision()
        self._invalidate_all_caches()
        self._refresh_nation_combobox()
        self._update_nation_ui_state()
        if hasattr(self, "_nation_inner_frame") and self._nation_inner_frame.winfo_exists():
            self._populate_nation_list()
        if self._pol is not None:
            self._ocean_pol = ocean_mask_exact(
                self._pol, list(self._cfg.get("ocean_colors") or [])
            )
        self._invalidate_eez_overlay()
        self._log(f"Merged nations fragment from {path} ({len(normalized)} nations).")
        messagebox.showinfo(
            "Merged",
            "Nations (and optional keys present in the fragment) are merged into memory.\n"
            "Use File → Save Everything to persist config.yaml.",
        )
        self._schedule_redraw_heavy()

    def _save_resource_fragment(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Save", "No config in memory.")
            return
        com = self.commodity_var.get()
        try:
            if com and self._get_session_anchors(com):
                if not self._flush_session_to_cfg_commodity(com):
                    pass
                else:
                    self._bump_cfg_revision()
                    self._invalidate_all_caches()
        except (ValueError, TypeError) as e:
            messagebox.showerror("Save", f"Bad commodity max ΔE: {e}")
            return
        leg = self._cfg.get("resource_legend") or {}
        if not leg:
            messagebox.showinfo("Save", "resource_legend is empty — add commodities first.")
            return
        p = filedialog.asksaveasfilename(
            title="Save resource_legend fragment",
            initialdir=str(_overlay_root()),
            initialfile="resource_legend_fragment.yaml",
            defaultextension=".yaml",
            filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
        )
        if not p:
            return
        payload = {"resource_legend": dict(leg)}
        header = (
            "# Fragment — merge `resource_legend` into config.yaml (replace block or per-key).\n"
            "# Does not write nations:, ocean_colors:, halo, etc.\n\n"
        )
        body = yaml.dump(
            payload,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        Path(p).write_text(header + body, encoding="utf-8")
        self._log(f"Saved resource_legend fragment: {p}")
        messagebox.showinfo(
            "Saved",
            "Merge this file into config.yaml, then run analyze_resources / the batch.",
        )
        self._schedule_redraw_heavy()

    def _add_commodity_dialog(self) -> None:
        if self._cfg is None:
            self._cfg = {
                "resource_max_delta_e": 22.0,
                "resource_legend": {},
                "resource_exclude_colors": [],
                "resource_exclude_tolerance": 0,
                "ocean_colors": [],
            }
            self._anchor_session = {}
            self._bump_cfg_revision()
            self._sync_zones_into_cfg()
            self._log("Created minimal in-memory config stub.")
        name = simpledialog.askstring(
            "Add commodity",
            "YAML key (e.g. oil, natural_gas):",
            parent=self,
        )
        if not name:
            return
        key = name.strip().replace(" ", "_")
        if not key:
            return
        leg = self._cfg.setdefault("resource_legend", {})
        if key in leg:
            messagebox.showinfo("Add commodity", f"{key!r} already exists.")
            self.commodity_var.set(key)
            self._on_commodity_change()
            return
        de = float(self._cfg.get("resource_max_delta_e", 22))
        leg[key] = {"anchors": [], "max_delta_e": de}
        self._anchor_session[key] = []
        self._bump_cfg_revision()
        self._invalidate_all_caches()
        self._refresh_commodities()
        self.commodity_var.set(key)
        self._sync_commodity_max_de_from_cfg()
        self._refresh_anchor_listbox()
        self._log(f"Added commodity {key!r} — use Eyedropper to sample RGB.")
        self._schedule_redraw_heavy()

    def _new_minimal_config(self) -> None:
        if not messagebox.askyesno(
            "New minimal config",
            "Replace in-memory config with a stub (resource_* keys only)? "
            "config.yaml on disk is not changed. PNGs stay loaded.",
        ):
            return
        self._cfg = {
            "resource_max_delta_e": 22.0,
            "resource_legend": {},
            "resource_exclude_colors": [],
            "resource_exclude_tolerance": 0,
            "ocean_colors": [],
        }
        self._config_path = None
        self._anchor_session = {}
        self._bump_cfg_revision()
        self._invalidate_all_caches()
        self._refresh_commodities()
        self._sync_commodity_max_de_from_cfg()
        self._refresh_anchor_listbox()
        self._zones = []
        self._selected_zone_ix = None
        if self._cfg is not None:
            self._cfg.pop("deposit_sampler_zones", None)
        self._refresh_zone_listbox()
        self._log("Minimal config in memory — Add commodity, Eyedropper, Apply, Save fragment.")
        self._lasso_closed_polys.clear()
        self._update_eez_km_hint()
        self._schedule_redraw_heavy()

    def _write_cfg_to_path(self, path: Path) -> None:
        if self._cfg is None:
            raise RuntimeError("no config")
        body = yaml.dump(
            self._cfg,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        path.write_text(body, encoding="utf-8")

    def _save_full_config_yaml(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Save", "No config in memory.")
            return

        # Ensure map paths are saved
        if self._political_path:
            try:
                rel = self._political_path.relative_to(_overlay_root())
                self._cfg["map_political_path"] = str(rel)
            except ValueError:
                self._cfg["map_political_path"] = str(self._political_path)
        if self._resource_path:
            try:
                rel = self._resource_path.relative_to(_overlay_root())
                self._cfg["map_resource_path"] = str(rel)
            except ValueError:
                self._cfg["map_resource_path"] = str(self._resource_path)

        initialdir = str(
            self._config_path.parent if self._config_path else _overlay_root()
        )
        initialfile = (
            self._config_path.name if self._config_path else "config.yaml"
        )
        p = filedialog.asksaveasfilename(
            title="Save full config.yaml",
            initialdir=initialdir,
            initialfile=initialfile,
            defaultextension=".yaml",
            filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
        )
        if not p:
            return
        try:
            self._persist_cfg_for_disk_write()
            self._write_cfg_to_path(Path(p))
        except OSError as e:
            messagebox.showerror("Save", str(e))
            return
        self._config_path = Path(p)
        self._log(f"Saved full config: {p}")

    def _open_output_folder(self) -> None:
        out = _overlay_root() / "output"
        out.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(out)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(out)])
            else:
                subprocess.Popen(["xdg-open", str(out)])
        except OSError as e:
            messagebox.showerror("Open folder", str(e))

    def _export_political_eez_map(self) -> None:
        if self._pol is None:
            messagebox.showinfo("Export", "Load a Political Map first.")
            return
            
        rgb_eez, a_eez = self._get_eez_overlay_for_draw()
        if rgb_eez is None or a_eez is None:
            messagebox.showinfo("Export", "Please enable the EEZ overlay and wait for it to compute first.")
            return
            
        p = filedialog.asksaveasfilename(
            title="Export Political + EEZ Map",
            initialdir=str(_overlay_root() / "output"),
            initialfile="Political_Map_with_EEZ.png",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
        )
        if not p:
            return
            
        self._log("Exporting Political + EEZ map...")
        
        # Blend full resolution
        aa = a_eez[..., np.newaxis]
        blended = np.clip(
            self._pol.astype(np.float32) * (1.0 - aa) + rgb_eez * aa,
            0,
            255,
        ).astype(np.uint8)
        
        try:
            Image.fromarray(blended).save(p)
            self._log(f"Exported to {p}")
            messagebox.showinfo("Export", f"Successfully exported map to:\n{p}")
        except Exception as e:
            self._log(f"Export failed: {e}")
            messagebox.showerror("Export Error", str(e))

    def _resolve_political_path_for_batch(self) -> Path | None:
        candidates: list[Path] = []
        if self._political_path is not None:
            candidates.append(self._political_path)
        candidates.append(_maps_dir() / "Political Map.png")
        for c in candidates:
            if c.exists():
                return c
        p = filedialog.askopenfilename(
            title="Political PNG for batch analyze",
            initialdir=str(_maps_dir()),
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
        )
        if not p:
            return None
        self._political_path = Path(p)
        return self._political_path

    def _resolve_resource_path_for_batch(self) -> Path | None:
        candidates: list[Path] = []
        if self._resource_path is not None:
            candidates.append(self._resource_path)
        candidates.append(_maps_dir() / "Resource Map aligned.png")
        for c in candidates:
            if c.exists():
                return c
        p = filedialog.askopenfilename(
            title="Resource PNG for batch analyze",
            initialdir=str(_maps_dir()),
            filetypes=[("PNG", "*.png"), ("All", "*.*")],
        )
        if not p:
            return None
        self._resource_path = Path(p)
        return self._resource_path

    def _run_full_analysis(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Analyze", "Load or create a config first.")
            return
        if self._analyze_busy:
            return
        if self._config_path is None:
            p = filedialog.asksaveasfilename(
                title="Choose config.yaml path (in-memory config will be saved here)",
                initialdir=str(_overlay_root()),
                initialfile="config.yaml",
                defaultextension=".yaml",
                filetypes=[("YAML", "*.yaml;*.yml"), ("All", "*.*")],
            )
            if not p:
                return
            self._config_path = Path(p)
        try:
            assert self._config_path is not None
            self._persist_cfg_for_disk_write()
            self._write_cfg_to_path(self._config_path)
        except OSError as e:
            messagebox.showerror("Save config", str(e))
            return

        pol_path = self._resolve_political_path_for_batch()
        if pol_path is None:
            return
        res_path = self._resolve_resource_path_for_batch()
        if res_path is None:
            return

        try:
            halo = float(self.analysis_halo_km.get())
        except tk.TclError:
            return
        if halo <= 0:
            messagebox.showinfo("Analyze", "Halo km must be positive.")
            return

        out_dir = _overlay_root() / "output"
        out_csv = out_dir / "results.csv"
        out_json = out_dir / "results.json"
        cfg_path = self._config_path

        self._analyze_busy = True
        self._analyze_status.configure(text="Running analyze (background)…")
        self._analyze_prog.pack(anchor=tk.W, pady=(4, 0))
        self._analyze_prog.start(10)
        self.update_idletasks()

        def work() -> None:
            err: BaseException | None = None
            nrows = 0
            try:

                def prog(msg: str) -> None:
                    self.after(0, lambda m=msg: self._log(f"[analyze] {m}"))

                nrows = len(
                    ar.analyze_and_write_outputs(
                        pol_path,
                        res_path,
                        cfg_path,
                        halo,
                        out_csv,
                        out_json=out_json,
                        nations_yaml=None,
                        progress=prog,
                        show_progress=True,
                    )
                )
            except BaseException as e:
                err = e

            def finish() -> None:
                self._analyze_busy = False
                try:
                    self._analyze_prog.stop()
                    self._analyze_prog.pack_forget()
                except tk.TclError:
                    pass
                self._analyze_status.configure(text="")
                if err is not None:
                    em = str(err) if str(err) else repr(err)
                    self._log(f"[analyze] FAILED: {em}")
                    messagebox.showerror("Analyze failed", em)
                    return
                self._log(
                    f"[analyze] Done — {nrows} rows → {out_csv.name}, {out_json.name}"
                )
                if messagebox.askyesno(
                    "Analyze",
                    f"Wrote {nrows} rows under output/. Open that folder?",
                ):
                    self._open_output_folder()

            self.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def _on_map_tool_change(self, event: tk.Event | None = None) -> None:
        val = self.map_tool_display.get()
        if val == "Lasso":
            self.map_tool.set("lasso")
        elif val == "Resource Eyedropper":
            self.map_tool.set("eyedropper")
        elif val == "Political Eyedropper":
            self.map_tool.set("political_eyedropper")

    def _update_nation_ui_state(self, *args) -> None:
        if not hasattr(self, "nation_add_btn"):
            return
        name = self.nation_name_var.get().strip()
        if self._cfg and "nations" in self._cfg and name in self._cfg["nations"]:
            self.nation_add_btn.config(text="Replace Colour")
            if hasattr(self, "nation_add_extra_btn"):
                self.nation_add_extra_btn.pack(side=tk.LEFT, padx=2)
        else:
            self.nation_add_btn.config(text="Add Nation")
            if hasattr(self, "nation_add_extra_btn"):
                self.nation_add_extra_btn.pack_forget()
            
        # Autocomplete filtering
        if hasattr(self, "nation_name_entry") and self._cfg and "nations" in self._cfg:
            val = self.nation_name_var.get().lower()
            nations = sorted(self._cfg["nations"].keys())
            if val == "":
                self.nation_name_entry["values"] = nations
            else:
                filtered = [n for n in nations if val in n.lower()]
                self.nation_name_entry["values"] = filtered

    def _refresh_nation_combobox(self) -> None:
        if hasattr(self, "nation_name_entry") and self._cfg and "nations" in self._cfg:
            self.nation_name_entry["values"] = sorted(self._cfg["nations"].keys())

    def _add_nation_from_ui(self, add_extra=False) -> None:
        if self._cfg is None:
            messagebox.showinfo("Nations", "Load or create a project first.")
            return
        if self._last_pol_rgb is None:
            messagebox.showinfo("Nations", "Sample a colour on the political map first.")
            return
        name = self.nation_name_var.get().strip()
        if not name:
            messagebox.showinfo("Nations", "Enter a nation name.")
            return
        
        if "nations" not in self._cfg:
            self._cfg["nations"] = {}
            
        # Check for duplicates
        for existing_name, existing_rgb in self._cfg["nations"].items():
            if existing_name != name:
                if not existing_rgb: continue
                if isinstance(existing_rgb[0], (int, float)):
                    rgbs = [existing_rgb]
                else:
                    rgbs = existing_rgb
                if list(self._last_pol_rgb) in rgbs:
                    self._log(f"WARNING: RGB {list(self._last_pol_rgb)} is also used by {existing_name}. "
                              f"You may need duplicate_fill_splits in config.yaml.")
        
        if add_extra and name in self._cfg["nations"]:
            existing = self._cfg["nations"][name]
            if isinstance(existing[0], (int, float)):
                self._cfg["nations"][name] = [existing, list(self._last_pol_rgb)]
            else:
                self._cfg["nations"][name].append(list(self._last_pol_rgb))
            self._log(f"Added extra colour to nation: {name} -> {list(self._last_pol_rgb)}")
        else:
            self._cfg["nations"][name] = list(self._last_pol_rgb)
            self._log(f"Added/Replaced nation: {name} -> {list(self._last_pol_rgb)}")
            
        self._bump_cfg_revision()
        self._invalidate_all_caches()
        
        self._refresh_nation_combobox()
        self._update_nation_ui_state()
        
        if self._nation_list_window is not None and self._nation_list_window.winfo_exists():
            self._populate_nation_list()

    def _add_ocean_from_ui(self) -> None:
        if self._cfg is None:
            messagebox.showinfo("Ocean", "Load or create a project first.")
            return
        if self._last_pol_rgb is None:
            messagebox.showinfo("Ocean", "Sample a colour on the political map first.")
            return
        
        if "ocean_colors" not in self._cfg:
            self._cfg["ocean_colors"] = []
        
        rgb_list = list(self._last_pol_rgb)
        if rgb_list not in self._cfg["ocean_colors"]:
            self._cfg["ocean_colors"].append(rgb_list)
            self._bump_cfg_revision()
            self._invalidate_all_caches()
            if self._pol is not None:
                self._ocean_pol = ocean_mask_exact(self._pol, self._cfg["ocean_colors"])
            self._log(f"Added ocean colour: {rgb_list}")
            if self._nation_list_window is not None and self._nation_list_window.winfo_exists():
                self._populate_nation_list()
        else:
            self._log(f"Ocean colour {rgb_list} already exists.")

    def _masks_for_rgb_group(
        self, rgb: np.ndarray, rgb_triplet: tuple[int, int, int], names: list[str]
    ) -> dict[str, np.ndarray]:
        # This is used for the eyedropper to resolve shared colors quickly if possible
        # but it's still heavy. We'll use the cached _eez_land_masks instead.
        return {}

    def _on_canvas_click(self, event: tk.Event) -> None:
        try:
            self._handle_canvas_click(event)
        except Exception as e:
            import traceback
            err = traceback.format_exc()
            self._log(f"ERROR during click:\n{err}")
            print(f"ERROR during click:\n{err}", file=sys.stderr)

    def _handle_canvas_click(self, event: tk.Event) -> None:
        tool = self.map_tool.get()
        if tool == "political_eyedropper":
            if self._pol is None:
                messagebox.showinfo("Eyedropper", "Load a Political Map first.")
                return
            p = self._canvas_to_img(event.x, event.y)
            if p is None:
                return
            ix, iy = p
            rgb = tuple(int(x) for x in self._pol[iy, ix])
            self._last_pol_rgb = rgb
            
            # Check if this belongs to a known nation
            owner_text = "Unclaimed / Unknown"
            
            # 1. First check the computed masks and EEZs if available
            # Use the full-map masks if we have them, otherwise fallback to cache
            land_masks = getattr(self, "_eez_land_masks", None)
            offshore_owner = getattr(self, "_eez_offshore_owner", None)
            nation_order = getattr(self, "_eez_nation_order", None)

            if land_masks:
                # Check land
                for name, mask in land_masks.items():
                    if mask[iy, ix]:
                        owner_text = name
                        break
                
                # If not land, check EEZ
                if owner_text == "Unclaimed / Unknown" and offshore_owner is not None:
                    wo = offshore_owner[iy, ix]
                    if wo >= 0 and wo < len(nation_order):
                        owner_text = f"Ocean ({nation_order[wo]} EEZ)"
                    else:
                        # Check if it's just ocean
                        if self._cfg and "ocean_colors" in self._cfg:
                            for o_rgb in self._cfg["ocean_colors"]:
                                if tuple(o_rgb) == rgb:
                                    owner_text = "Ocean (Unclaimed)"
                                    break
            
            # 2. Fallback to simple RGB check if EEZs haven't computed yet
            if owner_text == "Unclaimed / Unknown" and self._cfg and "nations" in self._cfg:
                # First check for exact matches
                for name, col_or_cols in self._cfg["nations"].items():
                    if not col_or_cols: continue
                    if isinstance(col_or_cols[0], (int, float)):
                        rgbs = [col_or_cols]
                    else:
                        rgbs = col_or_cols
                    if any(tuple(c) == rgb for c in rgbs):
                        owner_text = name
                        break
                
                # If we found a match, check if it's a shared color
                if owner_text != "Unclaimed / Unknown":
                    sharing = []
                    for n, col_or_cols in self._cfg["nations"].items():
                        if not col_or_cols: continue
                        if isinstance(col_or_cols[0], (int, float)):
                            rgbs = [col_or_cols]
                        else:
                            rgbs = col_or_cols
                        if any(tuple(c) == rgb for c in rgbs):
                            sharing.append(n)
                            
                    if len(sharing) > 1:
                        owner_text = f"Shared colour ({', '.join(sharing)})"
            
            # Check if it's ocean (fallback)
            if owner_text == "Unclaimed / Unknown" and self._cfg and "ocean_colors" in self._cfg:
                for o_rgb in self._cfg["ocean_colors"]:
                    if tuple(o_rgb) == rgb:
                        owner_text = "Ocean"
                        break
                        
            self.pol_rgb_label.config(text=f"Sampled RGB: {rgb[0]}, {rgb[1]}, {rgb[2]} ({owner_text})")
            self._log(f"Sampled political map: {rgb} -> {owner_text}")
            return
        elif tool == "eyedropper":
            if self._res is None:
                messagebox.showinfo("Eyedropper", "Load a resource PNG first.")
                return
            if self._cfg is None:
                messagebox.showinfo(
                    "Eyedropper",
                    "Load config.yaml (or use New minimal config) before sampling anchors.",
                )
                return
            p = self._canvas_to_img(event.x, event.y)
            if p is None:
                messagebox.showinfo(
                    "Eyedropper",
                    "Click on the map image (not the grey margin).",
                )
                return
            com = self.commodity_var.get()
            if not com:
                messagebox.showinfo("Eyedropper", "Add or select a commodity first.")
                return
            ix, iy = p
            trip = [int(x) for x in self._res[iy, ix].astype(int).tolist()]
            self._get_session_anchors(com).append(trip)
            self.anchor_listbox.insert(tk.END, str(trip))
            self._log(
                f"Eyedropper + {com}: {trip} (resource map @ {ix},{iy}; "
                "not the blended canvas colour)"
            )
            return
        p = self._canvas_to_img(event.x, event.y)
        if p is None:
            return
        snapped = self._snap_lasso_vertex_ixiy(event.x, event.y)
        self._lasso_pts.append(snapped if snapped is not None else p)
        self._schedule_redraw_fast()

    def _on_canvas_double_click(self, event: tk.Event) -> None:
        if self.map_tool.get() == "political_eyedropper":
            self._on_canvas_click(event)
            self._add_nation_from_ui()

    def _clear_lasso(self) -> None:
        self._lasso_pts.clear()
        self._schedule_redraw_fast()

    def _clear_closed_lassos(self) -> None:
        self._lasso_closed_polys.clear()
        self._schedule_redraw_fast()

    def _close_lasso(self) -> None:
        if len(self._lasso_pts) < 3:
            messagebox.showinfo("Lasso", "Need at least 3 corners (click map), then Close.")
            return
        commodities = self._lasso_commodities_for_stats()
        if not commodities:
            messagebox.showinfo(
                "Lasso",
                "Select commodities in the lasso list (Ctrl+click) or pick one in Deposit above the map.",
            )
            return
        verts = list(self._lasso_pts)
        self._lasso_closed_polys.append(verts)
        self._lasso_pts.clear()
        self._lasso_log_stats_for_polygon(verts, commodities, zone_label=None)
        self._schedule_redraw_fast()


def main() -> None:
    app = DepositTunerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
