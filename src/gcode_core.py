#!/usr/bin/env python3
"""
gcode_core.py

Shared conversion engine: raster image -> pen-plotter G-code for an
Ender 3 V2 that uses the stock Z axis (gantry) to lift/lower the pen
instead of a servo.

Pipeline:
  1. load_and_prepare      -- load, (composite alpha on white), resize,
                              edge-preserving denoise, tone-map (levels
                              stretch + CLAHE + brightness/contrast/gamma).
  2. detect_background_mask -- fixed-range flood fill from the border so a
                              genuinely uniform backdrop is excluded from
                              shading without leaking across gradients into
                              the subject.
  3. generate_outline_paths -- Canny edges -> contours -> simplified paths.
  4a. generate_hatch_paths  -- tone-mapped multi-angle crosshatch.
  4b. generate_stipple_paths-- tone-mapped dot stippling (error-diffused).
  5. order_paths            -- greedy nearest-neighbour ordering.
  6. fit_drawing / place    -- size the drawing to fit paper AND the
                              reachable bed area, keep it in the usable
                              region.
  7. paths_to_gcode         -- Z-height pen up/down (no servo, no heaters),
                              optional vertical flip / horizontal mirror so
                              the print reads the same way as the preview,
                              fan-off, dot dwell, and cleanup so no pen-down
                              move is wasted.

Both the GUI (src/main.py) and the CLI (cli/image_to_gcode.py) import from
this module so their output stays identical.
"""

import json
import math
import os
import re
import sys

import cv2
import numpy as np


# --------------------------------------------------------------------------
# Image loading / preparation
# --------------------------------------------------------------------------

