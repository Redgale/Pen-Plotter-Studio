#!/usr/bin/env python3
"""Render a .gcode file to a PNG the way it would actually be drawn on the
bed (pen-down moves only), so changes to the conversion engine can be
eyeballed without a printer. Not part of the app -- a dev aid."""

import argparse
import re
import sys

import numpy as np
import cv2


def parse_gcode(path):
    x = y = 0.0
    z = 10.0
    pen_down_z = None
    # first infer pen-down z as the smallest Z we ever see
    zs = []
    for line in open(path):
        m = re.search(r"Z(-?\d+\.?\d*)", line)
        if m:
            zs.append(float(m.group(1)))
    if zs:
        pen_down_z = min(zs)
    down_thresh = (min(zs) + sorted(set(zs))[1]) / 2 if len(set(zs)) > 1 else min(zs) + 0.1

    segments = []
    dots = []
    cur = None
    for line in open(path):
        line = line.split(";")[0].strip()
        if not line:
            continue
        if not line.startswith(("G0", "G1")):
            continue
        mx = re.search(r"X(-?\d+\.?\d*)", line)
        my = re.search(r"Y(-?\d+\.?\d*)", line)
        mz = re.search(r"Z(-?\d+\.?\d*)", line)
        nx = float(mx.group(1)) if mx else x
        ny = float(my.group(1)) if my else y
        nz = float(mz.group(1)) if mz else z
        pen = nz <= down_thresh and z <= down_thresh
        if mz and not mx and not my:
            # pure Z move -- transition
            if nz <= down_thresh and cur is None:
                cur = [(x, y)]
            elif nz > down_thresh and cur is not None:
                if len(cur) == 1:
                    dots.append(cur[0])
                elif len(cur) > 1:
                    segments.append(cur)
                cur = None
            z = nz
            continue
        if cur is not None:
            cur.append((nx, ny))
        x, y, z = nx, ny, nz
    if cur is not None and len(cur) > 1:
        segments.append(cur)
    return segments, dots


def render(path, out, bed=(220, 220), px_per_mm=4):
    segs, dots = parse_gcode(path)
    bed = (int(bed[0]), int(bed[1]))
    W = int(bed[0] * px_per_mm)
    H = int(bed[1] * px_per_mm)
    img = np.full((H, W, 3), 255, np.uint8)
    # bed grid
    for mm in range(0, bed[0] + 1, 10):
        cv2.line(img, (mm * px_per_mm, 0), (mm * px_per_mm, H), (235, 235, 235), 1)
    for mm in range(0, bed[1] + 1, 10):
        cv2.line(img, (0, H - mm * px_per_mm), (W, H - mm * px_per_mm), (235, 235, 235), 1)

    def to_px(p):
        return (int(p[0] * px_per_mm), int(H - p[1] * px_per_mm))  # +Y up

    for s in segs:
        pts = np.array([to_px(p) for p in s], np.int32)
        cv2.polylines(img, [pts], False, (0, 0, 0), 1, cv2.LINE_AA)
    for d in dots:
        cv2.circle(img, to_px(d), 1, (0, 0, 0), -1, cv2.LINE_AA)

    cv2.imwrite(out, img)
    print(f"{out}: {len(segs)} strokes, {len(dots)} dots")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("gcode")
    ap.add_argument("out")
    ap.add_argument("--bed", default="220x220")
    args = ap.parse_args()
    bw, bh = (float(v) for v in args.bed.lower().split("x"))
    render(args.gcode, args.out, (bw, bh))
