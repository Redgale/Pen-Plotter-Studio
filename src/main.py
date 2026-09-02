#!/usr/bin/env python3
"""
PenPlotter Studio
==================
A GUI front-end for converting images into pen-plotter G-code (Z-axis pen
lift, no servo required) for an Ender 3 V2 style plotter conversion, with
a live path preview and optional direct serial streaming to the machine.
"""

import os
import sys
import time
import traceback

import cv2
import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QComboBox, QFileDialog, QScrollArea, QPlainTextEdit,
    QProgressBar, QStatusBar, QMessageBox, QSplitter
)

import gcode_core as core
from theme import STYLE

APP_NAME = "PenPlotter Studio"

# (name, width_mm, height_mm)
PAPER_PRESETS = [
    ("A4 portrait", 210.0, 297.0),
    ("A4 landscape", 297.0, 210.0),
    ("US Letter portrait", 215.9, 279.4),
    ("US Letter landscape", 279.4, 215.9),
    ("A3 portrait", 297.0, 420.0),
    ("Custom / whole bed", 0.0, 0.0),
]


def _resolve_resources_dir():
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return os.path.join(frozen_base, "resources")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "resources")


RESOURCES_DIR = _resolve_resources_dir()


def cv_to_qpixmap(gray_or_bgr):
    if gray_or_bgr is None:
        return QPixmap()
    if len(gray_or_bgr.shape) == 2:
        h, w = gray_or_bgr.shape
        qimg = QImage(gray_or_bgr.data, w, h, w, QImage.Format_Grayscale8)
    else:
        h, w, ch = gray_or_bgr.shape
        rgb = cv2.cvtColor(gray_or_bgr, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def usable_rect(p):
    """Return (x0, y0, w, h) of the reachable bed area after margins."""
    x0 = p["margin_left"]
    y0 = p["margin_front"]
    w = p["bed_width_mm"] - p["margin_left"] - p["margin_right"]
    h = p["bed_height_mm"] - p["margin_front"] - p["margin_back"]
    return x0, y0, max(w, 1.0), max(h, 1.0)


def resolve_size_and_origin(p, img_w_px, img_h_px):
    """Shared by preview + export so the numbers always agree."""
    x0, y0, uw, uh = usable_rect(p)
    paper_w, paper_h = p["paper_w"], p["paper_h"]
    if paper_w <= 0 or paper_h <= 0:      # "whole bed"
        paper_w, paper_h, margin = uw, uh, 0.0
    else:
        margin = p["paper_margin_mm"]
    fit_w, fit_h = core.fit_drawing(
        img_w_px, img_h_px, p["width_mm"],
        None if p["lock_aspect"] else p["height_mm"],
        paper_w, paper_h, margin, uw, uh)
    ox, oy = core.place_in_usable(
        fit_w, fit_h, x0, y0, uw, uh, center=p["center_on_bed"],
        origin_x=p["origin_x"], origin_y=p["origin_y"])
    return fit_w, fit_h, ox, oy


# --------------------------------------------------------------------------
# Background worker: image -> paths -> preview render
# --------------------------------------------------------------------------

class PreviewWorker(QThread):
    finished_ok = Signal(object, object, object)   # paths, canvas, stats
    failed = Signal(str)

    def __init__(self, image_path, params):
        super().__init__()
        self.image_path = image_path
        self.params = params

    def run(self):
        try:
            p = self.params
            gray, px_x, px_y, w_mm, h_mm, alpha_mask = core.load_and_prepare(
                self.image_path, p["width_mm"],
                None if p["lock_aspect"] else p["height_mm"],
                brightness=p["brightness"], contrast=p["contrast"],
                gamma=p["gamma"], normalize=p["normalize"],
                clahe=p["clahe"], denoise=p["denoise"],
            )
            ih, iw = gray.shape

            fit_w, fit_h, ox, oy = resolve_size_and_origin(p, iw, ih)
            fpx_x, fpx_y = fit_w / iw, fit_h / ih

            bp = dict(p)
            bp["_px_x"], bp["_px_y"] = fpx_x, fpx_y
            paths, background_mask = core.build_paths(gray, alpha_mask, bp)

            # preview canvas: white bg, subject only, drawn the same way up
            # as the source image (which is the way it will print)
            canvas = np.full((ih, iw, 3), 255, dtype=np.uint8)
            stroke = (108, 92, 231)
            for path in paths:
                if len(path) == 1:
                    x, y = path[0]
                    cv2.circle(canvas, (int(x), int(y)), 1, stroke, -1, cv2.LINE_AA)
                else:
                    pts = np.array([(int(x), int(y)) for x, y in path], np.int32)
                    cv2.polylines(canvas, [pts], False, stroke, 1, cv2.LINE_AA)

            n_dots = sum(1 for pp in paths if len(pp) == 1)
            total_pts = sum(len(pp) for pp in paths)
            total_len_mm = 0.0
            travel_len_mm = 0.0
            prev_end = None
            for pp in paths:
                if prev_end is not None:
                    dx = (pp[0][0] - prev_end[0]) * fpx_x
                    dy = (pp[0][1] - prev_end[1]) * fpx_y
                    travel_len_mm += (dx * dx + dy * dy) ** 0.5
                for i in range(len(pp) - 1):
                    dx = (pp[i + 1][0] - pp[i][0]) * fpx_x
                    dy = (pp[i + 1][1] - pp[i][1]) * fpx_y
                    total_len_mm += (dx * dx + dy * dy) ** 0.5
                prev_end = pp[-1]

            # z time: one lift+drop per multi-point path, plus one per dot
            z_travel_mm = (len(paths)) * 2.0 * max(
                p["pen_up_z"] - p["pen_down_z"], 0.1)
            dwell_min = n_dots * p["dot_dwell_ms"] / 60000.0

            stats = dict(
                paths=len(paths), points=total_pts, dots=n_dots,
                draw_len_mm=total_len_mm, travel_len_mm=travel_len_mm,
                z_travel_mm=z_travel_mm, dwell_min=dwell_min,
                width_mm=fit_w, height_mm=fit_h,
                origin_x=ox, origin_y=oy, px_x=fpx_x, px_y=fpx_y,
                img_w=iw, img_h=ih,
            )
            self.finished_ok.emit(paths, canvas, stats)
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


# --------------------------------------------------------------------------
# Background worker: serial G-code streaming
# --------------------------------------------------------------------------

class SerialSendWorker(QThread):
    progress = Signal(int, int)
    log = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, port, baud, gcode_text):
        super().__init__()
        self.port = port
        self.baud = baud
        self.gcode_lines = [l for l in gcode_text.splitlines()
                            if l.strip() and not l.strip().startswith(";")]
        self._stop = False
        self._pause = False

    def stop(self):
        self._stop = True

    def set_paused(self, val):
        self._pause = val

    def run(self):
        try:
            import serial
        except ImportError:
            self.failed.emit("pyserial is not installed.")
            return
        try:
            ser = serial.Serial(self.port, self.baud, timeout=5)
            time.sleep(2)
            ser.reset_input_buffer()
            total = len(self.gcode_lines)
            self.log.emit(f"Connected to {self.port} @ {self.baud}. Sending {total} lines...")
            for i, line in enumerate(self.gcode_lines):
                if self._stop:
                    self.log.emit("Stopped by user.")
                    break
                while self._pause and not self._stop:
                    time.sleep(0.2)
                ser.write((line + "\n").encode("utf-8"))
                while True:
                    resp = ser.readline().decode(errors="ignore").strip()
                    if resp.lower().startswith("ok") or resp == "":
                        break
                    if resp.lower().startswith("error"):
                        self.log.emit(f"! {resp} (line: {line})")
                        break
                self.progress.emit(i + 1, total)
            ser.close()
            if not self._stop:
                self.log.emit("Done sending.")
                self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(str(e))


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 860)

        icon_path = os.path.join(RESOURCES_DIR, "icon_256.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.image_path = None
        self.current_paths = None
        self.current_canvas = None
        self.current_stats = None
        self.gcode_text = None
        self.preview_worker = None
        self.send_worker = None
        self._preview_busy = False
        self._preview_dirty = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(450)
        self._debounce.timeout.connect(self.update_preview)

        self._build_ui()
        self._connect_signals()
        self._sync_shading_style()

    # ---------------- UI construction ----------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(370)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(16, 16, 16, 16)
        sv.setSpacing(6)

        header = QHBoxLayout()
        icon_lbl = QLabel()
        ip = os.path.join(RESOURCES_DIR, "icon_48.png")
        if os.path.exists(ip):
            icon_lbl.setPixmap(QPixmap(ip).scaled(36, 36, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation))
        header.addWidget(icon_lbl)
        tbox = QVBoxLayout()
        title = QLabel(APP_NAME); title.setObjectName("appTitle")
        subtitle = QLabel("Image → plotter G-code"); subtitle.setObjectName("appSubtitle")
        tbox.addWidget(title); tbox.addWidget(subtitle); tbox.setSpacing(0)
        header.addLayout(tbox); header.addStretch()
        sv.addLayout(header)

        def spin(cls, lo, hi, val, step=None, suffix=None, decimals=None):
            w = cls()
            w.setRange(lo, hi); w.setValue(val)
            if step is not None:
                w.setSingleStep(step)
            if suffix:
                w.setSuffix(suffix)
            if decimals is not None and isinstance(w, QDoubleSpinBox):
                w.setDecimals(decimals)
            return w

        # ---- Source Image ----
        img_group = QGroupBox("Source Image")
        ig = QVBoxLayout(img_group)
        self.load_btn = QPushButton("Load Image…")
        self.file_label = QLabel("No image loaded")
        self.file_label.setStyleSheet("color:#8b8fa3; font-size:11px;")
        self.file_label.setWordWrap(True)
        self.thumb_label = QLabel(); self.thumb_label.setFixedHeight(120)
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self.thumb_label.setStyleSheet(
            "background:#101116; border:1px solid #2c2f3d; border-radius:6px;")
        ig.addWidget(self.load_btn); ig.addWidget(self.thumb_label)
        ig.addWidget(self.file_label)
        sv.addWidget(img_group)

        # ---- Paper & Placement ----
        paper_group = QGroupBox("Paper & Placement")
        pg = QFormLayout(paper_group)
        self.paper_combo = QComboBox()
        for name, _, _ in PAPER_PRESETS:
            self.paper_combo.addItem(name)
        self.width_spin = spin(QDoubleSpinBox, 10, 500, 180, 1, " mm")
        self.height_spin = spin(QDoubleSpinBox, 10, 500, 180, 1, " mm")
        self.height_spin.setEnabled(False)
        self.lock_aspect = QCheckBox("Lock aspect ratio"); self.lock_aspect.setChecked(True)
        self.paper_margin = spin(QDoubleSpinBox, 0, 100, 10, 1, " mm")
        pg.addRow("Paper", self.paper_combo)
        pg.addRow("Max width", self.width_spin)
        pg.addRow("Max height", self.height_spin)
        pg.addRow(self.lock_aspect)
        pg.addRow("Margin from paper edge", self.paper_margin)
        note = QLabel("The drawing is scaled to fit the paper AND the reachable "
                      "bed area, then centered in the reachable area.")
        note.setWordWrap(True); note.setStyleSheet("color:#8b8fa3; font-size:11px;")
        pg.addRow(note)
        sv.addWidget(paper_group)

        # ---- Bed / Unusable margins ----
        bed_group = QGroupBox("Bed & Unusable Margins")
        bg = QFormLayout(bed_group)
        self.bed_width = spin(QDoubleSpinBox, 10, 1000, 220, 1, " mm")
        self.bed_height = spin(QDoubleSpinBox, 10, 1000, 220, 1, " mm")
        self.margin_left = spin(QDoubleSpinBox, 0, 200, 0, 1, " mm")
        self.margin_right = spin(QDoubleSpinBox, 0, 200, 10, 1, " mm")
        self.margin_front = spin(QDoubleSpinBox, 0, 200, 0, 1, " mm")
        self.margin_back = spin(QDoubleSpinBox, 0, 200, 0, 1, " mm")
        bg.addRow("Bed width (X)", self.bed_width)
        bg.addRow("Bed height (Y)", self.bed_height)
        bg.addRow("Unusable left (X-)", self.margin_left)
        bg.addRow("Unusable right (X+)", self.margin_right)
        bg.addRow("Unusable front (Y-)", self.margin_front)
        bg.addRow("Unusable back (Y+)", self.margin_back)
        self.center_on_bed = QCheckBox("Center in reachable area"); self.center_on_bed.setChecked(True)
        bg.addRow(self.center_on_bed)
        self.origin_x = spin(QDoubleSpinBox, 0, 1000, 0, 1, " mm"); self.origin_x.setEnabled(False)
        self.origin_y = spin(QDoubleSpinBox, 0, 1000, 0, 1, " mm"); self.origin_y.setEnabled(False)
        bg.addRow("Origin X", self.origin_x)
        bg.addRow("Origin Y", self.origin_y)
        sv.addWidget(bed_group)

        # ---- Orientation ----
        ori_group = QGroupBox("Orientation")
        og = QVBoxLayout(ori_group)
        self.flip_y = QCheckBox("Flip vertical so the print matches the preview")
        self.flip_y.setChecked(True)
        self.mirror_x = QCheckBox("Mirror horizontal (180° / mirrored machine)")
        og.addWidget(self.flip_y); og.addWidget(self.mirror_x)
        oh = QLabel("Ender-3 Y grows toward the back. 'Flip vertical' puts the "
                    "top of the image at the back so it reads right-side-up "
                    "from the front without moving the coordinates off the bed.")
        oh.setWordWrap(True); oh.setStyleSheet("color:#8b8fa3; font-size:11px;")
        og.addWidget(oh)
        sv.addWidget(ori_group)

        # ---- Pen Z ----
        pen_group = QGroupBox("Pen Z Calibration  (Z-axis lift, no servo)")
        pf = QFormLayout(pen_group)
        self.pen_up = spin(QDoubleSpinBox, 0, 50, 3.0, 0.5, " mm")
        self.pen_down = spin(QDoubleSpinBox, -10, 50, 0.0, 0.1, " mm")
        pf.addRow("Pen up Z (hop)", self.pen_up)
        pf.addRow("Pen down Z", self.pen_down)
        ph = QLabel("Jog these live in the Connection panel, on real paper, "
                    "before trusting the values.")
        ph.setWordWrap(True); ph.setStyleSheet("color:#8b8fa3; font-size:11px;")
        pf.addRow(ph)
        sv.addWidget(pen_group)

        # ---- Tone ----
        tone_group = QGroupBox("Tone / Image Recognition")
        tf = QFormLayout(tone_group)
        self.brightness = spin(QSpinBox, -120, 120, 0)
        self.contrast = spin(QDoubleSpinBox, 0.3, 3.0, 1.0, 0.05, None, 2)
        self.gamma = spin(QDoubleSpinBox, 0.3, 3.0, 1.0, 0.05, None, 2)
        self.dark_boost = spin(QDoubleSpinBox, 0.5, 2.0, 1.0, 0.05, None, 2)
        tf.addRow("Brightness", self.brightness)
        tf.addRow("Contrast", self.contrast)
        tf.addRow("Gamma", self.gamma)
        tf.addRow("Shadow weight", self.dark_boost)
        self.opt_normalize = QCheckBox("Auto levels"); self.opt_normalize.setChecked(True)
        self.opt_clahe = QCheckBox("Local contrast (CLAHE)"); self.opt_clahe.setChecked(True)
        self.opt_denoise = QCheckBox("Edge-preserving denoise"); self.opt_denoise.setChecked(True)
        tf.addRow(self.opt_normalize)
        tf.addRow(self.opt_clahe)
        tf.addRow(self.opt_denoise)
        sv.addWidget(tone_group)

        # ---- Background detection ----
        bgd_group = QGroupBox("Background Detection")
        bgd_group.setCheckable(True); bgd_group.setChecked(True)
        self.bg_group = bgd_group
        bgf = QFormLayout(bgd_group)
        self.bg_tolerance = spin(QSpinBox, 1, 100, 18)
        bgf.addRow("Sensitivity", self.bg_tolerance)
        bgh = QLabel("Excludes a uniform backdrop touching the image edges from "
                     "shading. Transparent PNG cut-outs are handled automatically.")
        bgh.setWordWrap(True); bgh.setStyleSheet("color:#8b8fa3; font-size:11px;")
        bgf.addRow(bgh)
        sv.addWidget(bgd_group)

        # ---- Outline ----
        outline_group = QGroupBox("Outline Tracing")
        outline_group.setCheckable(True); outline_group.setChecked(True)
        self.outline_group = outline_group
        of = QFormLayout(outline_group)
        self.canny_low = spin(QSpinBox, 0, 500, 60)
        self.canny_high = spin(QSpinBox, 0, 500, 140)
        of.addRow("Edge sensitivity (low)", self.canny_low)
        of.addRow("Edge sensitivity (high)", self.canny_high)
        sv.addWidget(outline_group)

        # ---- Shading ----
        shade_group = QGroupBox("Shading")
        shade_group.setCheckable(True); shade_group.setChecked(True)
        self.shade_group = shade_group
        shf = QFormLayout(shade_group)
        self.shading_style = QComboBox()
        self.shading_style.addItems(["Crosshatch", "Dots (stipple)"])
        self.shading_levels = spin(QSpinBox, 1, 8, 4)
        self.hatch_spacing = spin(QDoubleSpinBox, 0.4, 6.0, 1.0, 0.1, " mm")
        self.dot_spacing = spin(QDoubleSpinBox, 0.3, 4.0, 0.6, 0.05, " mm")
        self.dot_gamma = spin(QDoubleSpinBox, 0.3, 3.0, 1.0, 0.05, None, 2)
        self.dot_dwell = spin(QSpinBox, 0, 500, 0, 5, " ms")
        shf.addRow("Style", self.shading_style)
        shf.addRow("Hatch levels", self.shading_levels)
        shf.addRow("Hatch spacing", self.hatch_spacing)
        shf.addRow("Dot pitch (darkest)", self.dot_spacing)
        shf.addRow("Dot tone gamma", self.dot_gamma)
        shf.addRow("Dot dwell", self.dot_dwell)
        self._hatch_rows = [self.shading_levels, self.hatch_spacing]
        self._dot_rows = [self.dot_spacing, self.dot_gamma, self.dot_dwell]
        sv.addWidget(shade_group)

        # ---- Motion / Output ----
        motion_group = QGroupBox("Motion & Output")
        mf = QFormLayout(motion_group)
        self.draw_feed = spin(QSpinBox, 100, 8000, 1500, 100, " mm/min")
        self.travel_feed = spin(QSpinBox, 100, 12000, 3000, 100, " mm/min")
        mf.addRow("Draw feed", self.draw_feed)
        mf.addRow("Travel feed", self.travel_feed)
        self.fan_off = QCheckBox("Head fan off (M107)"); self.fan_off.setChecked(True)
        self.home_xy = QCheckBox("Home X/Y at start (G28 X Y)"); self.home_xy.setChecked(True)
        mf.addRow(self.fan_off)
        mf.addRow(self.home_xy)
        sv.addWidget(motion_group)

        # ---- Actions ----
        self.preview_btn = QPushButton("Update Preview")
        self.preview_btn.setObjectName("primaryButton")
        self.export_btn = QPushButton("Export G-code…")
        self.export_btn.setObjectName("accentButton")
        self.export_btn.setEnabled(False)
        sv.addWidget(self.preview_btn)
        sv.addWidget(self.export_btn)

        # ---- Connection ----
        conn_group = QGroupBox("Send to Machine")
        cf = QVBoxLayout(conn_group)
        row1 = QHBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("↻"); self.refresh_ports_btn.setFixedWidth(32)
        row1.addWidget(self.port_combo); row1.addWidget(self.refresh_ports_btn)
        cf.addLayout(row1)
        row2 = QHBoxLayout()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["115200", "250000", "9600", "57600"])
        row2.addWidget(QLabel("Baud")); row2.addWidget(self.baud_combo)
        cf.addLayout(row2)
        jog_row = QHBoxLayout()
        self.jog_up_btn = QPushButton("Pen Up")
        self.jog_down_btn = QPushButton("Pen Down")
        jog_row.addWidget(self.jog_up_btn); jog_row.addWidget(self.jog_down_btn)
        cf.addLayout(jog_row)
        self.send_btn = QPushButton("Send G-code to Printer")
        self.send_btn.setObjectName("accentButton"); self.send_btn.setEnabled(False)
        self.pause_btn = QPushButton("Pause"); self.pause_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        send_row = QHBoxLayout()
        send_row.addWidget(self.pause_btn); send_row.addWidget(self.stop_btn)
        cf.addWidget(self.send_btn); cf.addLayout(send_row)
        self.send_progress = QProgressBar(); self.send_progress.setValue(0)
        cf.addWidget(self.send_progress)
        self.log_console = QPlainTextEdit(); self.log_console.setObjectName("logConsole")
        self.log_console.setReadOnly(True); self.log_console.setFixedHeight(90)
        cf.addWidget(self.log_console)
        sv.addWidget(conn_group)
        sv.addStretch()

        sidebar_scroll.setWidget(sidebar)
        splitter.addWidget(sidebar_scroll)

        # ---- Canvas ----
        canvas_area = QWidget(); canvas_area.setObjectName("canvasArea")
        cvl = QVBoxLayout(canvas_area)
        cvl.setContentsMargins(20, 20, 20, 20)
        self.canvas_label = QLabel("Load an image to begin")
        self.canvas_label.setAlignment(Qt.AlignCenter)
        self.canvas_label.setStyleSheet(
            "color:#5b5f73; font-size:15px; background:#101116; "
            "border:1px solid #2c2f3d; border-radius:10px;")
        self.canvas_label.setMinimumSize(400, 400)
        cvl.addWidget(self.canvas_label, 1)
        stats_row = QHBoxLayout()
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#9296ab; font-size:12px;")
        stats_row.addWidget(self.stats_label); stats_row.addStretch()
        cvl.addLayout(stats_row)
        splitter.addWidget(canvas_area)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")
        self.refresh_ports()

    def _connect_signals(self):
        self.load_btn.clicked.connect(self.on_load_image)
        self.preview_btn.clicked.connect(self.update_preview)
        self.export_btn.clicked.connect(self.on_export_gcode)
        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        self.send_btn.clicked.connect(self.on_send_gcode)
        self.pause_btn.clicked.connect(self.on_pause_toggle)
        self.stop_btn.clicked.connect(self.on_stop_send)
        self.jog_up_btn.clicked.connect(lambda: self.on_jog(self.pen_up.value()))
        self.jog_down_btn.clicked.connect(lambda: self.on_jog(self.pen_down.value()))

        self.lock_aspect.toggled.connect(lambda v: self.height_spin.setEnabled(not v))
        self.center_on_bed.toggled.connect(
            lambda v: (self.origin_x.setEnabled(not v), self.origin_y.setEnabled(not v)))
        self.paper_combo.currentIndexChanged.connect(self._on_paper_changed)
        self.shading_style.currentIndexChanged.connect(self._sync_shading_style)
        self.shading_style.currentIndexChanged.connect(self._schedule_preview)

        for w in [self.width_spin, self.height_spin, self.paper_margin,
                  self.bed_width, self.bed_height, self.margin_left,
                  self.margin_right, self.margin_front, self.margin_back,
                  self.origin_x, self.origin_y, self.canny_low, self.canny_high,
                  self.shading_levels, self.hatch_spacing, self.dot_spacing,
                  self.dot_gamma, self.bg_tolerance, self.brightness,
                  self.contrast, self.gamma, self.dark_boost]:
            w.valueChanged.connect(self._schedule_preview)
        for c in [self.outline_group, self.shade_group, self.bg_group,
                  self.lock_aspect, self.center_on_bed, self.flip_y,
                  self.mirror_x, self.opt_normalize, self.opt_clahe,
                  self.opt_denoise]:
            c.toggled.connect(self._schedule_preview)

    def _sync_shading_style(self, *_):
        is_dots = self.shading_style.currentText().startswith("Dots")
        for w in self._hatch_rows:
            w.setEnabled(not is_dots)
        for w in self._dot_rows:
            w.setEnabled(is_dots)

    def _on_paper_changed(self, idx):
        name, w, h = PAPER_PRESETS[idx]
        whole_bed = w <= 0
        self.paper_margin.setEnabled(not whole_bed)
        self._schedule_preview()

    def _schedule_preview(self, *_):
        if self.image_path:
            self._debounce.start()

    # ---------------- Actions ----------------

    def on_load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)")
        if not path:
            return
        self.image_path = path
        self.file_label.setText(os.path.basename(path))
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            thumb = cv_to_qpixmap(img).scaled(
                self.thumb_label.width() or 320, self.thumb_label.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.thumb_label.setPixmap(thumb)
            h, w = img.shape
            if self.lock_aspect.isChecked():
                self.height_spin.setValue(self.width_spin.value() * h / w)
        self.update_preview()

    def _paper_dims(self):
        idx = self.paper_combo.currentIndex()
        _, w, h = PAPER_PRESETS[idx]
        return w, h

    def _collect_params(self):
        pw, ph = self._paper_dims()
        style = "stipple" if self.shading_style.currentText().startswith("Dots") else "crosshatch"
        return dict(
            width_mm=self.width_spin.value(),
            height_mm=self.height_spin.value(),
            lock_aspect=self.lock_aspect.isChecked(),
            paper_w=pw, paper_h=ph,
            paper_margin_mm=self.paper_margin.value(),
            bed_width_mm=self.bed_width.value(),
            bed_height_mm=self.bed_height.value(),
            margin_left=self.margin_left.value(),
            margin_right=self.margin_right.value(),
            margin_front=self.margin_front.value(),
            margin_back=self.margin_back.value(),
            center_on_bed=self.center_on_bed.isChecked(),
            origin_x=self.origin_x.value(),
            origin_y=self.origin_y.value(),
            flip_y=self.flip_y.isChecked(),
            mirror_x=self.mirror_x.isChecked(),
            fan_off=self.fan_off.isChecked(),
            home_xy=self.home_xy.isChecked(),
            brightness=self.brightness.value(),
            contrast=self.contrast.value(),
            gamma=self.gamma.value(),
            dark_boost=self.dark_boost.value(),
            normalize=self.opt_normalize.isChecked(),
            clahe=self.opt_clahe.isChecked(),
            denoise=self.opt_denoise.isChecked(),
            bg_detect_enabled=self.bg_group.isChecked(),
            bg_tolerance=self.bg_tolerance.value(),
            outline_enabled=self.outline_group.isChecked(),
            canny_low=self.canny_low.value(),
            canny_high=self.canny_high.value(),
            shading_enabled=self.shade_group.isChecked(),
            shading_style=style,
            shading_levels=self.shading_levels.value(),
            hatch_spacing_mm=self.hatch_spacing.value(),
            dot_spacing_mm=self.dot_spacing.value(),
            dot_gamma=self.dot_gamma.value(),
            dot_dwell_ms=self.dot_dwell.value(),
            pen_up_z=self.pen_up.value(),
            pen_down_z=self.pen_down.value(),
            draw_feed=self.draw_feed.value(),
            travel_feed=self.travel_feed.value(),
        )

    def update_preview(self):
        if not self.image_path:
            return
        if self._preview_busy:
            self._preview_dirty = True
            return
        self._preview_busy = True
        self._preview_dirty = False
        self.statusBar().showMessage("Generating preview…")
        self.preview_btn.setEnabled(False)
        params = self._collect_params()
        self.preview_worker = PreviewWorker(self.image_path, params)
        self.preview_worker.finished_ok.connect(self._on_preview_ready)
        self.preview_worker.failed.connect(self._on_preview_failed)
        self.preview_worker.finished.connect(self._on_preview_thread_finished)
        self.preview_worker.start()

    def _on_preview_thread_finished(self):
        self._preview_busy = False
        if self._preview_dirty:
            self._preview_dirty = False
            QTimer.singleShot(0, self.update_preview)

    def _on_preview_ready(self, paths, canvas, stats):
        self.current_paths = paths
        self.current_canvas = canvas
        self.current_stats = stats
        pix = cv_to_qpixmap(canvas)
        scaled = pix.scaled(self.canvas_label.size(), Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
        self.canvas_label.setPixmap(scaled)
        self.canvas_label.setText("")
        df = max(self.draw_feed.value(), 1)
        tf = max(self.travel_feed.value(), 1)
        est_min = (stats["draw_len_mm"] / df
                   + (stats["travel_len_mm"] + stats["z_travel_mm"]) / tf
                   + stats["dwell_min"])
        shading = "dots" if stats["dots"] else "strokes"
        extra = f'{stats["dots"]} dots · ' if stats["dots"] else ""
        self.stats_label.setText(
            f'{stats["paths"]} paths · {extra}{stats["points"]} points · '
            f'{stats["draw_len_mm"]/1000:.1f} m of line · '
            f'~{est_min:.0f} min ({shading}, incl. travel) · '
            f'{stats["width_mm"]:.0f}×{stats["height_mm"]:.0f} mm '
            f'@ ({stats["origin_x"]:.0f}, {stats["origin_y"]:.0f})')
        self.export_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        self.statusBar().showMessage("Preview updated", 3000)

    def _on_preview_failed(self, msg):
        self.preview_btn.setEnabled(True)
        self.statusBar().showMessage("Preview failed", 4000)
        QMessageBox.warning(self, "Preview failed", msg)

    def _build_gcode(self):
        p = self._collect_params()
        s = self.current_stats
        gcode = core.paths_to_gcode(
            self.current_paths, s["px_x"], s["px_y"],
            p["pen_up_z"], p["pen_down_z"], p["draw_feed"], p["travel_feed"],
            s["origin_x"], s["origin_y"],
            content_w_px=s["img_w"], content_h_px=s["img_h"],
            flip_y=p["flip_y"], mirror_x=p["mirror_x"], fan_off=p["fan_off"],
            dot_dwell_ms=p["dot_dwell_ms"], home_xy=p["home_xy"],
        )
        return gcode

    def on_export_gcode(self):
        if not self.current_paths:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export G-code",
                                              "drawing.gcode", "G-code (*.gcode)")
        if not path:
            return
        gcode = self._build_gcode()
        with open(path, "w") as f:
            f.write(gcode)
        self.gcode_text = gcode
        self.send_btn.setEnabled(True)
        self.statusBar().showMessage(f"Saved {path}", 4000)

    # ---- Serial ----

    def refresh_ports(self):
        self.port_combo.clear()
        try:
            from serial.tools import list_ports
            ports = list_ports.comports()
            for p in ports:
                self.port_combo.addItem(p.device)
            if not ports:
                self.port_combo.addItem("No ports found")
        except Exception:
            self.port_combo.addItem("pyserial not available")

    def on_jog(self, z_value):
        port = self.port_combo.currentText()
        if not port or "found" in port or "available" in port:
            QMessageBox.information(self, "No port", "Select a serial port first.")
            return
        try:
            import serial
            baud = int(self.baud_combo.currentText())
            ser = serial.Serial(port, baud, timeout=3)
            time.sleep(2)
            ser.write(f"G1 Z{z_value:.2f} F1000\n".encode())
            ser.readline()
            ser.close()
            self.log_console.appendPlainText(f"Jogged Z to {z_value:.2f}")
        except Exception as e:
            QMessageBox.warning(self, "Jog failed", str(e))

    def on_send_gcode(self):
        if not self.gcode_text:
            if self.current_paths:
                self.gcode_text = self._build_gcode()
            else:
                return
        port = self.port_combo.currentText()
        if not port or "found" in port or "available" in port:
            QMessageBox.information(self, "No port", "Select a serial port first.")
            return
        baud = int(self.baud_combo.currentText())
        self.send_worker = SerialSendWorker(port, baud, self.gcode_text)
        self.send_worker.progress.connect(self._on_send_progress)
        self.send_worker.log.connect(self.log_console.appendPlainText)
        self.send_worker.finished_ok.connect(
            lambda: self.statusBar().showMessage("G-code sent", 4000))
        self.send_worker.failed.connect(self._on_send_failed_msg)
        self.send_worker.finished.connect(self._on_send_thread_finished)
        self.send_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.send_worker.start()

    def _on_send_thread_finished(self):
        self.send_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.pause_btn.setText("Pause")

    def _on_send_failed_msg(self, msg):
        QMessageBox.warning(self, "Send failed", msg)

    def _on_send_progress(self, sent, total):
        self.send_progress.setMaximum(total)
        self.send_progress.setValue(sent)

    def on_pause_toggle(self):
        if not self.send_worker:
            return
        paused = self.pause_btn.text() == "Pause"
        self.send_worker.set_paused(paused)
        self.pause_btn.setText("Resume" if paused else "Pause")

    def on_stop_send(self):
        if self.send_worker:
            self.send_worker.stop()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setApplicationName(APP_NAME)
    icon_path = os.path.join(RESOURCES_DIR, "icon_256.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