def _imread_any(path, flags=cv2.IMREAD_UNCHANGED):
    """cv2.imread that survives non-ASCII / OneDrive / long Windows paths
    (cv2.imread uses plain fopen and silently returns None for those).
    Reads the bytes ourselves and decodes from memory."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as e:
        raise FileNotFoundError(f"Could not open image file: {path} ({e})")
    if data.size == 0:
        raise FileNotFoundError(f"Image file is empty or unreadable: {path}")
    img = cv2.imdecode(data, flags)
    if img is None:
        raise ValueError(
            f"Could not decode image (unsupported or corrupt format): {path}")
    return img


def _to_uint8_gray(raw):
    """Normalise whatever cv2 handed back (8/16-bit, 1/3/4 channel, float)
    into a contiguous uint8 grayscale image + an optional uint8 alpha."""
    alpha = None
    if raw.ndim == 3 and raw.shape[2] == 4:
        alpha = raw[:, :, 3]
        raw = raw[:, :, :3]

    if raw.dtype != np.uint8:
        r = raw.astype(np.float32)
        mn, mx = float(r.min()), float(r.max())
        raw = (np.zeros_like(r, dtype=np.uint8) if mx - mn < 1e-6
               else ((r - mn) * (255.0 / (mx - mn))).astype(np.uint8))
        if alpha is not None and alpha.dtype != np.uint8:
            a = alpha.astype(np.float32)
            am = float(a.max()) or 1.0
            alpha = (a * (255.0 / am)).astype(np.uint8)

    if raw.ndim == 3 and raw.shape[2] == 3:
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 3 and raw.shape[2] == 1:
        gray = raw[:, :, 0]
    else:
        gray = raw

    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    if alpha is not None:
        alpha = np.ascontiguousarray(alpha, dtype=np.uint8)
    return gray, alpha


def _apply_tone(gray, brightness=0, contrast=1.0, gamma=1.0,
                normalize=True, clahe=True, denoise=True):
    """Turn a raw grayscale frame into something with the local contrast a
    hatch/stipple pass can actually reproduce. Order matters: denoise ->
    stretch levels -> local contrast -> global brightness/contrast/gamma."""
    out = np.ascontiguousarray(gray, dtype=np.uint8)

    if denoise:
        # edge-preserving: keeps face/hair boundaries crisp while killing
        # sensor/JPEG noise that would otherwise become stray hatch marks.
        out = cv2.bilateralFilter(out, d=7, sigmaColor=45, sigmaSpace=7)

    if normalize:
        lo, hi = np.percentile(out, (2.0, 98.0))
        if hi - lo > 1e-3:
            out = np.clip((out.astype(np.float32) - lo) * (255.0 / (hi - lo)),
                          0, 255).astype(np.uint8)

    if clahe:
        out = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8)).apply(out)

    if abs(contrast - 1.0) > 1e-3 or brightness:
        out = np.clip(out.astype(np.float32) * contrast + brightness,
                      0, 255).astype(np.uint8)

    if abs(gamma - 1.0) > 1e-3:
        inv = 1.0 / max(gamma, 1e-3)
        lut = np.array([((i / 255.0) ** inv) * 255 for i in range(256)],
                       dtype=np.uint8)
        out = cv2.LUT(out, lut)

    return out


def load_and_prepare(image_path, width_mm, height_mm, max_px=1100,
                     brightness=0, contrast=1.0, gamma=1.0,
                     normalize=True, clahe=True, denoise=True):
    """Load image, composite any alpha onto white, convert to grayscale,
    resize so the longer edge is max_px, tone-map it, and compute the
    px-per-mm scale factors for the requested output size.

    Returns: gray, px_to_mm_x, px_to_mm_y, width_mm, height_mm, alpha_mask
    where alpha_mask is a bool array (True = opaque subject) or None.
    """
    raw = _imread_any(image_path)
    img, alpha = _to_uint8_gray(raw)

    if alpha is not None:
        # composite the subject over white so a transparent border doesn't
        # read as pure black in the tone pass
        a = alpha.astype(np.float32) / 255.0
        img = (img.astype(np.float32) * a + 255.0 * (1.0 - a)).astype(np.uint8)

    if img.ndim != 2:
        raise ValueError(f"Unexpected image shape after load: {img.shape}")
    h, w = img.shape
    aspect = h / w
    if height_mm is None:
        height_mm = width_mm * aspect

    if w >= h:
        new_w = max_px
        new_h = max(1, int(round(max_px * aspect)))
    else:
        new_h = max_px
        new_w = max(1, int(round(max_px / aspect)))
    interp = cv2.INTER_AREA if new_w < w else cv2.INTER_CUBIC
    img = cv2.resize(img, (new_w, new_h), interpolation=interp)
    if alpha is not None:
        alpha = cv2.resize(alpha, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

    img = _apply_tone(img, brightness, contrast, gamma, normalize, clahe, denoise)

    alpha_mask = (alpha > 16) if alpha is not None else None

    px_to_mm_x = width_mm / new_w
    px_to_mm_y = height_mm / new_h
    return img, px_to_mm_x, px_to_mm_y, width_mm, height_mm, alpha_mask


# --------------------------------------------------------------------------
# Background detection
# --------------------------------------------------------------------------

def detect_background_mask(gray, tolerance=18, alpha_mask=None):
    """Detect the "background": region(s) of near-uniform brightness that
    touch the image border. Uses a FIXED-RANGE flood fill (every candidate
    pixel is compared to the seed colour, not its filled neighbour) so a
    smooth gradient -- e.g. a lit cheek fading toward a white backdrop --
    does NOT let the fill leak into the subject the way a floating-range
    fill does.

    If the resulting mask covers more than 92% of the frame it almost
    certainly leaked; in that case only the outer border pixels are
    treated as background.

    If alpha_mask is given (from a cut-out PNG), everything transparent is
    background and everything opaque is kept -- no flood fill needed.

    Returns a bool mask, True where a pixel is background.
    """
    h, w = gray.shape

    if alpha_mask is not None:
        return ~alpha_mask

    flood_mask = np.zeros((h + 2, w + 2), np.uint8)
    work = gray.copy()

    step_x = max(1, w // 80)
    step_y = max(1, h // 80)
    seeds = set()
    for x in range(0, w, step_x):
        seeds.add((x, 0)); seeds.add((x, h - 1))
    for y in range(0, h, step_y):
        seeds.add((0, y)); seeds.add((w - 1, y))

    flags = (4 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE
             | (255 << 8))
    for (x, y) in seeds:
        if flood_mask[y + 1, x + 1] == 0:
            cv2.floodFill(work, flood_mask, (x, y), 0,
                          loDiff=tolerance, upDiff=tolerance, flags=flags)

    mask = flood_mask[1:-1, 1:-1] > 0

    if mask.mean() > 0.92:
        mask = np.zeros((h, w), bool)
        mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = True
        return mask

    # close pinholes so isolated bright specks inside the subject aren't
    # counted as background, then drop a 1px rind back so we don't shave
    # the subject outline.
    m = mask.astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return m > 0


def _subject_mask(gray_shape, background_mask):
    if background_mask is None:
        return np.ones(gray_shape, bool)
    return ~background_mask


# --------------------------------------------------------------------------
# Outline pass
# --------------------------------------------------------------------------

def generate_outline_paths(gray, low, high, min_len=6, epsilon=1.2,
                           background_mask=None):
    edges = cv2.Canny(gray, low, high, L2gradient=True)
    if background_mask is not None:
        # never trace an edge that only exists because of the backdrop
        edges[background_mask] = 0
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)

    paths = []
    for c in contours:
        if len(c) < min_len:
            continue
        approx = cv2.approxPolyDP(c, epsilon, closed=False)
        pts = [(float(p[0][0]), float(p[0][1])) for p in approx]
        if len(pts) < 2:
            continue
        if background_mask is not None:
            hh, ww = background_mask.shape
            in_bg = sum(
                1 for (px, py) in pts
                if 0 <= int(round(px)) < ww and 0 <= int(round(py)) < hh
                and background_mask[int(round(py)), int(round(px))]
            )
            if in_bg / len(pts) > 0.6:
                continue
        paths.append(pts)
    return paths


# --------------------------------------------------------------------------
# Shading pass -- tone-mapped crosshatch
# --------------------------------------------------------------------------

def hatch_lines_for_mask(mask, angle_deg, spacing_px, sample_step=1.0):
    """Parallel line segments at angle_deg, spacing_px apart, clipped to the
    True regions of `mask`. A run is only emitted where the mask is set, so
    the pen is never down over a region that shouldn't be inked."""
    h, w = mask.shape
    theta = math.radians(angle_deg)
    dx, dy = math.cos(theta), math.sin(theta)
    nx, ny = -math.sin(theta), math.cos(theta)

    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    offsets = [nx * x + ny * y for x, y in corners]
    alongs = [dx * x + dy * y for x, y in corners]
    min_off, max_off = min(offsets), max(offsets)
    min_along, max_along = min(alongs), max(alongs)

    paths = []
    offset = min_off
    while offset <= max_off:
        base_x, base_y = nx * offset, ny * offset
        t = min_along
        run = []
        while t <= max_along:
            x = base_x + dx * t
            y = base_y + dy * t
            xi, yi = int(round(x)), int(round(y))
            if 0 <= xi < w and 0 <= yi < h and mask[yi, xi]:
                run.append((x, y))
            else:
                if len(run) > 1:
                    paths.append(run)
                run = []
            t += sample_step
        if len(run) > 1:
            paths.append(run)
        offset += spacing_px
    return paths


