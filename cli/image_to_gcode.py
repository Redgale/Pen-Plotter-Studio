#!/usr/bin/env python3
"""
image_to_gcode.py -- command-line front-end for the PenPlotter Studio
conversion engine (no GUI / Qt required).

The conversion itself lives in ../src/gcode_core.py, which the GUI imports
too, so CLI and GUI output stay identical.

Usage:
  python3 image_to_gcode.py input.jpg output.gcode --width-mm 150 \
      --pen-up-z 3 --pen-down-z 0

Run with --help for the full list of options.
"""

import argparse
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import gcode_core as core  # noqa: E402


PAPER_PRESETS = {
    "a4-portrait": (210.0, 297.0),
    "a4-landscape": (297.0, 210.0),
    "letter-portrait": (215.9, 279.4),
    "letter-landscape": (279.4, 215.9),
}


def main():
    ap = argparse.ArgumentParser(
        description="Convert an image into pen-plotter G-code (Z-axis pen lift).")
    ap.add_argument("image", help="input image path (jpg/png/etc)")
    ap.add_argument("output", help="output .gcode path")

    # size / placement
    ap.add_argument("--width-mm", type=float, default=150.0)
    ap.add_argument("--height-mm", type=float, default=None,
                    help="explicit height (default: from image aspect)")
    ap.add_argument("--paper", choices=sorted(PAPER_PRESETS), default="a4-portrait")
    ap.add_argument("--paper-margin-mm", type=float, default=10.0,
                    help="keep the drawing this far in from the paper edge")
    ap.add_argument("--bed-width-mm", type=float, default=220.0)
    ap.add_argument("--bed-height-mm", type=float, default=220.0)
    ap.add_argument("--margin-left-mm", type=float, default=0.0)
    ap.add_argument("--margin-right-mm", type=float, default=10.0,
                    help="unusable strip on the printer's high-X edge")
    ap.add_argument("--margin-front-mm", type=float, default=0.0)
    ap.add_argument("--margin-back-mm", type=float, default=0.0)
    ap.add_argument("--no-center", action="store_true")
    ap.add_argument("--origin-x", type=float, default=None)
    ap.add_argument("--origin-y", type=float, default=None)

    # pen Z
    ap.add_argument("--pen-up-z", type=float, default=3.0,
                    help="Z with the pen lifted (mm) -- default 3")
    ap.add_argument("--pen-down-z", type=float, default=0.0)

    # motion
    ap.add_argument("--draw-feed", type=int, default=1500)
    ap.add_argument("--travel-feed", type=int, default=3000)

    # orientation / machine
    ap.add_argument("--no-flip-y", action="store_true",
                    help="don't map image-top to the back of the bed")
    ap.add_argument("--mirror-x", action="store_true")
    ap.add_argument("--fan-on", action="store_true",
                    help="don't emit M107 (leave the head fan alone)")
    ap.add_argument("--no-home", action="store_true", help="skip G28 X Y")

    # tone
    ap.add_argument("--brightness", type=float, default=0.0)
    ap.add_argument("--contrast", type=float, default=1.0)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--dark-boost", type=float, default=1.0)
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--no-clahe", action="store_true")
    ap.add_argument("--no-denoise", action="store_true")

    # passes
    ap.add_argument("--no-outline", action="store_true")
    ap.add_argument("--canny-low", type=int, default=60)
    ap.add_argument("--canny-high", type=int, default=140)
    ap.add_argument("--shading", choices=["crosshatch", "stipple", "none"],
                    default="crosshatch")
    ap.add_argument("--shading-levels", type=int, default=4)
    ap.add_argument("--hatch-spacing-mm", type=float, default=1.0)
    ap.add_argument("--dot-spacing-mm", type=float, default=0.6,
                    help="dot pitch in the darkest areas (stipple mode)")
    ap.add_argument("--dot-gamma", type=float, default=1.0)
    ap.add_argument("--dot-dwell-ms", type=int, default=0,
                    help="pause at each dot so a stiff pen leaves a mark")

    # background
    ap.add_argument("--no-bg-detect", action="store_true")
    ap.add_argument("--bg-tolerance", type=int, default=18)

    args = ap.parse_args()

    gray, px_x, px_y, w_mm, h_mm, alpha_mask = core.load_and_prepare(
        args.image, args.width_mm, args.height_mm,
        brightness=args.brightness, contrast=args.contrast, gamma=args.gamma,
        normalize=not args.no_normalize, clahe=not args.no_clahe,
        denoise=not args.no_denoise,
    )

    # Work out the final on-paper size FIRST, so hatch/dot spacing is
    # computed against the real scale (matches what the GUI does).
    paper_w, paper_h = PAPER_PRESETS[args.paper]
    usable_w = args.bed_width_mm - args.margin_left_mm - args.margin_right_mm
    usable_h = args.bed_height_mm - args.margin_front_mm - args.margin_back_mm
    usable_x0 = args.margin_left_mm
    usable_y0 = args.margin_front_mm

    fit_w, fit_h = core.fit_drawing(
        gray.shape[1], gray.shape[0], args.width_mm, args.height_mm,
        paper_w, paper_h, args.paper_margin_mm, usable_w, usable_h)
    px_x = fit_w / gray.shape[1]
    px_y = fit_h / gray.shape[0]

    params = dict(
        bg_detect_enabled=not args.no_bg_detect,
        bg_tolerance=args.bg_tolerance,
        outline_enabled=not args.no_outline,
        canny_low=args.canny_low, canny_high=args.canny_high,
        shading_enabled=args.shading != "none",
        shading_style=args.shading,
        shading_levels=args.shading_levels,
        hatch_spacing_mm=args.hatch_spacing_mm,
        dot_spacing_mm=args.dot_spacing_mm,
        dot_gamma=args.dot_gamma,
        dark_boost=args.dark_boost,
        _px_x=px_x, _px_y=px_y,
    )

    paths, _ = core.build_paths(gray, alpha_mask, params)
    if not paths:
        print("No paths generated -- try adjusting tone / thresholds.", file=sys.stderr)
        sys.exit(1)

    ox, oy = core.place_in_usable(
        fit_w, fit_h, usable_x0, usable_y0, usable_w, usable_h,
        center=not args.no_center, origin_x=args.origin_x, origin_y=args.origin_y)

    gcode = core.paths_to_gcode(
        paths, px_x, px_y, args.pen_up_z, args.pen_down_z,
        args.draw_feed, args.travel_feed, ox, oy,
        content_w_px=gray.shape[1], content_h_px=gray.shape[0],
        flip_y=not args.no_flip_y, mirror_x=args.mirror_x,
        fan_off=not args.fan_on, dot_dwell_ms=args.dot_dwell_ms,
        home_xy=not args.no_home,
    )

    with open(args.output, "w") as f:
        f.write(gcode)

    total_pts = sum(len(p) for p in paths)
    print(f"Wrote {args.output}: {len(paths)} paths, {total_pts} points")
    print(f"Drawing {fit_w:.1f} x {fit_h:.1f} mm at ({ox:.1f}, {oy:.1f}); "
          f"usable bed {usable_w:.0f} x {usable_h:.0f} mm")


if __name__ == "__main__":
    main()
