#!/usr/bin/env python3
"""Core image-to-G-code pipeline for PenPlotter Studio."""

import argparse
import math
import sys

import cv2
import numpy as np

DEFAULT_PEN_UP_Z = 3.0
DEFAULT_BED_WIDTH_MM = 220.0
DEFAULT_BED_HEIGHT_MM = 220.0
DEFAULT_UNUSABLE_RIGHT_MM = 10.0


def load_and_prepare(image_path, width_mm, height_mm, max_px=900):
    """Load, normalize, lightly denoise, and resize an image for plotting."""
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")

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
    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    # Robust contrast normalization makes photos/scans much less sensitive to
    # exposure and paper/background brightness than raw Canny input.
    lo, hi = np.percentile(img, (1.0, 99.0))
    if hi > lo + 1:
        img = np.clip((img.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)

    img = cv2.GaussianBlur(img, (3, 3), 0)
    px_to_mm_x = width_mm / new_w
    px_to_mm_y = height_mm / new_h
    return img, px_to_mm_x, px_to_mm_y, width_mm, height_mm


def detect_background_mask(gray, tolerance=18):
    """Return border-connected, low-variation background pixels."""
    h, w = gray.shape
    flood_mask = np.zeros((h + 2, w + 2), np.uint8)
    work = gray.copy()
    step_x = max(1, w // 60)
    step_y = max(1, h // 60)
    border_points = set()
    for x in range(0, w, step_x):
        border_points.add((x, 0)); border_points.add((x, h - 1))
    for y in range(0, h, step_y):
        border_points.add((0, y)); border_points.add((w - 1, y))

    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
    for x, y in border_points:
        if flood_mask[y + 1, x + 1] == 0:
            cv2.floodFill(work, flood_mask, (x, y), 0,
                          loDiff=tolerance, upDiff=tolerance, flags=flags)
    return flood_mask[1:-1, 1:-1] > 0


def _effective_canny_thresholds(gray, low, high):
    """Convert UI/CLI thresholds into stable Canny values.

    The source image is contrast-normalized first. Values 0..500 are accepted
    for backwards compatibility. Zero is treated as 'most sensitive', but is
    still given a small noise floor so the setting does not turn the entire
    image into an edge map.
    """
    low = float(np.clip(low, 0, 500))
    high = float(np.clip(high, 0, 500))
    if low > high:
        low, high = high, low

    # Map the legacy 0..500 controls to useful Canny thresholds. Keep a floor
    # and ensure a real hysteresis gap at the extremes.
    effective_low = 3.0 + (low / 500.0) * 117.0
    effective_high = 12.0 + (high / 500.0) * 183.0
    if effective_high <= effective_low + 4:
        effective_high = effective_low + 4
    return int(round(effective_low)), int(round(min(255, effective_high)))


def _path_length_px(path):
    if len(path) < 2:
        return 0.0
    return sum(math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
               for i in range(len(path) - 1))


def simplify_and_filter_paths(paths, epsilon=1.0, min_length_px=5.0):
    """Remove tiny/no-op paths and redundant points without changing shape."""
    result = []
    for path in paths:
        if len(path) < 2 or _path_length_px(path) < min_length_px:
            continue
        arr = np.asarray(path, dtype=np.float32).reshape(-1, 1, 2)
        if len(arr) > 2:
            approx = cv2.approxPolyDP(arr, epsilon, False)
            pts = [(float(p[0][0]), float(p[0][1])) for p in approx]
        else:
            pts = [(float(x), float(y)) for x, y in path]
        if len(pts) >= 2 and _path_length_px(pts) >= min_length_px:
            result.append(pts)
    return result


def generate_outline_paths(gray, low, high, min_len=6, epsilon=1.2, background_mask=None):
    """Trace meaningful image edges while suppressing isolated noise."""
    canny_low, canny_high = _effective_canny_thresholds(gray, low, high)
    # A small blur plus Canny's hysteresis gives much cleaner outlines than
    # dilating every edge pixel, which used to thicken/noisify contours.
    edges = cv2.Canny(gray, canny_low, canny_high, apertureSize=3, L2gradient=True)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)

    paths = []
    h, w = gray.shape
    for c in contours:
        if len(c) < min_len:
            continue
        pts = [(float(p[0][0]), float(p[0][1])) for p in c]
        if background_mask is not None and pts:
            in_bg = 0
            for px, py in pts[::max(1, len(pts) // 80)]:
                xi, yi = int(round(px)), int(round(py))
                if 0 <= xi < w and 0 <= yi < h and background_mask[yi, xi]:
                    in_bg += 1
            samples = max(1, len(pts[::max(1, len(pts) // 80)]))
            if in_bg / samples > 0.80:
                continue
        paths.append(pts)

    return simplify_and_filter_paths(paths, epsilon=epsilon, min_length_px=float(min_len))


def hatch_lines_for_mask(mask, angle_deg, spacing_px, sample_step=1.0):
    """Return continuous line segments inside a boolean mask."""
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
    while offset <= max_off + 1e-6:
        base_x, base_y = nx * offset, ny * offset
        t = min_along
        run = []
        while t <= max_along + 1e-6:
            x, y = base_x + dx * t, base_y + dy * t
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
        offset += max(0.5, spacing_px)
    return paths


def generate_hatch_paths(gray, levels, base_spacing_px, angle_start=20, background_mask=None):
    """Generate layered crosshatching with continuous segments only."""
    if levels <= 0:
        return []
    thresholds = [int(255 * (levels - i) / (levels + 1)) for i in range(levels)]
    paths = []
    for i, thresh in enumerate(thresholds):
        mask = gray < thresh
        if background_mask is not None:
            mask &= ~background_mask
        angle = angle_start + i * (180.0 / (levels + 1))
        spacing = base_spacing_px * (levels - i) / levels * 1.3 + base_spacing_px * 0.4
        paths.extend(hatch_lines_for_mask(mask, angle, max(spacing, 1.5)))
    return simplify_and_filter_paths(paths, epsilon=0.8, min_length_px=2.0)


def generate_dot_infill_paths(gray, spacing_px, darkness_threshold=0.08,
                              background_mask=None, jitter=False):
    """Generate one-point paths for halftone-style dot shading.

    Dot density follows darkness: white/near-white pixels create no dots;
    darker regions get more regular-grid positions. One-point paths are kept
    distinct so the G-code writer can make an actual pen mark without a
    meaningless travel line.
    """
    spacing_px = max(float(spacing_px), 2.0)
    h, w = gray.shape
    # Use a cell-average rather than one noisy pixel. This makes the dot field
    # much more stable on photographs and scanned paper.
    radius = max(1, int(round(spacing_px * 0.35)))
    smooth = cv2.GaussianBlur(gray, (radius * 2 + 1, radius * 2 + 1), 0)
    paths = []
    row = 0
    y = spacing_px * 0.5
    while y < h:
        x = spacing_px * 0.5 + (spacing_px * 0.5 if row % 2 else 0.0)
        while x < w:
            xi, yi = int(round(x)), int(round(y))
            if 0 <= xi < w and 0 <= yi < h:
                if background_mask is not None and background_mask[yi, xi]:
                    x += spacing_px
                    continue
                darkness = 1.0 - smooth[yi, xi] / 255.0
                # The expected dot probability is proportional to darkness.
                # At very low darkness, skip entirely to avoid gray-noise ink.
                if darkness >= darkness_threshold:
                    probability = (darkness - darkness_threshold) / max(1e-6, 1.0 - darkness_threshold)
                    # Deterministic threshold pattern; no RNG means previews and
                    # exported G-code always agree.
                    if ((xi * 73856093 + yi * 19349663) % 100000) / 100000.0 < probability:
                        paths.append([(float(x), float(y))])
            x += spacing_px
        y += spacing_px * math.sqrt(3) / 2
        row += 1
    return paths


def order_paths(paths):
    """Greedy nearest-neighbor ordering; supports both lines and dots."""
    if not paths:
        return []
    remaining = list(paths)
    ordered = [remaining.pop(0)]
    cur = ordered[0][-1]
    while remaining:
        best_i, best_d, best_rev = 0, float("inf"), False
        for i, p in enumerate(remaining):
            d_start = (p[0][0] - cur[0]) ** 2 + (p[0][1] - cur[1]) ** 2
            d_end = (p[-1][0] - cur[0]) ** 2 + (p[-1][1] - cur[1]) ** 2
            if d_start < best_d:
                best_d, best_i, best_rev = d_start, i, False
            if d_end < best_d and len(p) > 1:
                best_d, best_i, best_rev = d_end, i, True
        nxt = remaining.pop(best_i)
        if best_rev:
            nxt = nxt[::-1]
        ordered.append(nxt)
        cur = ordered[-1][-1]
    return ordered


def compute_centered_origin(bed_width_mm, bed_height_mm, width_mm, height_mm,
                            unusable_right_mm=DEFAULT_UNUSABLE_RIGHT_MM):
    """Center within the usable area, reserving unusable_right_mm on the right."""
    usable_width = bed_width_mm - max(0.0, unusable_right_mm)
    origin_x = (usable_width - width_mm) / 2.0
    origin_y = (bed_height_mm - height_mm) / 2.0
    return origin_x, origin_y


def transform_paths_for_front_view(paths, image_width_px, image_height_px,
                                   flip_x=True, flip_y=True):
    """Transform image coordinates so the front-view result is not inverted."""
    out = []
    for path in paths:
        transformed = []
        for x, y in path:
            tx = image_width_px - 1 - x if flip_x else x
            ty = image_height_px - 1 - y if flip_y else y
            transformed.append((tx, ty))
        out.append(transformed)
    return out


def _format_xy(origin_x, origin_y, px, py, px_to_mm_x, px_to_mm_y):
    return origin_x + px * px_to_mm_x, origin_y + py * px_to_mm_y


def paths_to_gcode(paths, px_to_mm_x, px_to_mm_y, pen_up_z, pen_down_z,
                    draw_feed, travel_feed, origin_x=0.0, origin_y=0.0,
                    fan_off=True, park_x=0.0, park_y=0.0):
    lines = [
        "; Pen plotter G-code -- generated by PenPlotter Studio",
        "; Uses Z moves for pen up/down; no extrusion or heater commands",
        "G21 ; mm units",
        "G90 ; absolute positioning",
        "M107 ; part-cooling fan OFF",
        f"G1 Z{pen_up_z:.2f} F{travel_feed} ; pen up",
        "G28 X Y ; home X and Y",
    ]

    for path in paths:
        if not path:
            continue
        x0, y0 = _format_xy(origin_x, origin_y, path[0][0], path[0][1], px_to_mm_x, px_to_mm_y)
        lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed}")
        lines.append(f"G1 X{x0:.2f} Y{y0:.2f} F{travel_feed}")
        lines.append(f"G1 Z{pen_down_z:.2f} F{travel_feed}")
        if len(path) == 1:
            # A one-point path is a real dot: lower at the target and raise
            # immediately. There is no fake second XY move.
            lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed}")
            continue
        for px, py in path[1:]:
            x, y = _format_xy(origin_x, origin_y, px, py, px_to_mm_x, px_to_mm_y)
            lines.append(f"G1 X{x:.2f} Y{y:.2f} F{draw_feed}")

    lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed} ; pen up")
    if fan_off:
        lines.append("M107 ; ensure part-cooling fan remains OFF")
    lines.append(f"G1 X{park_x:.2f} Y{park_y:.2f} F{travel_feed}")
    lines.append("; end of drawing")
    return "\n".join(lines)


def build_paths_for_image(gray, params):
    """Shared pipeline used by GUI and CLI."""
    background_mask = None
    if params.get("bg_detect_enabled", True):
        background_mask = detect_background_mask(gray, params.get("bg_tolerance", 18))

    paths = []
    if params.get("outline_enabled", True):
        paths.extend(generate_outline_paths(
            gray, params.get("canny_low", 50), params.get("canny_high", 150),
            background_mask=background_mask))

    method = params.get("shading_method", "crosshatch")
    if params.get("shading_enabled", True):
        spacing_px = params.get("shading_spacing_mm", params.get("hatch_spacing_mm", 1.4)) / ((params["px_x"] + params["px_y"]) / 2)
        if method == "infill":
            paths.extend(generate_dot_infill_paths(
                gray, spacing_px,
                darkness_threshold=params.get("dot_threshold", 0.08),
                background_mask=background_mask))
        elif params.get("shading_levels", 3) > 0:
            paths.extend(generate_hatch_paths(
                gray, params["shading_levels"], spacing_px,
                background_mask=background_mask))

    paths = order_paths(paths)
    if params.get("front_view_correction", True):
        paths = transform_paths_for_front_view(paths, gray.shape[1], gray.shape[0], True, True)
    return paths


def main():
    ap = argparse.ArgumentParser(description="Convert an image into pen-plotter G-code.")
    ap.add_argument("image")
    ap.add_argument("output")
    ap.add_argument("--width-mm", type=float, default=150.0)
    ap.add_argument("--height-mm", type=float, default=None)
    ap.add_argument("--pen-up-z", type=float, default=DEFAULT_PEN_UP_Z)
    ap.add_argument("--pen-down-z", type=float, default=0.0)
    ap.add_argument("--draw-feed", type=int, default=1500)
    ap.add_argument("--travel-feed", type=int, default=3000)
    ap.add_argument("--canny-low", type=int, default=50)
    ap.add_argument("--canny-high", type=int, default=150)
    ap.add_argument("--hatch-spacing-mm", type=float, default=1.4)
    ap.add_argument("--shading-levels", type=int, default=3)
    ap.add_argument("--shading-method", choices=["crosshatch", "infill"], default="crosshatch")
    ap.add_argument("--dot-spacing-mm", type=float, default=1.8)
    ap.add_argument("--dot-threshold", type=float, default=0.08)
    ap.add_argument("--no-outline", action="store_true")
    ap.add_argument("--no-bg-detect", action="store_true")
    ap.add_argument("--bg-tolerance", type=int, default=18)
    ap.add_argument("--bed-width-mm", type=float, default=DEFAULT_BED_WIDTH_MM)
    ap.add_argument("--bed-height-mm", type=float, default=DEFAULT_BED_HEIGHT_MM)
    ap.add_argument("--unusable-right-mm", type=float, default=DEFAULT_UNUSABLE_RIGHT_MM)
    ap.add_argument("--no-center", action="store_true")
    ap.add_argument("--origin-x", type=float, default=0.0)
    ap.add_argument("--origin-y", type=float, default=0.0)
    ap.add_argument("--no-front-view-correction", action="store_true")
    args = ap.parse_args()

    gray, px_x, px_y, w_mm, h_mm = load_and_prepare(args.image, args.width_mm, args.height_mm)
    params = {
        "outline_enabled": not args.no_outline,
        "shading_enabled": True,
        "bg_detect_enabled": not args.no_bg_detect,
        "bg_tolerance": args.bg_tolerance,
        "canny_low": args.canny_low,
        "canny_high": args.canny_high,
        "shading_levels": args.shading_levels,
        "shading_method": args.shading_method,
        "shading_spacing_mm": args.dot_spacing_mm if args.shading_method == "infill" else args.hatch_spacing_mm,
        "dot_threshold": args.dot_threshold,
        "px_x": px_x, "px_y": px_y,
        "front_view_correction": not args.no_front_view_correction,
    }
    all_paths = build_paths_for_image(gray, params)
    if not all_paths:
        print("No paths generated -- try adjusting the image or sensitivity.", file=sys.stderr)
        sys.exit(1)

    if args.no_center:
        origin_x, origin_y = args.origin_x, args.origin_y
    else:
        origin_x, origin_y = compute_centered_origin(
            args.bed_width_mm, args.bed_height_mm, w_mm, h_mm, args.unusable_right_mm)

    gcode = paths_to_gcode(all_paths, px_x, px_y, args.pen_up_z, args.pen_down_z,
                           args.draw_feed, args.travel_feed, origin_x, origin_y)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(gcode)

    total_pts = sum(len(p) for p in all_paths)
    print(f"Wrote {args.output}: {len(all_paths)} paths, {total_pts} points")
    print(f"Drawing area: {w_mm:.1f}mm x {h_mm:.1f}mm, usable bed width: {args.bed_width_mm - args.unusable_right_mm:.1f}mm")


if __name__ == "__main__":
    main()