def generate_hatch_paths(gray, levels, base_spacing_px, angle_start=18,
                         background_mask=None, dark_boost=1.0,
                         deepen_blacks=True):
    """Tone-mapped crosshatch.

    `levels` hatch directions are laid down. A pixel receives direction k
    (k = 0 is the first / lightest pass) only where its darkness is at
    least (k + 0.5)/levels of full black, so mid-tones get a couple of
    overlapping directions and shadows get all of them -- the overlap count
    tracks image darkness monotonically. Line spacing is constant within a
    pass, so the result is predictable as you change levels/spacing.
    """
    if levels <= 0:
        return []
    subject = _subject_mask(gray.shape, background_mask)

    dark = (255 - gray.astype(np.float32)) / 255.0
    if abs(dark_boost - 1.0) > 1e-3:
        dark = np.clip(dark * dark_boost, 0, 1)

    spacing = max(base_spacing_px, 1.2)
    paths = []
    for k in range(levels):
        thresh = (k + 0.5) / levels
        mask = (dark >= thresh) & subject
        if not mask.any():
            continue
        angle = angle_start + k * (180.0 / levels)
        paths.extend(hatch_lines_for_mask(mask, angle, spacing))

    if deepen_blacks:
        mask = (dark >= 0.82) & subject
        if mask.any():
            paths.extend(hatch_lines_for_mask(
                mask, angle_start + 90.0, max(spacing * 0.5, 0.9)))
    return paths


