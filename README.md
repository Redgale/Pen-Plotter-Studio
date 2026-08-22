# PenPlotter Studio

A desktop app that converts images into G-code for a pen-plotter conversion
of an Ender 3 V2 (or similar) — the printer's own **Z axis** lifts and lowers
the pen, so there's no servo, no custom firmware, and no heater commands
involved. Outline tracing, adjustable crosshatch shading, a live preview of
the actual pen strokes, and optional direct serial streaming to the machine.

![screenshot](docs/screenshot.png)

## Features

- Load any image, get a live preview of the actual paths that will be drawn
  (not just the source image) as you adjust settings.
- Outline tracing (Canny edge detection) for clean linework.
- Adjustable multi-level crosshatch shading — darker areas get more
  overlapping hatch directions, mimicking pencil/ink shading.
- Export standalone `.gcode`, or stream it straight to the machine over
  serial with a progress bar and live log.
- Runs on Linux (AppImage) and Windows (`.exe`); source is plain
  cross-platform Python/Qt, so macOS works too if you build it yourself.

## Repo layout

```
src/                    the application (main.py, gcode_core.py, theme.py)
cli/                    standalone command-line version, no GUI/Qt required
resources/              icons (png + ico)
packaging/linux/        build_appimage.sh -> PenPlotterStudio-x86_64.AppImage
packaging/windows/      build.bat / build_debug.bat -> PenPlotterStudio.exe
docs/                   screenshots etc.
```

## Quick start — run from source (any OS)

```bash
pip install -r requirements.txt
python src/main.py
```

## Building a distributable

**Linux (AppImage):**
```bash
bash packaging/linux/build_appimage.sh
```
Produces `packaging/linux/PenPlotterStudio-x86_64.AppImage`. The script
installs its own Python dependencies, runs PyInstaller, bundles
`libxcb-cursor` and friends (Qt 6.5+ needs it and not every distro has it by
default), and downloads `appimagetool` automatically if it's not already
next to the script.

**Windows (.exe):**
```
packaging\windows\build.bat
```
Requires Python 3.11+ installed with "Add python.exe to PATH" checked.
Produces `packaging\windows\dist\PenPlotterStudio.exe`. If something crashes
and you need the real error instead of a silent window close, use
`build_debug.bat` instead and run the resulting exe from a Command Prompt
window (not by double-clicking) so the console stays open.

**CLI only (no GUI dependencies):**
```bash
pip install opencv-python numpy
python cli/image_to_gcode.py input.jpg output.gcode --width-mm 150
```
Same conversion engine, scriptable, useful for batch jobs. Run
`--help` for all flags.

## Hardware assumption

This targets a plotter conversion that uses the machine's **existing Z
axis** to lift/lower the pen (no servo). Because the G-code this produces
never sends a heater command, stock Marlin's thermal-runaway protection is
never triggered — no firmware changes needed on a typical Ender 3 V2.

---

## Settings reference

### Source Image
**Load Image…** — opens a file picker (PNG/JPG/BMP/TIFF). This is the only
required input; everything else has a sensible default.

### Drawing Size
| Setting | Range | Default | What it does |
|---|---|---|---|
| Width | 10–500 mm | 150 | Physical width of the output drawing. |
| Height | 10–500 mm | 150 | Physical height. Disabled while aspect lock is on. |
| Lock aspect ratio | — | on | When on, Height is computed automatically from the source image's proportions. Turn off to intentionally stretch/squash. |
| Bed origin X / Y | 0–300 mm | 10 / 10 | Where the drawing's origin lands on the bed — shift it to clear clips, a jig, or a previous drawing. |

### Pen Z Calibration (Z-axis lift, no servo)
| Setting | Range | Default | What it does |
|---|---|---|---|
| Pen up Z | 0–50 mm | 5 | Height the machine parks at between strokes / while repositioning. Must clear the paper. |
| Pen down Z | -10–50 mm | 0 | Height where the pen tip touches paper with the right pressure. **This is specific to your pen and holder** — don't trust the default. Use the **Pen Up / Pen Down** jog buttons in the Connection panel, on real paper, to find the right value before drawing anything you care about. |

### Outline Tracing
Uses a Canny edge detector to find and trace clean outlines, then simplifies
each traced line to cut down the point count.

