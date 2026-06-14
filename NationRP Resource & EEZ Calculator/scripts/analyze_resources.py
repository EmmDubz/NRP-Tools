#!/usr/bin/env python3
"""
Overlay political (nations + ocean) and resource rasters.

Counts resource-colour pixels per nation on:
  - national land (political fill colours, excluding ocean / ignore list)
  - optional map title/label pixels (see nation_title_colors) merged into the
    nearest nation's land so halos and land resource totals are not "cut out"
  - offshore halo: water pixels (ocean colour) within a Euclidean **image-space**
    radius (**offshore_halo_px** in config, or ``--halo-px`` / ``--halo-km`` on the CLI)
    of that nation's effective land, assigned by nearest-distance-to-coast among
    all nations within the band (ties: earlier entry in YAML ``nations:`` wins).

When two or more nations share the **same RGB** in `nations:`, one mask is built
for that colour and split: optional `duplicate_fill_splits` seeds, otherwise
Lloyd k-means on pixel coordinates — **larger** cluster -> **first** nation in
the YAML declaration order for that colour group.

Resource map matching uses CIELAB dE (CIE76) so shaded / gradient pixels that
are perceptually "the same" commodity still count. Each commodity can list
multiple RGB anchors and its own max_delta_e. Pixels that fit more than one
commodity are assigned by lowest dE (ties -> earlier key in resource_legend).

Optional resource_propagation in config: seed strong spectral hits, grow to
neighbors under loose dE, guard oil vs natural_gas when both are competitive,
then fall back to per-pixel winners.

Requires political and resource images with identical width × height.

Optional ``deposit_sampler_zones`` in ``config.yaml`` (lasso polygons in image pixels):
when present, ``analyze_resources`` also writes ``deposit_zones_attribution.json`` next to
the CSV/JSON outputs, with per-zone ``eez_offshore_px``, ``beyond_halo_ocean_px``, and
``on_land_px`` for econ/reporting (same halo rule as the main CSV run).
"""

from __future__ import annotations

import argparse
import csv
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, TypeVar

import numpy as np
import yaml
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.spatial.distance import cdist

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover

    T = TypeVar("T")

    def tqdm(
        it: list[T] | range,
        **_: Any,
    ) -> list[T] | range:
        return it