# --------------------------------------------------------------------------
# Shading pass -- tone-mapped dot stippling
# --------------------------------------------------------------------------

def generate_stipple_paths(gray, spacing_px, background_mask=None,
                           dark_boost=1.0, gamma=1.0, min_gap_px=1.4,
                           jitter=0.42, seed=1234):
    """Dot stippling. The tone image is reduced to a grid whose cell size is
    the *darkest-area* dot pitch; each cell's target ink coverage
    (0..1, from local darkness) is Floyd-Steinberg error-diffused to a
    clean on/off dot pattern, so local average tone is preserved without
    visible banding. Dots are jittered off the grid so they don't line up.

    Returns a list of single-point "paths" -- paths_to_gcode renders each
    as one pen tap.
    """
    subject = _subject_mask(gray.shape, background_mask)
    h, w = gray.shape
    cell = max(spacing_px, 1.5)
    gh = max(1, int(h / cell))
    gw = max(1, int(w / cell))

    small = cv2.resize(gray, (gw, gh), interpolation=cv2.INTER_AREA).astype(np.float32)
    subj_small = cv2.resize(subject.astype(np.uint8), (gw, gh),
                            interpolation=cv2.INTER_AREA) > 0.5

    dark = (255.0 - small) / 255.0
    if abs(gamma - 1.0) > 1e-3:
        dark = np.power(np.clip(dark, 0, 1), 1.0 / max(gamma, 1e-3))
    dark = np.clip(dark * dark_boost, 0.0, 1.0)
    dark[~subj_small] = 0.0

    # Floyd-Steinberg on the coverage field
    err = dark.copy()
    dots_grid = np.zeros((gh, gw), bool)
    for y in range(gh):
        for x in range(gw):
            old = err[y, x]
            new = 1.0 if old >= 0.5 else 0.0
            dots_grid[y, x] = new > 0.5
            d = old - new
            if x + 1 < gw:
                err[y, x + 1] += d * 7 / 16
            if y + 1 < gh:
                if x > 0:
                    err[y + 1, x - 1] += d * 3 / 16
                err[y + 1, x] += d * 5 / 16
                if x + 1 < gw:
                    err[y + 1, x + 1] += d * 1 / 16

    rng = np.random.default_rng(seed)
    hh, ww = background_mask.shape if background_mask is not None else (h, w)
    dots = []
    for gy in range(gh):
        cols = range(gw) if gy % 2 == 0 else range(gw - 1, -1, -1)
        for gx in cols:
            if not dots_grid[gy, gx]:
                continue
            px = (gx + 0.5 + rng.uniform(-jitter, jitter)) * cell
            py = (gy + 0.5 + rng.uniform(-jitter, jitter)) * cell
            xi, yi = int(round(px)), int(round(py))
            if 0 <= xi < ww and 0 <= yi < hh:
                if background_mask is not None and background_mask[yi, xi]:
                    continue
            dots.append([(px, py)])
    return dots


# --------------------------------------------------------------------------
# Path ordering (greedy nearest-neighbor)
# --------------------------------------------------------------------------

def _serpentine_sort(paths, band_mm_px):
    """O(n log n) ordering: sweep the bed in horizontal bands, alternating
    left-to-right / right-to-left each band. Near-optimal for the dense,
    roughly-uniform path sets a portrait produces, and it doesn't blow up
    the way the O(n^2) greedy pass does at tens of thousands of paths."""
    def key(p):
        xs = [pt[0] for pt in p]; ys = [pt[1] for pt in p]
        cx = sum(xs) / len(xs); cy = sum(ys) / len(ys)
        band = int(cy / band_mm_px)
        return (band, cx if band % 2 == 0 else -cx)
    out = sorted(paths, key=key)
    # flip each path so it starts nearest the previous path's end
    cur = None
    for i, p in enumerate(out):
        if cur is not None and len(p) > 1:
            d0 = (p[0][0] - cur[0]) ** 2 + (p[0][1] - cur[1]) ** 2
            d1 = (p[-1][0] - cur[0]) ** 2 + (p[-1][1] - cur[1]) ** 2
            if d1 < d0:
                out[i] = p = p[::-1]
        cur = p[-1]
    return out