| Setting | Range | Default | What it does |
|---|---|---|---|
| Enable (checkbox on the group title) | — | on | Turn outline tracing off entirely for shading-only output. |
| Edge sensitivity (low) | 0–500 | 50 | Canny's lower threshold. Pixels below this gradient strength are never treated as edges. |
| Edge sensitivity (high) | 0–500 | 150 | Canny's upper threshold. Pixels above this are always treated as edges; pixels between low and high only count if connected to one. |

**Tuning tip:** lower both values to catch fainter/more detail (more lines,
more noise); raise both to keep only strong, confident edges (cleaner, but
may drop subtle detail).

### Crosshatch Shading
Builds shading from layered hatch lines: darker regions of the image get
more overlapping line directions stacked on top of each other, the way a
pencil illustrator crosshatches for shadow.

| Setting | Range | Default | What it does |
|---|---|---|---|
| Enable (checkbox on the group title) | — | on | Turn shading off for outline-only line art. |
| Hatch levels | 0–6 | 3 | How many overlapping hatch directions get layered. Each extra level adds one more rotated line pass over progressively darker brightness bands — darkest areas get the most overlapping directions (visually darkest), lighter mid-tones get fewer. More levels = smoother tonal range but larger files and longer draw times. `0` = no shading at all. |
| Base spacing | 0.4–5.0 mm | 1.4 | Gap between parallel lines within one hatch layer. **This is the biggest lever on how dark the shading can get** — smaller spacing = more ink coverage = darker; go below ~0.8mm only if your pen tip is fine enough not to just fill in solid black. |

### Motion
| Setting | Range | Default | What it does |
|---|---|---|---|
| Draw feed | 100–8000 mm/min | 1500 | Speed while the pen is down and drawing. Slower = cleaner lines through curves; faster = quicker draws but risk of skipping/vibration. |
| Travel feed | 100–12000 mm/min | 3000 | Speed for pen-up repositioning moves and Z moves. Can be much faster than draw feed since nothing's touching the paper. |

### Preview / Export
- **Update Preview** — forces an immediate regeneration (auto-updates ~400ms
  after any setting change anyway; this just skips the wait).
- The stats line under the canvas shows path/point counts, total line
  length, and an **estimated draw time** — this is `line length ÷ draw feed`
  only, so it doesn't account for travel moves or acceleration; real time
  will run somewhat longer.
- **Export G-code…** — saves the currently previewed paths to a `.gcode`
  file.

### Send to Machine
| Control | What it does |
|---|---|
| Port dropdown + ⟳ | Lists available serial ports (via `pyserial`). Click refresh after plugging the printer in. |
| Baud | Connection baud rate — 115200 is standard for stock Marlin on an Ender 3 V2. |
| Pen Up / Pen Down | Jogs the machine live to the current `Pen up Z` / `Pen down Z` values — use these on real paper to calibrate before trusting the numbers. |
| Send G-code to Printer | Streams the current G-code over serial, one line at a time, waiting for Marlin's `ok` acknowledgement between lines. |
| Pause / Resume | Pauses mid-stream. Doesn't lift the pen automatically — the machine just stops where it is. |
| Stop | Aborts sending. |
| Progress bar | Lines sent vs. total. |
| Log console | Raw connection/response log. |

---

## How the conversion works (for contributors)

1. **Outline pass** — Canny edge detection → `cv2.findContours` → each
   contour simplified with `approxPolyDP` to cut point count.
2. **Shading pass** — the image is split into brightness bands; each band
   gets a hatch-line pass at a different angle and spacing (`gcode_core.
   generate_hatch_paths`), so darker bands accumulate more overlapping
   directions.
3. **Path ordering** — a greedy nearest-neighbor pass (`order_paths`)
   reorders and optionally reverses paths to cut down on pen-up travel
   distance.
4. **G-code export** — straight `G1` moves for both draw and pen-lift (no
   `G0`, no `M280` servo commands, no heater commands), so it runs on
   unmodified Marlin.

`src/gcode_core.py` and `cli/image_to_gcode.py` share this logic; the GUI in
`src/main.py` imports the same functions the CLI script uses, so behavior
stays identical between the two.

## Known gotcha if you modify and rebuild

Use `opencv-python-headless`, not `opencv-python`. The non-headless build
bundles its own Qt plugins that silently conflict with PySide6's when both
get packaged together by PyInstaller, causing the app to fail to start with
no useful error. `requirements.txt` and both packaging scripts already use
the headless build — just don't swap it out.

## License

MIT — see [LICENSE](LICENSE).