def load_rgb(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c / 255.0, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB 0–255 -> CIELAB (D65). Last axis must be 3 (R,G,B)."""
    r, g, b = (
        _srgb_to_linear(rgb[..., 0]),
        _srgb_to_linear(rgb[..., 1]),
        _srgb_to_linear(rgb[..., 2]),
    )
    X = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    Y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    Z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    Xn, Yn, Zn = 0.95047, 1.0, 1.08883
    x, y, z = X / Xn, Y / Yn, Z / Zn

    delta = (6 / 29) ** 3

    def f(t: np.ndarray) -> np.ndarray:
        return np.where(t > delta, np.cbrt(t), (t / (3 * (6 / 29) ** 2)) + 4 / 29)

    fx, fy, fz = f(x), f(y), f(z)
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b2 = 200.0 * (fy - fz)
    return np.stack([L, a, b2], axis=-1)


def min_delta_e_to_anchors(lab_image: np.ndarray, anchors_rgb: list[list[int]]) -> np.ndarray:
    """H×W×3 Lab image; return H×W min dE to any anchor (sRGB anchors)."""
    a = np.asarray(anchors_rgb, dtype=np.uint8).reshape(-1, 3)
    lab_a = rgb_to_lab(a.reshape(-1, 1, 1, 3)).reshape(-1, 3)  # N×3
    diff = lab_image[..., None, :] - lab_a[None, None, :, :]
    d2 = np.sum(diff * diff, axis=-1)
    return np.sqrt(np.min(d2, axis=-1))


def color_close(rgb: np.ndarray, targets: list[list[int]], tol: int) -> np.ndarray:
    if not targets:
        return np.zeros(rgb.shape[:2], dtype=bool)
    masks = []
    r16 = rgb.astype(np.int16)
    for t in targets:
        t = np.array(t, dtype=np.int16)
        masks.append(np.all(np.abs(r16 - t) <= tol, axis=-1))
    return np.any(np.stack(masks, axis=0), axis=0)


def _lloyd_split_union(
    union: np.ndarray, k: int, centers_xy: np.ndarray | None, max_iter: int = 25
) -> np.ndarray:
    """
    union: H×W bool. Returns H×W int labels 0..k-1.
    centers_xy: k×2 (x, y) starting centers. If provided, one fixed Voronoi step
    only (seeds define regions). If None, Lloyd refinement with random init.
    """
    ys, xs = np.where(union)
    if len(xs) == 0:
        return np.full(union.shape, -1, dtype=np.int32)
    pts = np.stack([xs.astype(np.float64), ys.astype(np.float64)], axis=1)
    npt = pts.shape[0]
    if centers_xy is not None:
        centers = np.asarray(centers_xy, dtype=np.float64).reshape(k, 2).copy()
        d = cdist(pts, centers, "sqeuclidean")
        assign = np.argmin(d, axis=1)
        lab_img = np.full(union.shape, -1, dtype=np.int32)
        lab_img[ys, xs] = assign
        return lab_img

    mins = pts.min(axis=0)
    maxs = pts.max(axis=0)
    rng = np.random.default_rng(42)
    centers = mins + (maxs - mins) * rng.random((k, 2))

    for _ in range(max_iter):
        d = cdist(pts, centers, "sqeuclidean")
        assign = np.argmin(d, axis=1)
        prev = centers.copy()
        for j in range(k):
            sel = pts[assign == j]
            if len(sel):
                centers[j] = sel.mean(axis=0)
        if np.allclose(centers, prev):
            break

    lab_img = np.full(union.shape, -1, dtype=np.int32)
    lab_img[ys, xs] = assign
    return lab_img


def _seeds_to_centers(
    names: list[str],
    seeds_xy: dict[str, list[int]],
) -> np.ndarray:
    return np.array(
        [[float(seeds_xy[n][0]), float(seeds_xy[n][1])] for n in names],
        dtype=np.float64,
    )


def _find_split_config(cfg: dict, names: list[str]) -> dict | None:
    want = set(names)
    for entry in cfg.get("duplicate_fill_splits", []) or []:
        if set(entry.get("nations", [])) == want:
            return entry
    return None


def _masks_for_rgb_group(
    rgb: np.ndarray,
    rgb_triplet: tuple[int, int, int],
    names: list[str],
    ocean: np.ndarray,
    ignore: np.ndarray,
    tol: int,
    cfg: dict,
) -> dict[str, np.ndarray]:
    col = [list(rgb_triplet)]
    raw = color_close(rgb, col, tol) & ~ocean & ~ignore
    out: dict[str, np.ndarray] = {}
    if len(names) == 1:
        out[names[0]] = raw.copy()
        return out

    split_cfg = _find_split_config(cfg, names)
    seeds = (split_cfg or {}).get("seeds_xy") or (split_cfg or {}).get("seeds")
    polygon_xy = (split_cfg or {}).get("polygon_xy")
    
    if polygon_xy:
        claimed = np.zeros_like(raw, dtype=bool)
        h, w = rgb.shape[:2]
        
        # 1. First assign pixels that are explicitly inside a nation's polygon
        for name in names:
            pts = polygon_xy.get(name)
            if pts:
                poly_mask = sampler_zone_polygon_mask(pts, w, h)
                # If a nation has a polygon, it claims ALL land pixels of the group color inside it.
                m = raw & poly_mask
                out[name] = m
                claimed |= m
            else:
                out[name] = np.zeros_like(raw, dtype=bool)
                
        # 2. For any remaining unclaimed pixels of this color, assign to the nearest nation in the group
        unclaimed = raw & ~claimed
        if np.any(unclaimed):
            nations_with_polys = [n for n in names if n in polygon_xy]
            
            if nations_with_polys:
                # Calculate centroids of polygons to use as seeds
                all_centers = []
                for name in names:
                    if name in polygon_xy:
                        pts = np.array(polygon_xy[name])
                        # If a nation has multiple polygons, we use the centroid of all points
                        all_centers.append(pts.mean(axis=0))
                    else:
                        # For nations without polygons, use a dummy far-away point
                        all_centers.append([1e9, 1e9])
                
                labels = _lloyd_split_union(unclaimed, len(names), np.array(all_centers))
                for i, name in enumerate(names):
                    out[name] |= (labels == i) & unclaimed
            else:
                # Fallback: assign all unclaimed to the first nation if no polygons defined at all
                out[names[0]] |= unclaimed
    elif seeds:
        centers = _seeds_to_centers(names, seeds)
        labels = _lloyd_split_union(raw, len(names), centers)
        for yaml_idx, j in enumerate(range(len(names))):
            m = (labels == j) & raw
            out[names[yaml_idx]] = m
    else:
        labels = _lloyd_split_union(raw, len(names), None)
        clusters = [int(np.sum(labels == j)) for j in range(len(names))]
        j_sorted = sorted(range(len(names)), key=lambda j: -clusters[j])
        for yaml_idx, j in enumerate(j_sorted):
            m = (labels == j) & raw
            out[names[yaml_idx]] = m
    return out


def _group_nations_by_rgb(nations_cfg: dict[str, list]) -> list[tuple[tuple[int, int, int], list[str]]]:
    rgb_to_names: dict[tuple[int, int, int], list[str]] = {}
    for name, col_or_cols in nations_cfg.items():
        if not col_or_cols:
            continue
        if isinstance(col_or_cols[0], (int, float)):
            cols = [col_or_cols]
        else:
            cols = col_or_cols
            
        for col in cols:
            key = tuple(int(x) for x in col)
            names_list = rgb_to_names.setdefault(key, [])
            if name not in names_list:
                names_list.append(name)
    return list(rgb_to_names.items())


def nation_masks_and_context(rgb: np.ndarray, cfg: dict):
    tol = int(cfg.get("color_tolerance", 0))
    ocean = color_close(rgb, list(cfg.get("ocean_colors", [])), tol)
    ignore = color_close(rgb, list(cfg.get("ignore_land_colors", [])), tol)
    nations_cfg: dict = cfg.get("nations", {})
    masks: dict[str, np.ndarray] = {}
    for rgb_key, names in _group_nations_by_rgb(nations_cfg):
        group_masks = _masks_for_rgb_group(
            rgb, rgb_key, names, ocean, ignore, tol, cfg
        )
        for name, m in group_masks.items():
            if name in masks:
                masks[name] |= m
            else:
                masks[name] = m
    land_union = np.zeros_like(ocean, dtype=bool)
    for m in masks.values():
        land_union |= m
    water = ocean & ~land_union
    order = list(nations_cfg.keys())
    return masks, water, land_union, order, ocean


def effective_nation_masks(
    rgb: np.ndarray,
    cfg: dict,
    solid_masks: dict[str, np.ndarray],
    nation_order: list[str],
    ocean: np.ndarray,
) -> dict[str, np.ndarray]:
    """
    Nation labels on the political map often overwrite fill colour. Those pixels
    are matched via nation_title_colors; each title pixel within
    title_land_attach_px of any solid land is assigned to the nation whose solid
    land is closest (tie -> earlier nation_order).
    """
    tol = int(cfg.get("color_tolerance", 0))
    title_colors = list(cfg.get("nation_title_colors", []))
    if not title_colors:
        return solid_masks

    attach = max(0, int(cfg.get("title_land_attach_px", 8)))
    title_pix = color_close(rgb, title_colors, tol) & ~ocean

    land_u = np.zeros_like(ocean, dtype=bool)
    for m in solid_masks.values():
        land_u |= m

    if attach == 0:
        near_land = land_u.copy()
    else:
        near_land = ndimage.binary_dilation(land_u, iterations=attach)
    title_candidate = title_pix & near_land
    if not np.any(title_candidate):
        return solid_masks

    dists = np.stack(
        [ndimage.distance_transform_edt(~solid_masks[n]) for n in nation_order],
        axis=0,
    )
    owner = np.argmin(dists, axis=0)

    return {
        name: solid_masks[name] | (title_candidate & (owner == i))
        for i, name in enumerate(nation_order)
    }


def parse_commodity_spec(raw: Any, default_de: float) -> tuple[list[list[int]], float]:
    """YAML value: [r,g,b] | list of anchors | dict anchors + max_delta_e."""
    if isinstance(raw, dict):
        anchors = raw["anchors"]
        de = float(raw.get("max_delta_e", default_de))
        return [list(map(int, row)) for row in anchors], de
    if (
        isinstance(raw, list)
        and len(raw) == 3
        and all(isinstance(x, (int, float)) for x in raw)
    ):
        return [[int(raw[0]), int(raw[1]), int(raw[2])]], default_de
    if isinstance(raw, list) and raw and isinstance(raw[0], (list, tuple)):
        return [list(map(int, row)) for row in raw], default_de
    raise ValueError(f"Cannot parse resource_legend entry: {raw!r}")


def _resource_spectral_tensors(
    rgb: np.ndarray, cfg: dict
) -> tuple[
    list[str],
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """
    Shared per-pixel spectral math for ``resource_masks`` and diagnostics.

    Returns
        commodities, dstack (C×H×W ΔE), thresh (C×1×1), exclude, valid,
        winner (argmin among in-threshold commodities), minval (best ΔE or +inf).
    """
    legend = cfg.get("resource_legend", {})
    if not legend:
        raise ValueError("resource_legend is empty")
    default_de = float(cfg.get("resource_max_delta_e", 18.0))
    lab = rgb_to_lab(rgb)
    commodities = list(legend.keys())
    dist_stack = []
    thresholds = []
    for key in commodities:
        anchors, de = parse_commodity_spec(legend[key], default_de)
        dist_stack.append(min_delta_e_to_anchors(lab, anchors))
        thresholds.append(de)
    dstack = np.stack(dist_stack, axis=0).astype(np.float64)
    thresh = np.array(thresholds, dtype=np.float64)[:, np.newaxis, np.newaxis]
    masked = np.where(dstack <= thresh, dstack, np.inf)
    winner = np.argmin(masked, axis=0)
    minval = np.min(masked, axis=0)
    valid = np.isfinite(minval)
    exclude = color_close(
        rgb,
        list(cfg.get("resource_exclude_colors", [])),
        int(cfg.get("resource_exclude_tolerance", 0)),
    )
    return commodities, dstack, thresh, exclude, valid, winner, minval


def resource_spectral_diagnostics(rgb: np.ndarray, cfg: dict) -> dict[str, np.ndarray]:
    """
    Boolean H×W maps for tuner overlays — **ignores** ``resource_propagation``
    (pure per-pixel spectral gates).

    Keys:

    - ``spectral_void``: no commodity within its ``max_delta_e`` (and not excluded).
    - ``oil_gas_ambiguous``: oil **and** natural_gas both in-range and
      |ΔE_oil − ΔE_gas| < ``resource_propagation.oil_gas_ambiguity_delta``
      (default 5); same rule as propagation guard, for manual review.
    """
    legend = cfg.get("resource_legend", {})
    if not legend:
        return {}
    commodities, dstack, thresh, exclude, valid, _winner, _minv = _resource_spectral_tensors(
        rgb, cfg
    )
    h, w = rgb.shape[:2]
    spectral_void = (~valid) & (~exclude)
    prop = cfg.get("resource_propagation") or {}
    amb_d = float(prop.get("oil_gas_ambiguity_delta", 5.0))
    oil_i = commodities.index("oil") if "oil" in commodities else None
    gas_i = commodities.index("natural_gas") if "natural_gas" in commodities else None
    if oil_i is not None and gas_i is not None:
        do = dstack[oil_i]
        dg = dstack[gas_i]
        to = float(thresh[oil_i, 0, 0])
        tg = float(thresh[gas_i, 0, 0])
        oil_gas_ambiguous = (
            (do <= to) & (dg <= tg) & (np.abs(do - dg) < amb_d) & (~exclude)
        )
    else:
        oil_gas_ambiguous = np.zeros((h, w), dtype=bool)
    return {"spectral_void": spectral_void, "oil_gas_ambiguous": oil_gas_ambiguous}


def _resource_propagation_pass(
    labels: np.ndarray,
    dstack: np.ndarray,
    thresh: np.ndarray,
    n: int,
    oil_i: int | None,
    gas_i: int | None,
    amb_d: float,
    exclude: np.ndarray,
) -> tuple[np.ndarray, bool]:
    """One step of neighbor fill for unknown (-1) pixels. Returns (new_labels, changed)."""
    h, w = labels.shape
    unk = (labels < 0) & ~exclude
    if not np.any(unk):
        return labels, False

    labels_pad = np.pad(labels.astype(np.int16), 1, constant_values=-1)
    best_d = np.full((h, w), np.inf, dtype=np.float64)
    best_l = np.full((h, w), -1, dtype=np.int16)
    yi, xi = np.indices((h, w))
    t_flat = thresh[:, 0, 0].astype(np.float64)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            slab = labels_pad[1 + dy : h + 1 + dy, 1 + dx : w + 1 + dx]
            cand = unk & (slab >= 0)
            if not np.any(cand):
                continue
            li = slab.astype(np.intp)
            d_pix = dstack[li, yi, xi]
            t_pix = t_flat[li]
            ok = cand & (li >= 0) & (li < n) & (d_pix <= t_pix)
            better = ok & (d_pix < best_d)
            best_d = np.where(better, d_pix, best_d)
            best_l = np.where(better, li.astype(np.int16), best_l)

    if oil_i is not None and gas_i is not None:
        do = dstack[oil_i]
        dg = dstack[gas_i]
        to = float(thresh[oil_i, 0, 0])
        tg = float(thresh[gas_i, 0, 0])
        amb_mask = (do <= to) & (dg <= tg) & (np.abs(do - dg) < amb_d)
        amb_clear = (labels < 0) & ~exclude & amb_mask & (
            (best_l == oil_i) | (best_l == gas_i)
        )
        best_l = np.where(amb_clear, np.int16(-1), best_l)

    upd = unk & (best_l >= 0)
    if not np.any(upd):
        return labels, False
    out = labels.copy()
    out[upd] = best_l[upd]
    return out, True


def resource_masks(rgb: np.ndarray, cfg: dict) -> dict[str, np.ndarray]:
    """
    Each pixel assigned to at most one commodity: smallest dE among those within
    that commodity's threshold; if none qualify, pixel is not counted as resource.

    If ``resource_propagation.enabled`` is true in cfg, classification is
    two-phase: (1) spectral seeds (tight inner fraction of each threshold, plus
    optional high-confidence loose pixels), (2) region-grow to neighbors that
    still pass the loose dE gate, with an oil / natural_gas ambiguity guard,
    then (3) fallback to the legacy single-pixel winner anywhere still unknown
    but spectrally valid.
    """
    legend = cfg.get("resource_legend", {})
    if not legend:
        return {}
    commodities, dstack, thresh, exclude, valid, winner, minval = _resource_spectral_tensors(
        rgb, cfg
    )
    n = len(commodities)

    prop = cfg.get("resource_propagation") or {}
    if not bool(prop.get("enabled", False)):
        return {
            commodities[i]: valid & (winner == i) & ~exclude
            for i in range(n)
        }

    h, w = rgb.shape[:2]
    oil_i = commodities.index("oil") if "oil" in commodities else None
    gas_i = (
        commodities.index("natural_gas") if "natural_gas" in commodities else None
    )
    inner_frac = float(prop.get("inner_fraction", 0.55))
    conf_margin = float(prop.get("confidence_margin", 3.0))
    max_passes = int(prop.get("max_passes", 48))
    amb_d = float(prop.get("oil_gas_ambiguity_delta", 5.0))

    inner_thresh = thresh * inner_frac
    masked_inner = np.where(dstack <= inner_thresh, dstack, np.inf)
    winner_inner = np.argmin(masked_inner, axis=0)
    min_inner = np.min(masked_inner, axis=0)
    seed_inner = np.isfinite(min_inner) & ~exclude

    dm = np.min(dstack, axis=0)
    second = np.full((h, w), np.inf, dtype=np.float64)
    for c in range(n):
        dc = dstack[c]
        np.minimum(second, np.where(dc > dm + 1e-3, dc, np.inf), out=second)
    margin = second - dm
    tw = np.take(thresh[:, 0, 0], winner.astype(np.intp))
    seed_conf = valid & (margin > conf_margin) & (dm <= tw) & ~exclude

    labels = np.full((h, w), -1, dtype=np.int16)
    labels = np.where(seed_inner, winner_inner.astype(np.int16), labels)
    labels = np.where((labels < 0) & seed_conf, winner.astype(np.int16), labels)
    if np.count_nonzero(labels >= 0) == 0:
        labels = np.where(valid & ~exclude, winner.astype(np.int16), labels)

    for _ in range(max_passes):
        labels, chg = _resource_propagation_pass(
            labels, dstack, thresh, n, oil_i, gas_i, amb_d, exclude
        )
        if not chg:
            break

    fb = (labels < 0) & valid & ~exclude
    labels = np.where(fb, winner.astype(np.int16), labels)
    labels = np.where(exclude, np.int16(-1), labels)

    return {
        commodities[i]: (labels == i) & ~exclude for i in range(n)
    }


def assign_offshore(
    land_masks: dict[str, np.ndarray],
    water_mask: np.ndarray,
    halo_px: float,
    nation_order: list[str],
) -> tuple[np.ndarray, np.ndarray]:
    # Label water connected components to prevent EEZ jumping across unclaimed land
    labeled_water, _ = ndimage.label(water_mask)
    
    h, w = water_mask.shape
    owner = np.full((h, w), -1, dtype=np.int32)
    BIG = 1e9
    best = np.full((h, w), BIG, dtype=np.float64)

    for idx, name in enumerate(nation_order):
        land = land_masks[name]
        
        # Find which water components this country touches
        # Dilate land by 3 pixels to jump over 1-2px anti-aliasing or black outlines
        touches_water = ndimage.binary_dilation(land, iterations=3) & water_mask
        touched_labels = np.unique(labeled_water[touches_water])
        touched_labels = touched_labels[touched_labels > 0]
        
        if len(touched_labels) == 0:
            continue
            
        valid_water_for_country = np.isin(labeled_water, touched_labels)
        
        d = ndimage.distance_transform_edt(~land)
        score = np.where((d <= halo_px) & valid_water_for_country, d, BIG)
        take = score < best
        owner = np.where(take, idx, owner)
        best = np.where(take, score, best)

    owner = np.where(best < BIG, owner, -1)
    return owner, np.where(best < BIG, best, np.nan)


def sampler_zone_polygon_mask(
    points_xy: list[Any], width: int, height: int
) -> np.ndarray:
    """Boolean mask for a closed polygon (same pixel frame as political map)."""
    if not points_xy:
        return np.zeros((height, width), dtype=bool)
        
    poly = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(poly)
    
    # Check if we have a list of polygons (multi-polygon) or a single list of points
    # A single polygon is [[x,y], [x,y], ...]
    # A multi-polygon is [[[x,y],...], [[x,y],...]]
    if isinstance(points_xy[0][0], (int, float)):
        # Single polygon
        flat: list[int] = []
        for p in points_xy:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                flat.extend([int(p[0]), int(p[1])])
        if len(flat) >= 6:
            draw.polygon(flat, outline=1, fill=1)
    else:
        # Multi-polygon
        for sub_poly in points_xy:
            flat: list[int] = []
            for p in sub_poly:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    flat.extend([int(p[0]), int(p[1])])
            if len(flat) >= 6:
                draw.polygon(flat, outline=1, fill=1)
                
    return np.asarray(poly) > 0


def attribute_deposit_sampler_zones(
    political_rgb: np.ndarray,
    cfg: dict[str, Any],
    halo_px: float,
    zones: list[dict[str, Any]],
    *,
    use_effective_land_for_halo: bool = True,
) -> None:
    """
    Enrich ``deposit_sampler_zones``-style dicts in-place for econ / reporting.

    Each zone should have ``points_xy``. Sets:

    - ``eez_offshore_px``: mapping nation name -> pixel count inside the polygon
      that are ocean and assigned to that nation's halo (same rule as
      ``pixels_offshore`` in the CSV).
    - ``beyond_halo_ocean_px``: ocean pixels in the polygon not within any halo.
    - ``on_land_px``: nation -> land pixels under the polygon.
    - ``halo_px_used``, ``attribute_mode``.
    """
    height, width = political_rgb.shape[:2]
    nation_masks, _wctx, _land_u, nation_order, ocean = nation_masks_and_context(
        political_rgb, cfg
    )
    if not nation_order:
        return
    if use_effective_land_for_halo:
        land_masks = effective_nation_masks(
            political_rgb, cfg, nation_masks, nation_order, ocean
        )
        mode = "effective_land"
    else:
        land_masks = nation_masks
        mode = "solid_fills_only"
    land_union = np.zeros_like(ocean, dtype=bool)
    for m in land_masks.values():
        land_union |= m
    water_mask = ocean & ~land_union
    offshore_owner, _best = assign_offshore(
        land_masks, water_mask, float(halo_px), nation_order
    )
    name_to_i = {n: i for i, n in enumerate(nation_order)}
    for z in zones:
        pts = z.get("points_xy")
        if pts is None:
            pts = z.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        zm = sampler_zone_polygon_mask(pts, width, height)
        if not np.any(zm):
            continue
        eez: dict[str, int] = {}
        for name in nation_order:
            i = name_to_i[name]
            c = int(np.count_nonzero(zm & (offshore_owner == i)))
            if c:
                eez[name] = c
        beyond = int(np.count_nonzero(zm & water_mask & (offshore_owner < 0)))
        on_land: dict[str, int] = {}
        for name in nation_order:
            c = int(np.count_nonzero(zm & land_masks[name] & land_union))
            if c:
                on_land[name] = c
        z["eez_offshore_px"] = eez
        z["beyond_halo_ocean_px"] = beyond
        z["on_land_px"] = on_land
        z["halo_px_used"] = float(halo_px)
        z["attribute_mode"] = mode


def add_global_commodity_pct(rows: list[dict]) -> None:
    """In-place: add pct_of_global_commodity (0–100) from area_km2_total sums."""
    totals: dict[str, float] = {}
    for r in rows:
        c = r["commodity"]
        totals[c] = totals.get(c, 0.0) + float(r["area_km2_total"])
    for r in rows:
        t = totals.get(r["commodity"], 0.0) or 0.0
        r["pct_of_global_commodity"] = round(
            (100.0 * float(r["area_km2_total"]) / t) if t > 0 else 0.0, 4
        )


def resolve_offshore_halo_px(
    cfg: dict[str, Any],
    halo_km_cli: float | None,
    halo_px_cli: float | None,
) -> float:
    """
    Image-space halo radius in pixels for ``assign_offshore``.

    Precedence: ``halo_px_cli`` > ``halo_km_cli`` > ``offshore_halo_px`` in cfg >
    ``offshore_halo_km`` in cfg > legacy default **80 km** at ``km_per_pixel``.
    """
    km_per_px = float(cfg.get("km_per_pixel", 6.0))
    if km_per_px <= 0:
        raise SystemExit("config km_per_pixel must be positive")
    if halo_px_cli is not None and float(halo_px_cli) > 0:
        return float(halo_px_cli)
    if halo_km_cli is not None and float(halo_km_cli) > 0:
        return float(halo_km_cli) / km_per_px
    cpx = cfg.get("offshore_halo_px")
    if cpx is not None and float(cpx) > 0:
        return float(cpx)
    ckm = cfg.get("offshore_halo_km")
    if ckm is not None and float(ckm) > 0:
        return float(ckm) / km_per_px
    return 80.0 / km_per_px


def run(
    political_rgb: np.ndarray,
    resource_rgb: np.ndarray,
    cfg: dict,
    halo_px: float,
    progress: Callable[[str], None] | None = None,
    show_progress: bool = True,
) -> list[dict]:
    def p(msg: str) -> None:
        if progress:
            progress(msg)

    km_per_px = float(cfg.get("km_per_pixel", 6.0))
    halo_px = float(halo_px)
    if halo_px <= 0:
        raise SystemExit("offshore halo radius (px) must be positive")

    p("Building nation land masks (incl. duplicate-fill splits)...")
    nation_masks, _, _, nation_order, ocean = nation_masks_and_context(
        political_rgb, cfg
    )
    if not nation_order:
        raise SystemExit("config has no `nations` entries")

    p("Merging label/title pixels into nearest nation...")
    effective_land = effective_nation_masks(
        political_rgb, cfg, nation_masks, nation_order, ocean
    )
    land_union = np.zeros_like(ocean, dtype=bool)
    for m in effective_land.values():
        land_union |= m
    water_mask = ocean & ~land_union

    p("Assigning offshore halo water pixels...")
    offshore_owner, _ = assign_offshore(
        effective_land, water_mask, halo_px, nation_order
    )
    p("Classifying resource-map pixels (CIELAB delta-E)...")
    res_masks = resource_masks(resource_rgb, cfg)
    if not res_masks:
        raise SystemExit("config has no `resource_legend` entries")

    area_px_km2 = km_per_px**2
    rows: list[dict] = []
    name_to_i = {n: i for i, n in enumerate(nation_order)}

    for nation in tqdm(
        nation_order,
        desc="Per-nation commodity sums",
        unit="nation",
        disable=not show_progress,
    ):
        land_m = effective_land[nation]
        off_m = offshore_owner == name_to_i[nation]
        for commodity, rmask in res_masks.items():
            pl = int(np.sum(land_m & rmask))
            po = int(np.sum(off_m & rmask))
            rows.append(
                {
                    "nation": nation,
                    "commodity": commodity,
                    "pixels_on_land": pl,
                    "pixels_offshore": po,
                    "area_km2_land": round(pl * area_px_km2, 3),
                    "area_km2_offshore": round(po * area_px_km2, 3),
                    "area_km2_total": round((pl + po) * area_px_km2, 3),
                }
            )
    p("Computing % of global total per commodity...")
    add_global_commodity_pct(rows)
    return rows


def load_config_for_run(config_path: Path, nations_yaml: Path | None) -> dict[str, Any]:
    """
    Load base config, then optionally replace ``nations:`` entirely from a fragment
    (same format as the colour picker export). Does not modify files on disk.
    """
    cfg: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit(f"{config_path}: root must be a mapping")
    if nations_yaml is None:
        return cfg

    raw = yaml.safe_load(nations_yaml.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{nations_yaml}: root must be a mapping")
    nations = raw.get("nations")
    if not isinstance(nations, dict) or not nations:
        raise SystemExit(f"{nations_yaml}: needs a non-empty `nations:` mapping")

    normalized: dict[str, Any] = {}
    for name, col_or_cols in nations.items():
        key = str(name)
        if not col_or_cols:
            continue
        if isinstance(col_or_cols[0], (int, float)):
            if len(col_or_cols) != 3:
                raise SystemExit(f"{nations_yaml}: nations[{key!r}] must be [R, G, B] or a list of [R, G, B]")
            normalized[key] = [int(col_or_cols[0]), int(col_or_cols[1]), int(col_or_cols[2])]
        else:
            normalized[key] = []
            for col in col_or_cols:
                if not isinstance(col, (list, tuple)) or len(col) != 3:
                    raise SystemExit(f"{nations_yaml}: nations[{key!r}] must be [R, G, B] or a list of [R, G, B]")
                normalized[key].append([int(col[0]), int(col[1]), int(col[2])])

    merged: dict[str, Any] = {**cfg, "nations": normalized}
    ct = raw.get("color_tolerance")
    if isinstance(ct, int):
        merged["color_tolerance"] = ct
    return merged


def analyze_and_write_outputs(
    political: Path,
    resources: Path,
    config: Path,
    out_csv: Path,
    *,
    halo_km: float | None = None,
    halo_px: float | None = None,
    out_json: Path | None = None,
    nations_yaml: Path | None = None,
    progress: Callable[[str], None] | None = None,
    show_progress: bool = True,
) -> list[dict[str, Any]]:
    """
    Full batch run (same files as CLI): load rasters and config, compute rows,
    write CSV / optional JSON, and ``deposit_zones_attribution.json`` when
    ``deposit_sampler_zones`` is non-empty in config.

    Raises:
        ValueError: image shape mismatch.
        SystemExit: invalid YAML / empty nations / empty resource legend (same as ``run()``).
    """
    def prog(msg: str) -> None:
        if not show_progress:
            return
        if progress is not None:
            progress(msg)
        else:
            print(msg, flush=True)

    pol = load_rgb(political)
    res = load_rgb(resources)
    if pol.shape != res.shape:
        raise ValueError(
            f"Shape mismatch: political {pol.shape} vs resources {res.shape}"
        )

    cfg = load_config_for_run(config, nations_yaml)
    if nations_yaml is not None:
        n_n = len(cfg.get("nations", {}))
        prog(
            f"Using nations from {nations_yaml} ({n_n} entries); "
            f"other keys from {config}"
        )

    hp = resolve_offshore_halo_px(cfg, halo_km, halo_px)
    rows = list(
        run(
            pol,
            res,
            cfg,
            hp,
            prog if show_progress else None,
            show_progress=show_progress,
        )
    )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    else:
        out_csv.write_text("", encoding="utf-8")

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    out_base = out_json if out_json is not None else out_csv
    zones_raw = cfg.get("deposit_sampler_zones")
    if isinstance(zones_raw, list) and zones_raw:
        zones_copy = deepcopy(zones_raw)
        try:
            attribute_deposit_sampler_zones(
                pol,
                cfg,
                hp,
                zones_copy,
                use_effective_land_for_halo=True,
            )
        except (KeyError, TypeError, ValueError) as e:
            prog(f"deposit_sampler_zones attribution skipped: {e}")
        else:
            out_base.parent.mkdir(parents=True, exist_ok=True)
            sidecar = out_base.parent / "deposit_zones_attribution.json"
            sidecar.write_text(
                json.dumps(zones_copy, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            prog(f"Wrote {sidecar}")

    prog(f"Wrote {len(rows)} rows to {out_csv}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--political", type=Path, required=True)
    ap.add_argument("--resources", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument(
        "--nations-yaml",
        type=Path,
        default=None,
        help=(
            "YAML with `nations:` (and optional `color_tolerance`); replaces those "
            "for this run only — config.yaml on disk is unchanged"
        ),
    )
    ap.add_argument(
        "--halo-km",
        type=float,
        default=None,
        help="offshore radius in km (converted with km_per_pixel); optional if config sets offshore_halo_px / offshore_halo_km",
    )
    ap.add_argument(
        "--halo-px",
        type=float,
        default=None,
        help="offshore radius in image pixels (overrides --halo-km and config halo keys when set)",
    )
    ap.add_argument("--out", type=Path, required=True, help="output CSV")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument(
        "--no-progress",
        action="store_true",
        help="disable tqdm / stderr step messages",
    )
    args = ap.parse_args()

    use_progress = not args.no_progress

    def progress(msg: str) -> None:
        if use_progress:
            print(msg, flush=True)

    try:
        analyze_and_write_outputs(
            args.political,
            args.resources,
            args.config,
            args.out,
            halo_km=args.halo_km,
            halo_px=args.halo_px,
            out_json=args.json,
            nations_yaml=args.nations_yaml,
            progress=progress if use_progress else None,
            show_progress=use_progress,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