def order_paths(paths, band_px=24.0):
    if not paths:
        return paths
    if len(paths) > 1500:
        return _serpentine_sort(paths, band_px)
    remaining = paths[:]
    ordered = [remaining.pop(0)]
    cur = ordered[0][-1]
    while remaining:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, p in enumerate(remaining):
            d_start = (p[0][0] - cur[0]) ** 2 + (p[0][1] - cur[1]) ** 2
            d_end = (p[-1][0] - cur[0]) ** 2 + (p[-1][1] - cur[1]) ** 2
            if d_start < best_d:
                best_d, best_i, best_rev = d_start, i, False
            if d_end < best_d:
                best_d, best_i, best_rev = d_end, i, True
        nxt = remaining.pop(best_i)
        if best_rev and len(nxt) > 1:
            nxt = nxt[::-1]
        ordered.append(nxt)
        cur = ordered[-1][-1]
    return ordered


# --------------------------------------------------------------------------
# Placement / fit
# --------------------------------------------------------------------------

def fit_drawing(img_w_px, img_h_px, req_width_mm, req_height_mm,
                paper_w_mm, paper_h_mm, paper_margin_mm,
                usable_w_mm, usable_h_mm):
    """Return (width_mm, height_mm) for the drawing: the requested size,
    shrunk if needed so it fits inside BOTH the sheet (minus margin) and
    the reachable bed area. Aspect ratio is preserved when req_height_mm is
    None; otherwise the explicit size is only scaled down uniformly."""
    aspect = img_h_px / img_w_px
    w = req_width_mm
    h = req_height_mm if req_height_mm else req_width_mm * aspect

    limit_w = min(usable_w_mm, max(paper_w_mm - 2 * paper_margin_mm, 1.0))
    limit_h = min(usable_h_mm, max(paper_h_mm - 2 * paper_margin_mm, 1.0))

    scale = min(1.0, limit_w / w, limit_h / h)
    return w * scale, h * scale


def place_in_usable(width_mm, height_mm, usable_x0, usable_y0,
                    usable_w_mm, usable_h_mm, center=True,
                    origin_x=None, origin_y=None):
    """Origin (drawing bottom-left in bed coords) either centered in the
    usable rectangle or at an explicit offset, clamped so the whole drawing
    stays inside the usable rectangle."""
    if center or origin_x is None or origin_y is None:
        ox = usable_x0 + (usable_w_mm - width_mm) / 2.0
        oy = usable_y0 + (usable_h_mm - height_mm) / 2.0
    else:
        ox, oy = origin_x, origin_y
    ox = min(max(ox, usable_x0), usable_x0 + max(usable_w_mm - width_mm, 0))
    oy = min(max(oy, usable_y0), usable_y0 + max(usable_h_mm - height_mm, 0))
    return ox, oy


def compute_centered_origin(bed_width_mm, bed_height_mm, width_mm, height_mm):
    """Back-compat helper: center a drawing on the full bed."""
    return (bed_width_mm - width_mm) / 2.0, (bed_height_mm - height_mm) / 2.0


def apply_clip_avoidance(x0, y0, w, h, clip_left=0.0, clip_right=0.0,
                         clip_front=0.0, clip_back=0.0):
    """Pull a usable-bed rectangle (x0, y0, w, h, in mm) further in on every
    side so the pen never runs into a clip holding the paper down. Directions
    are as you face the machine from the front:

        clip_back  -> the BACK edge of the bed  (Y+, "Top" in the UI)
        clip_front -> the FRONT edge of the bed (Y-, "Bottom" in the UI)
        clip_left  -> the LEFT edge  (X-)
        clip_right -> the RIGHT edge (X+)

    The values are subtracted from the rectangle that the ordinary unusable
    margins already produced, so Clip Avoidance stacks on top of them. Width
    and height are clamped to a 1 mm floor so a silly value can't invert the
    rectangle.
    """
    x0 = x0 + max(clip_left, 0.0)
    y0 = y0 + max(clip_front, 0.0)
    w = max(w - max(clip_left, 0.0) - max(clip_right, 0.0), 1.0)
    h = max(h - max(clip_front, 0.0) - max(clip_back, 0.0), 1.0)
    return x0, y0, w, h


# --------------------------------------------------------------------------
# Path cleanup
# --------------------------------------------------------------------------

