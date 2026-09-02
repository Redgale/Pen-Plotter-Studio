#!/usr/bin/env python3
"""
image_to_gcode.py

Converts a raster image into G-code for a pen-plotter conversion of an
Ender 3 V2 that uses the stock Z axis (gantry) to lift/lower the pen
instead of a servo.

Pipeline:
  1. Load image, resize to fit the requested drawing area.
  2. Outline pass: Canny edge detection -> contours -> simplified line paths.
  3. Shading pass: multi-angle crosshatching, density/angle-count driven by
     local brightness (darker regions get more hatch directions and tighter
     line spacing).
  4. Greedy nearest-neighbor path ordering to cut down on pen-up travel.
  5. G-code export using Z-height moves for pen up/down (no M280/servo
     commands, no heater commands -> works on stock, unmodified Marlin).

Usage:
  python3 image_to_gcode.py input.jpg output.gcode --width-mm 150 \
      --pen-up-z 5 --pen-down-z 0

Run with --help for the full list of options.
"""

import argparse
import math
import sys

import cv2
import numpy as np


# --------------------------------------------------------------------------
# Image loading / preparation
# --------------------------------------------------------------------------

def load_and_prepare(image_path, width_mm, height_mm, max_px=900):
    """Load image, convert to grayscale, resize so the longer edge is
    max_px pixels (keeps processing fast), and compute the px-per-mm
    scale factors for the requested output size."""
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {image_path}")

    h, w = img.shape
    aspect = h / w

    if height_mm is None:
        height_mm = width_mm * aspect

    # resize for processing speed, keep aspect ratio
    if w >= h:
        new_w = max_px
        new_h = int(max_px * aspect)
    else:
        new_h = max_px
        new_w = int(max_px / aspect)
    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    # mild blur to reduce noise before edge/hatch work
    img = cv2.GaussianBlur(img, (3, 3), 0)

    px_to_mm_x = width_mm / new_w
    px_to_mm_y = height_mm / new_h

    return img, px_to_mm_x, px_to_mm_y, width_mm, height_mm


# --------------------------------------------------------------------------
# Background detection
# --------------------------------------------------------------------------