def _clean_paths(paths, px_to_mm_x, px_to_mm_y, min_seg_mm=0.12,
                 min_path_mm=0.6):
    """Drop redundant points, degenerate segments, and multi-point paths
    too short to be worth a pen-down/up cycle. Single-point paths (stipple
    dots) always pass through."""
    out = []
    for p in paths:
        if len(p) == 1:
            out.append(p)
            continue
        pruned = [p[0]]
        for pt in p[1:]:
            dx = (pt[0] - pruned[-1][0]) * px_to_mm_x
            dy = (pt[1] - pruned[-1][1]) * px_to_mm_y
            if (dx * dx + dy * dy) ** 0.5 >= min_seg_mm:
                pruned.append(pt)
        if len(pruned) < 2:
            continue
        length = 0.0
        for i in range(len(pruned) - 1):
            dx = (pruned[i + 1][0] - pruned[i][0]) * px_to_mm_x
            dy = (pruned[i + 1][1] - pruned[i][1]) * px_to_mm_y
            length += (dx * dx + dy * dy) ** 0.5
        if length < min_path_mm:
            continue
        out.append(pruned)
    return out


# --------------------------------------------------------------------------
# G-code export
# --------------------------------------------------------------------------

def paths_to_gcode(paths, px_to_mm_x, px_to_mm_y, pen_up_z, pen_down_z,
                   draw_feed, travel_feed, origin_x=0.0, origin_y=0.0,
                   content_w_px=None, content_h_px=None,
                   flip_y=True, mirror_x=False, fan_off=True,
                   dot_dwell_ms=0, z_feed=None, home_xy=True,
                   min_seg_mm=0.12, min_path_mm=0.6, settings_comment=None):
    """Emit G-code. Pen up/down are Z moves (no servo, no heater commands).

    flip_y   -- map image-top to the BACK of the bed so the finished print
                reads the same way up as the on-screen preview / the source
                image (Ender-3 Y grows toward the back).
    mirror_x -- also mirror left<->right (for a 180 deg / mirrored machine).
    fan_off  -- emit M107 so the part-cooling fan on the head stays off.
    settings_comment -- optional JSON-serialisable dict of the settings that
                produced this file; written as a single
                "; PPS-SETTINGS: {...}" header line so PenPlotter Studio can
                restore every control when the file is re-imported.
    """
    if content_h_px is None or content_w_px is None:
        allx = [pt[0] for p in paths for pt in p] or [0.0]
        ally = [pt[1] for p in paths for pt in p] or [0.0]
        content_w_px = content_w_px or (max(allx) + 1)
        content_h_px = content_h_px or (max(ally) + 1)

    z_feed = z_feed or travel_feed
    paths = _clean_paths(paths, px_to_mm_x, px_to_mm_y, min_seg_mm, min_path_mm)

    def xf(px, py):
        x = px
        y = py
        if mirror_x:
            x = content_w_px - x
        if flip_y:
            y = content_h_px - y
        return origin_x + x * px_to_mm_x, origin_y + y * px_to_mm_y

    L = []
    L.append("; Pen plotter G-code -- generated by PenPlotter Studio")
    L.append("; Z moves for pen up/down (no servo, no heaters)")
    if settings_comment:
        try:
            L.append("; PPS-SETTINGS: "
                     + json.dumps(settings_comment, sort_keys=True))
        except (TypeError, ValueError):
            pass
    L.append("G21 ; mm")
    L.append("G90 ; absolute")
    if fan_off:
        L.append("M107 ; part-cooling fan off (unused pen-plotter head)")
    L.append(f"G1 Z{pen_up_z:.2f} F{z_feed} ; pen up")
    if home_xy:
        L.append("G28 X Y ; home X/Y (comment out if already homed)")

    # pen state is tracked so we never emit a redundant Z move and never
    # drag the pen across a travel move
    pen_up = True
    last = None

    def lift():
        nonlocal pen_up
        if not pen_up:
            L.append(f"G1 Z{pen_up_z:.2f} F{z_feed}")
            pen_up = True

    def drop():
        nonlocal pen_up
        if pen_up:
            L.append(f"G1 Z{pen_down_z:.2f} F{z_feed}")
            pen_up = False

    for path in paths:
        if len(path) == 1:
            x0, y0 = xf(*path[0])
            lift()
            L.append(f"G1 X{x0:.3f} Y{y0:.3f} F{travel_feed}")
            drop()
            if dot_dwell_ms > 0:
                L.append(f"G4 P{int(dot_dwell_ms)}")
            lift()
            last = (x0, y0)
            continue

        x0, y0 = xf(*path[0])
        # skip a pointless re-draw if we're already sitting on the start
        if last is None or (abs(last[0] - x0) > 1e-4 or abs(last[1] - y0) > 1e-4):
            lift()
            L.append(f"G1 X{x0:.3f} Y{y0:.3f} F{travel_feed}")
        drop()
        x, y = x0, y0
        for pt in path[1:]:
            x, y = xf(*pt)
            L.append(f"G1 X{x:.3f} Y{y:.3f} F{draw_feed}")
        last = (x, y)

    lift()
    L.append(f"G1 X0 Y0 F{travel_feed} ; park")
    L.append("; end")
    return "\n".join(L)


# --------------------------------------------------------------------------
# One-call convenience wrapper (used by GUI worker and CLI)
# --------------------------------------------------------------------------

def build_paths(gray, alpha_mask, params):
    """Run the enabled passes and return (paths, background_mask)."""
    background_mask = None
    if params.get("bg_detect_enabled", True):
        background_mask = detect_background_mask(
            gray, params.get("bg_tolerance", 18), alpha_mask)
    elif alpha_mask is not None:
        background_mask = ~alpha_mask

    paths = []
    if params.get("outline_enabled", True):
        paths += generate_outline_paths(
            gray, params["canny_low"], params["canny_high"],
            background_mask=background_mask)

    style = params.get("shading_style", "crosshatch")
    if params.get("shading_enabled", True):
        px_x = params["_px_x"]; px_y = params["_px_y"]
        avg = (px_x + px_y) / 2.0
        if style == "stipple":
            spacing_px = params["dot_spacing_mm"] / avg
            paths += generate_stipple_paths(
                gray, spacing_px, background_mask=background_mask,
                dark_boost=params.get("dark_boost", 1.0),
                gamma=params.get("dot_gamma", 1.0))
        elif params.get("shading_levels", 0) > 0:
            spacing_px = params["hatch_spacing_mm"] / avg
            paths += generate_hatch_paths(
                gray, params["shading_levels"], spacing_px,
                background_mask=background_mask,
                dark_boost=params.get("dark_boost", 1.0))

    if paths:
        paths = order_paths(paths)
    return paths, background_mask


# --------------------------------------------------------------------------
# G-code import -- read a file back into a toolpath + the settings that made it
# --------------------------------------------------------------------------

_GCODE_NUM = r"(-?\d+(?:\.\d+)?)"


def _axis(code, letter, default):
    m = re.search(letter + _GCODE_NUM, code)
    return (float(m.group(1)), True) if m else (default, False)