def detect_background_mask(gray, tolerance=18):
    """Detect the "background" of the image as the region(s) of roughly
    uniform brightness that are connected to the image border. Flood-fills
    inward from sample points along the border with the given brightness
    tolerance, so a plain/near-uniform backdrop (paper, wall, solid color)
    gets excluded from shading regardless of its own brightness -- only
    border-connected, low-variation areas count as background, so a subject
    that merely happens to be light or dark is not treated as background.

    Returns a boolean mask, True where a pixel is considered background.
    """
    h, w = gray.shape
    flood_mask = np.zeros((h + 2, w + 2), np.uint8)
    work = gray.copy()

    step_x = max(1, w // 60)
    step_y = max(1, h // 60)
    border_points = set()
    for x in range(0, w, step_x):
        border_points.add((x, 0))
        border_points.add((x, h - 1))
    for y in range(0, h, step_y):
        border_points.add((0, y))
        border_points.add((w - 1, y))

    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8)
    for (x, y) in border_points:
        if flood_mask[y + 1, x + 1] == 0:
            cv2.floodFill(work, flood_mask, (x, y), 0,
                           loDiff=tolerance, upDiff=tolerance, flags=flags)

    return flood_mask[1:-1, 1:-1] > 0


# --------------------------------------------------------------------------
# Outline pass
# --------------------------------------------------------------------------

def generate_outline_paths(gray, low, high, min_len=6, epsilon=1.2, background_mask=None):
    edges = cv2.Canny(gray, low, high)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    paths = []
    for c in contours:
        if len(c) < min_len:
            continue
        approx = cv2.approxPolyDP(c, epsilon, closed=False)
        pts = [(float(p[0][0]), float(p[0][1])) for p in approx]
        if len(pts) < 2:
            continue
        if background_mask is not None:
            h, w = background_mask.shape
            in_bg = 0
            for (px, py) in pts:
                xi, yi = int(round(px)), int(round(py))
                if 0 <= xi < w and 0 <= yi < h and background_mask[yi, xi]:
                    in_bg += 1
            if in_bg / len(pts) > 0.85:
                continue
        paths.append(pts)
    return paths


# --------------------------------------------------------------------------
# Shading pass (multi-angle crosshatch)
# --------------------------------------------------------------------------

def hatch_lines_for_mask(mask, angle_deg, spacing_px, sample_step=1.0):
    """Return line segments (as point lists) covering the True regions of
    `mask`, using parallel lines at angle_deg spaced spacing_px apart."""
    h, w = mask.shape
    theta = math.radians(angle_deg)
    dx, dy = math.cos(theta), math.sin(theta)      # direction along a line
    nx, ny = -math.sin(theta), math.cos(theta)     # direction between lines

    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    offsets = [nx * x + ny * y for x, y in corners]
    alongs = [dx * x + dy * y for x, y in corners]
    min_off, max_off = min(offsets), max(offsets)
    min_along, max_along = min(alongs), max(alongs)

    paths = []
    offset = min_off
    while offset <= max_off:
        base_x = nx * offset
        base_y = ny * offset
        t = min_along
        run = []
        while t <= max_along:
            x = base_x + dx * t
            y = base_y + dy * t
            xi, yi = int(round(x)), int(round(y))
            inside = 0 <= xi < w and 0 <= yi < h
            val = mask[yi, xi] if inside else False
            if val:
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


def generate_hatch_paths(gray, levels, base_spacing_px, angle_start=20, background_mask=None):
    """Layered crosshatch: darker brightness bands get an extra hatch
    direction added on top (so the darkest areas end up with the most
    overlapping line directions = visually darkest). Pixels covered by
    `background_mask` are excluded so a uniform backdrop never gets hatched."""
    h, w = gray.shape
    # brightness thresholds splitting 0..255 into `levels` bands, darkest first
    thresholds = [int(255 * (i + 1) / (levels + 1)) for i in range(levels)]
    thresholds.reverse()  # e.g. levels=3 -> [191, 127, 63] roughly, adjust below
    thresholds = [int(255 * (levels - i) / (levels + 1)) for i in range(levels)]

    paths = []
    for i, thresh in enumerate(thresholds):
        mask = gray < thresh
        if background_mask is not None:
            mask = mask & ~background_mask
        angle = angle_start + i * (180.0 / (levels + 1))
        spacing = base_spacing_px * (levels - i) / levels * 1.3 + base_spacing_px * 0.4
        level_paths = hatch_lines_for_mask(mask, angle, max(spacing, 1.5))
        paths.extend(level_paths)
    return paths


# --------------------------------------------------------------------------
# Path ordering (greedy nearest-neighbor, cuts down pen-up travel)
# --------------------------------------------------------------------------

def order_paths(paths):
    if not paths:
        return paths
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
        if best_rev:
            nxt = nxt[::-1]
        ordered.append(nxt)
        cur = ordered[-1][-1]
    return ordered


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------

def compute_centered_origin(bed_width_mm, bed_height_mm, width_mm, height_mm):
    """Origin (bottom-left of the drawing) that centers a width_mm x height_mm
    drawing on a bed_width_mm x bed_height_mm plate (which is where a sheet of
    paper centered on the plate would be)."""
    origin_x = (bed_width_mm - width_mm) / 2.0
    origin_y = (bed_height_mm - height_mm) / 2.0
    return origin_x, origin_y


# --------------------------------------------------------------------------
# G-code export
# --------------------------------------------------------------------------

def paths_to_gcode(paths, px_to_mm_x, px_to_mm_y, pen_up_z, pen_down_z,
                    draw_feed, travel_feed, origin_x=0.0, origin_y=0.0):
    lines = []
    lines.append("; Pen plotter G-code -- generated by image_to_gcode.py")
    lines.append("; Uses Z moves for pen up/down (no servo, no heaters)")
    lines.append("G21 ; mm units")
    lines.append("G90 ; absolute positioning")
    lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed}  ; pen up")
    lines.append("G28 X Y ; home X and Y (comment out if already homed)")

    for path in paths:
        if len(path) < 2:
            continue
        x0 = origin_x + path[0][0] * px_to_mm_x
        y0 = origin_y + path[0][1] * px_to_mm_y
        lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed}")
        lines.append(f"G1 X{x0:.2f} Y{y0:.2f} F{travel_feed}")
        lines.append(f"G1 Z{pen_down_z:.2f} F{travel_feed}")
        for (px, py) in path[1:]:
            x = origin_x + px * px_to_mm_x
            y = origin_y + py * px_to_mm_y
            lines.append(f"G1 X{x:.2f} Y{y:.2f} F{draw_feed}")

    lines.append(f"G1 Z{pen_up_z:.2f} F{travel_feed}  ; pen up")
    lines.append("G1 X0 Y0 F{}".format(travel_feed))
    lines.append("; end of drawing")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Convert an image into pen-plotter G-code (Z-axis pen lift).")
    ap.add_argument("image", help="input image path (jpg/png/etc)")
    ap.add_argument("output", help="output .gcode path")
    ap.add_argument("--width-mm", type=float, default=150.0, help="drawing width in mm (default 150)")
    ap.add_argument("--height-mm", type=float, default=None, help="drawing height in mm (default: auto from image aspect ratio)")
    ap.add_argument("--pen-up-z", type=float, default=5.0, help="Z height, pen lifted off paper (mm)")
    ap.add_argument("--pen-down-z", type=float, default=0.0, help="Z height, pen touching paper (mm) -- calibrate this on your machine")
    ap.add_argument("--draw-feed", type=int, default=1500, help="feedrate while drawing (mm/min)")
    ap.add_argument("--travel-feed", type=int, default=3000, help="feedrate for pen-up travel / Z moves (mm/min)")
    ap.add_argument("--canny-low", type=int, default=50, help="Canny edge detector low threshold")
    ap.add_argument("--canny-high", type=int, default=150, help="Canny edge detector high threshold")
    ap.add_argument("--hatch-spacing-mm", type=float, default=1.4, help="base spacing between hatch lines, in mm")
    ap.add_argument("--shading-levels", type=int, default=3, help="number of crosshatch layers (0 = outlines only)")
    ap.add_argument("--no-outline", action="store_true", help="skip the edge/outline pass")
    ap.add_argument("--no-bg-detect", action="store_true", help="disable background detection (background will be shaded like any other region)")
    ap.add_argument("--bg-tolerance", type=int, default=18, help="brightness tolerance for background detection (0-255, higher = more aggressive)")
    ap.add_argument("--bed-width-mm", type=float, default=220.0, help="plotter bed/plate width (mm)")
    ap.add_argument("--bed-height-mm", type=float, default=220.0, help="plotter bed/plate height (mm)")
    ap.add_argument("--no-center", action="store_true", help="don't auto-center the drawing on the bed; use --origin-x/--origin-y instead")
    ap.add_argument("--origin-x", type=float, default=10.0, help="X offset of drawing origin on the bed (mm) -- only used with --no-center")
    ap.add_argument("--origin-y", type=float, default=10.0, help="Y offset of drawing origin on the bed (mm) -- only used with --no-center")

    args = ap.parse_args()

    gray, px_to_mm_x, px_to_mm_y, w_mm, h_mm = load_and_prepare(
        args.image, args.width_mm, args.height_mm
    )
    hatch_spacing_px = args.hatch_spacing_mm / ((px_to_mm_x + px_to_mm_y) / 2)

    background_mask = None
    if not args.no_bg_detect:
        background_mask = detect_background_mask(gray, args.bg_tolerance)

    all_paths = []
    if not args.no_outline:
        outline_paths = generate_outline_paths(gray, args.canny_low, args.canny_high, background_mask=background_mask)
        print(f"Outline pass: {len(outline_paths)} paths")
        all_paths.extend(outline_paths)

    if args.shading_levels > 0:
        hatch_paths = generate_hatch_paths(gray, args.shading_levels, hatch_spacing_px, background_mask=background_mask)
        print(f"Shading pass: {len(hatch_paths)} paths")
        all_paths.extend(hatch_paths)

    if not all_paths:
        print("No paths generated -- try lowering thresholds or checking the image.", file=sys.stderr)
        sys.exit(1)

    print("Ordering paths for minimal travel...")
    all_paths = order_paths(all_paths)

    if args.no_center:
        origin_x, origin_y = args.origin_x, args.origin_y
    else:
        origin_x, origin_y = compute_centered_origin(args.bed_width_mm, args.bed_height_mm, w_mm, h_mm)
        if origin_x < 0 or origin_y < 0:
            print(f"Warning: drawing ({w_mm:.1f}x{h_mm:.1f}mm) is larger than the bed "
                  f"({args.bed_width_mm:.1f}x{args.bed_height_mm:.1f}mm); it will run off the plate.",
                  file=sys.stderr)

    gcode = paths_to_gcode(
        all_paths, px_to_mm_x, px_to_mm_y,
        args.pen_up_z, args.pen_down_z,
        args.draw_feed, args.travel_feed,
        origin_x, origin_y,
    )

    with open(args.output, "w") as f:
        f.write(gcode)

    total_pts = sum(len(p) for p in all_paths)
    print(f"Wrote {args.output}: {len(all_paths)} paths, {total_pts} points")
    print(f"Drawing area: {w_mm:.1f}mm x {h_mm:.1f}mm, placed at ({origin_x:.1f}, {origin_y:.1f})")


if __name__ == "__main__":
    main()