def parse_gcode(text):
    """Read a G-code file back into what the printer would actually draw.

    Returns (paths, settings):
      * paths    -- list of pen-down polylines in bed millimetre coordinates,
                    exactly as the machine would move. A single-point path is
                    a dot tap, matching the rest of this module.
      * settings -- dict of recovered PenPlotter Studio control values. If the
                    file carries a "; PPS-SETTINGS: {...}" header (every file
                    this app exports does) that is used verbatim; otherwise
                    the pen up/down Z planes, draw/travel feeds, M106/M107 fan
                    state and G28 homing are inferred from the motion itself,
                    so hand-written or third-party files still import.

    This does NOT re-run the image conversion -- it replays the moves in the
    file, so the preview is the real result of that exact G-code.
    """
    lines = text.splitlines()

    embedded = {}
    for raw in lines:
        s = raw.strip()
        if s.startswith("; PPS-SETTINGS:"):
            try:
                embedded = json.loads(s.split(":", 1)[1].strip())
            except (ValueError, TypeError):
                embedded = {}
            break

    def _moves():
        for raw in lines:
            code = raw.split(";", 1)[0].strip()
            if not code:
                continue
            head = code.split()[0].upper()
            yield head, code

    # 1. find the pen-up / pen-down Z planes from every Z we ever see
    zs = []
    for head, code in _moves():
        if head in ("G0", "G1", "G00", "G01"):
            m = re.search(r"[Zz]" + _GCODE_NUM, code)
            if m:
                zs.append(float(m.group(1)))
    uniq = sorted(set(zs))
    if len(uniq) >= 2:
        down_z, up_z = uniq[0], uniq[-1]
        down_thresh = (uniq[0] + uniq[1]) / 2.0
    elif uniq:
        down_z = up_z = uniq[0]
        down_thresh = uniq[0] + 0.5
    else:
        down_z, up_z, down_thresh = 0.0, 2.0, 1.0

    # 2. walk the moves: track pen state, collect polylines + feed rates
    x = y = 0.0
    z = up_z
    pen_down = False
    cur = None
    paths = []
    draw_feeds, travel_feeds = [], []
    has_m106 = has_m107 = has_g28 = False

    for head, code in _moves():
        if head == "M106":
            has_m106 = True
            continue
        if head == "M107":
            has_m107 = True
            continue
        if head == "G28":
            has_g28 = True
            continue
        if head not in ("G0", "G1", "G00", "G01"):
            continue

        nx, has_x = _axis(code, "[Xx]", x)
        ny, has_y = _axis(code, "[Yy]", y)
        nz, has_z = _axis(code, "[Zz]", z)
        mf = re.search(r"[Ff]" + _GCODE_NUM, code)
        moved_xy = has_x or has_y
        now_down = nz <= down_thresh

        if mf and moved_xy:
            feed = float(mf.group(1))
            (draw_feeds if (pen_down and now_down) else travel_feeds).append(feed)

        if has_z and not moved_xy:                 # pure Z -> pen transition
            if now_down and not pen_down:
                cur = [(x, y)]
                pen_down = True
            elif not now_down and pen_down:
                if cur and len(cur) >= 1:
                    paths.append(cur)
                cur = None
                pen_down = False
            z = nz
            continue

        if pen_down and now_down:
            if cur is None:
                cur = [(x, y)]
            cur.append((nx, ny))
        x, y, z = nx, ny, nz

    if cur and len(cur) >= 1:
        paths.append(cur)

    # 3. assemble the recovered settings (embedded header wins over inferred)
    def _mode(vals):
        return max(set(vals), key=vals.count) if vals else None

    inferred = {"pen_down_z": down_z, "pen_up_z": up_z}
    df, tf = _mode(draw_feeds), _mode(travel_feeds)
    if df:
        inferred["draw_feed"] = int(round(df))
    if tf:
        inferred["travel_feed"] = int(round(tf))
    if has_m106 or has_m107:
        inferred["fan_off"] = has_m107 and not has_m106
    if has_g28:
        inferred["home_xy"] = True

    settings = dict(inferred)
    if isinstance(embedded, dict):
        settings.update({k: v for k, v in embedded.items()
                         if not str(k).startswith("_")})
    return paths, settings


def render_gcode_bed_preview(paths, bed_w_mm, bed_h_mm, px_per_mm=4,
                             line_color=(0, 0, 0), bg=255):
    """Draw a parsed toolpath (bed-mm coordinates, from parse_gcode) onto a
    white bed image with a 10 mm grid, +Y pointing up, and return it as a
    BGR uint8 array. Single-point paths render as dots."""
    W = max(int(round(bed_w_mm * px_per_mm)), 1)
    H = max(int(round(bed_h_mm * px_per_mm)), 1)
    img = np.full((H, W, 3), bg, np.uint8)

    for mm in range(0, int(round(bed_w_mm)) + 1, 10):
        gx = int(round(mm * px_per_mm))
        cv2.line(img, (gx, 0), (gx, H), (235, 235, 235), 1)
    for mm in range(0, int(round(bed_h_mm)) + 1, 10):
        gy = int(round(H - mm * px_per_mm))
        cv2.line(img, (0, gy), (W, gy), (235, 235, 235), 1)

    def to_px(p):
        return (int(round(p[0] * px_per_mm)), int(round(H - p[1] * px_per_mm)))

    for path in paths:
        if len(path) == 1:
            cv2.circle(img, to_px(path[0]), 1, line_color, -1, cv2.LINE_AA)
        elif len(path) > 1:
            pts = np.array([to_px(p) for p in path], np.int32)
            cv2.polylines(img, [pts], False, line_color, 1, cv2.LINE_AA)
    return img
