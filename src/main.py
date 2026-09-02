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

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize
from PySide6.QtGui import QImage, QPixmap, QIcon, QAction, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QGroupBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QComboBox, QFileDialog, QScrollArea, QSizePolicy, QPlainTextEdit,
    QProgressBar, QStatusBar, QMessageBox, QSplitter, QToolBar
)

import gcode_core as core
from theme import STYLE

APP_NAME = "PenPlotter Studio"


def _resolve_resources_dir():
    """Find the resources/ folder whether we're:
    - frozen by PyInstaller (resources bundled at sys._MEIPASS/resources), or
    - running from source in this repo (resources/ lives one level above src/)
    """
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        return os.path.join(frozen_base, "resources")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "resources")


RESOURCES_DIR = _resolve_resources_dir()


def section_label(text):
    lbl = QLabel(text)
    lbl.setProperty("class", "sectionHeader")
    lbl.setObjectName("sectionHeader")
    lbl.setStyleSheet("color:#7c82ff; font-weight:600; font-size:11px; letter-spacing:1px;")
    return lbl


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


# --------------------------------------------------------------------------
# Background worker: image -> paths -> preview render
# --------------------------------------------------------------------------

class PreviewWorker(QThread):
    finished_ok = Signal(object, object, object)   # paths, canvas(np array), stats(dict)
    failed = Signal(str)

    def __init__(self, image_path, params):
        super().__init__()
        self.image_path = image_path
        self.params = params

    def run(self):
        try:
            p = self.params
            gray, px_x, px_y, w_mm, h_mm = core.load_and_prepare(
                self.image_path, p["width_mm"], p["height_mm"]
            )

            p = dict(p)
            p["px_x"], p["px_y"] = px_x, px_y
            paths = core.build_paths_for_image(gray, p)

            # render preview canvas (white bg, black strokes) at gray's resolution
            canvas = np.full((*gray.shape, 3), 255, dtype=np.uint8)
            for path in paths:
                if len(path) == 1:
                    x, y = int(round(path[0][0])), int(round(path[0][1]))
                    if 0 <= x < canvas.shape[1] and 0 <= y < canvas.shape[0]:
                        cv2.circle(canvas, (x, y), 1, (108, 92, 231), -1, cv2.LINE_AA)
                else:
                    pts = np.array([(int(x), int(y)) for x, y in path], dtype=np.int32)
                    if len(pts) >= 2:
                        cv2.polylines(canvas, [pts], False, (108, 92, 231), 1, cv2.LINE_AA)

            total_pts = sum(len(pp) for pp in paths)
            total_len_mm = 0.0
            for pp in paths:
                for i in range(len(pp) - 1):
                    dx = (pp[i + 1][0] - pp[i][0]) * px_x
                    dy = (pp[i + 1][1] - pp[i][1]) * px_y
                    total_len_mm += (dx * dx + dy * dy) ** 0.5

            stats = {
                "paths": len(paths),
                "points": total_pts,
                "draw_len_mm": total_len_mm,
                "width_mm": w_mm,
                "height_mm": h_mm,
                "px_x": px_x,
                "px_y": px_y,
            }
            self.finished_ok.emit(paths, canvas, stats)
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


# --------------------------------------------------------------------------
# Background worker: serial G-code streaming
# --------------------------------------------------------------------------

class SerialSendWorker(QThread):
    progress = Signal(int, int)     # sent, total
    log = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, port, baud, gcode_text):
        super().__init__()
        self.port = port
        self.baud = baud
        self.gcode_lines = [l for l in gcode_text.splitlines() if l.strip() and not l.strip().startswith(";")]
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
            time.sleep(2)  # allow board to reset
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
                # wait for 'ok'
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
        self.resize(1280, 820)

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
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self.update_preview)

        self._build_ui()
        self._connect_signals()

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

        # ---- Sidebar ----
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(360)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(16, 16, 16, 16)
        sv.setSpacing(6)

        header = QHBoxLayout()
        icon_lbl = QLabel()
        icon_path = os.path.join(RESOURCES_DIR, "icon_48.png")
        if os.path.exists(icon_path):
            icon_lbl.setPixmap(QPixmap(icon_path).scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(icon_lbl)
        title_box = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("Image \u2192 plotter G-code")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_box.setSpacing(0)
        header.addLayout(title_box)
        header.addStretch()
        sv.addLayout(header)

        # Image group
        img_group = QGroupBox("Source Image")
        ig = QVBoxLayout(img_group)
        self.load_btn = QPushButton("Load Image\u2026")
        self.file_label = QLabel("No image loaded")
        self.file_label.setStyleSheet("color:#8b8fa3; font-size:11px;")
        self.file_label.setWordWrap(True)
        self.thumb_label = QLabel()
        self.thumb_label.setFixedHeight(120)
        self.thumb_label.setAlignment(Qt.AlignCenter)
        self.thumb_label.setStyleSheet("background:#101116; border:1px solid #2c2f3d; border-radius:6px;")
        ig.addWidget(self.load_btn)
        ig.addWidget(self.thumb_label)
        ig.addWidget(self.file_label)
        sv.addWidget(img_group)

        # Size group
        size_group = QGroupBox("Drawing Size")
        sf = QFormLayout(size_group)
        self.width_spin = QDoubleSpinBox(); self.width_spin.setRange(10, 500); self.width_spin.setValue(150); self.width_spin.setSuffix(" mm")
        self.height_spin = QDoubleSpinBox(); self.height_spin.setRange(10, 500); self.height_spin.setValue(150); self.height_spin.setSuffix(" mm"); self.height_spin.setEnabled(False)
        self.lock_aspect = QCheckBox("Lock aspect ratio"); self.lock_aspect.setChecked(True)
        sf.addRow("Width", self.width_spin)
        sf.addRow("Height", self.height_spin)
        sf.addRow(self.lock_aspect)
        self.bed_width = QDoubleSpinBox(); self.bed_width.setRange(10, 1000); self.bed_width.setValue(220); self.bed_width.setSuffix(" mm")
        self.bed_height = QDoubleSpinBox(); self.bed_height.setRange(10, 1000); self.bed_height.setValue(220); self.bed_height.setSuffix(" mm")
        self.unusable_right = QDoubleSpinBox(); self.unusable_right.setRange(0, 100); self.unusable_right.setValue(10); self.unusable_right.setSuffix(" mm")
        sf.addRow("Plate width", self.bed_width)
        sf.addRow("Plate height", self.bed_height)
        sf.addRow("Reserved right side", self.unusable_right)
        self.center_on_bed = QCheckBox("Center on plate"); self.center_on_bed.setChecked(True)
        sf.addRow(self.center_on_bed)
        self.origin_x = QDoubleSpinBox(); self.origin_x.setRange(0, 1000); self.origin_x.setValue(0); self.origin_x.setSuffix(" mm")
        self.origin_y = QDoubleSpinBox(); self.origin_y.setRange(0, 1000); self.origin_y.setValue(10); self.origin_y.setSuffix(" mm")
        sf.addRow("Bed origin X", self.origin_x)
        sf.addRow("Bed origin Y", self.origin_y)
        self.origin_x.setEnabled(False)
        self.origin_y.setEnabled(False)
        sv.addWidget(size_group)

        # Pen Z group
        pen_group = QGroupBox("Pen Z Calibration  (Z-axis lift, no servo)")
        pf = QFormLayout(pen_group)
        self.pen_up = QDoubleSpinBox(); self.pen_up.setRange(0, 50); self.pen_up.setValue(3.0); self.pen_up.setSuffix(" mm"); self.pen_up.setSingleStep(0.5)
        self.pen_down = QDoubleSpinBox(); self.pen_down.setRange(-10, 50); self.pen_down.setValue(0.0); self.pen_down.setSuffix(" mm"); self.pen_down.setSingleStep(0.1)
        pf.addRow("Pen up Z", self.pen_up)
        pf.addRow("Pen down Z", self.pen_down)
        hint = QLabel("Jog these live in the Connection panel below, on real paper, before trusting the values.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8fa3; font-size:11px;")
        pf.addRow(hint)
        sv.addWidget(pen_group)

        # Background detection group
        bg_group = QGroupBox("Background Detection")
        bg_group.setCheckable(True)
        bg_group.setChecked(True)
        self.bg_group = bg_group
        bgf = QFormLayout(bg_group)
        self.bg_tolerance = QSpinBox(); self.bg_tolerance.setRange(1, 100); self.bg_tolerance.setValue(18)
        bgf.addRow("Sensitivity", self.bg_tolerance)
        bg_hint = QLabel("Excludes uniform backdrop (paper, wall, solid color) touching the image edges from shading, so it isn't hatched.")
        bg_hint.setWordWrap(True)
        bg_hint.setStyleSheet("color:#8b8fa3; font-size:11px;")
        bgf.addRow(bg_hint)
        sv.addWidget(bg_group)

        # Outline group
        outline_group = QGroupBox("Outline Tracing")
        outline_group.setCheckable(True)
        outline_group.setChecked(True)
        self.outline_group = outline_group
        of = QFormLayout(outline_group)
        self.canny_low = QSpinBox(); self.canny_low.setRange(0, 500); self.canny_low.setValue(50)
        self.canny_high = QSpinBox(); self.canny_high.setRange(0, 500); self.canny_high.setValue(150)
        of.addRow("Edge sensitivity (low)", self.canny_low)
        of.addRow("Edge sensitivity (high)", self.canny_high)
        edge_hint = QLabel("The image is contrast-normalized first; extreme settings are kept usable instead of turning every pixel into an edge.")
        edge_hint.setWordWrap(True); edge_hint.setStyleSheet("color:#8b8fa3; font-size:11px;")
        of.addRow(edge_hint)
        sv.addWidget(outline_group)

        # Shading group
        shade_group = QGroupBox("Crosshatch Shading")
        shade_group.setCheckable(True)
        shade_group.setChecked(True)
        self.shade_group = shade_group
        shf = QFormLayout(shade_group)
        self.shading_method = QComboBox(); self.shading_method.addItems(["Crosshatch", "Infill (dots)"])
        self.dot_spacing = QDoubleSpinBox(); self.dot_spacing.setRange(0.6, 6.0); self.dot_spacing.setValue(1.8); self.dot_spacing.setSingleStep(0.1); self.dot_spacing.setSuffix(" mm")
        self.dot_threshold = QDoubleSpinBox(); self.dot_threshold.setRange(0.0, 0.8); self.dot_threshold.setValue(0.08); self.dot_threshold.setSingleStep(0.01)
        self.shading_levels = QSpinBox(); self.shading_levels.setRange(0, 6); self.shading_levels.setValue(3)
        self.hatch_spacing = QDoubleSpinBox(); self.hatch_spacing.setRange(0.4, 5.0); self.hatch_spacing.setValue(1.4); self.hatch_spacing.setSingleStep(0.1); self.hatch_spacing.setSuffix(" mm")
        shf.addRow("Method", self.shading_method)
        shf.addRow("Hatch levels", self.shading_levels)
        shf.addRow("Hatch spacing", self.hatch_spacing)
        shf.addRow("Dot spacing", self.dot_spacing)
        shf.addRow("Dot darkness threshold", self.dot_threshold)
        shade_hint = QLabel("Infill uses individual pen dots with density proportional to darkness; blank areas are skipped entirely.")
        shade_hint.setWordWrap(True); shade_hint.setStyleSheet("color:#8b8fa3; font-size:11px;")
        shf.addRow(shade_hint)
        sv.addWidget(shade_group)

        # Motion group
        motion_group = QGroupBox("Motion")
        mf = QFormLayout(motion_group)
        self.draw_feed = QSpinBox(); self.draw_feed.setRange(100, 8000); self.draw_feed.setValue(1500); self.draw_feed.setSuffix(" mm/min")
        self.travel_feed = QSpinBox(); self.travel_feed.setRange(100, 12000); self.travel_feed.setValue(3000); self.travel_feed.setSuffix(" mm/min")
        mf.addRow("Draw feed", self.draw_feed)
        mf.addRow("Travel feed", self.travel_feed)
        sv.addWidget(motion_group)

        # Action buttons
        self.preview_btn = QPushButton("Update Preview")
        self.preview_btn.setObjectName("primaryButton")
        self.export_btn = QPushButton("Export G-code\u2026")
        self.export_btn.setObjectName("accentButton")
        self.export_btn.setEnabled(False)
        sv.addWidget(self.preview_btn)
        sv.addWidget(self.export_btn)

        # Connection group
        conn_group = QGroupBox("Send to Machine")
        cf = QVBoxLayout(conn_group)
        row1 = QHBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports_btn = QPushButton("\u21bb")
        self.refresh_ports_btn.setFixedWidth(32)
        row1.addWidget(self.port_combo)
        row1.addWidget(self.refresh_ports_btn)
        cf.addLayout(row1)
        row2 = QHBoxLayout()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["115200", "250000", "9600", "57600"])
        row2.addWidget(QLabel("Baud"))
        row2.addWidget(self.baud_combo)
        cf.addLayout(row2)

        jog_row = QHBoxLayout()
        self.jog_up_btn = QPushButton("Pen Up")
        self.jog_down_btn = QPushButton("Pen Down")
        jog_row.addWidget(self.jog_up_btn)
        jog_row.addWidget(self.jog_down_btn)
        cf.addLayout(jog_row)

        self.send_btn = QPushButton("Send G-code to Printer")
        self.send_btn.setObjectName("accentButton")
        self.send_btn.setEnabled(False)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setEnabled(False)
        send_row = QHBoxLayout()
        send_row.addWidget(self.pause_btn)
        send_row.addWidget(self.stop_btn)
        cf.addWidget(self.send_btn)
        cf.addLayout(send_row)

        self.send_progress = QProgressBar()
        self.send_progress.setValue(0)
        cf.addWidget(self.send_progress)

        self.log_console = QPlainTextEdit()
        self.log_console.setObjectName("logConsole")
        self.log_console.setReadOnly(True)
        self.log_console.setFixedHeight(90)
        cf.addWidget(self.log_console)

        sv.addWidget(conn_group)
        sv.addStretch()

        sidebar_scroll.setWidget(sidebar)
        splitter.addWidget(sidebar_scroll)

        # ---- Canvas area ----
        canvas_area = QWidget()
        canvas_area.setObjectName("canvasArea")
        cv_layout = QVBoxLayout(canvas_area)
        cv_layout.setContentsMargins(20, 20, 20, 20)

        self.canvas_label = QLabel("Load an image to begin")
        self.canvas_label.setAlignment(Qt.AlignCenter)
        self.canvas_label.setStyleSheet("color:#5b5f73; font-size:15px; background:#101116; border:1px solid #2c2f3d; border-radius:10px;")
        self.canvas_label.setMinimumSize(400, 400)
        cv_layout.addWidget(self.canvas_label, 1)

        stats_row = QHBoxLayout()
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#9296ab; font-size:12px;")
        stats_row.addWidget(self.stats_label)
        stats_row.addStretch()
        cv_layout.addLayout(stats_row)

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
        self.center_on_bed.toggled.connect(lambda v: (self.origin_x.setEnabled(not v), self.origin_y.setEnabled(not v)))

        # Debounced auto-preview on parameter changes
        for w in [self.width_spin, self.height_spin, self.canny_low, self.canny_high,
                  self.shading_levels, self.hatch_spacing, self.dot_spacing, self.dot_threshold, self.bg_tolerance]:
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                w.valueChanged.connect(self._schedule_preview)
        self.outline_group.toggled.connect(self._schedule_preview)
        self.shade_group.toggled.connect(self._schedule_preview)
        self.shading_method.currentIndexChanged.connect(self._schedule_preview)
        self.bg_group.toggled.connect(self._schedule_preview)

    def _schedule_preview(self, *_):
        if self.image_path:
            self._debounce.start()

    # ---------------- Actions ----------------

    def on_load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Image", "", "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)"
        )
        if not path:
            return
        self.image_path = path
        self.file_label.setText(os.path.basename(path))
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            thumb = cv_to_qpixmap(img).scaled(
                self.thumb_label.width() or 320, self.thumb_label.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.thumb_label.setPixmap(thumb)
            h, w = img.shape
            if self.lock_aspect.isChecked():
                self.height_spin.setValue(self.width_spin.value() * h / w)
        self.update_preview()

    def _collect_params(self):
        return dict(
            width_mm=self.width_spin.value(),
            height_mm=None if self.lock_aspect.isChecked() else self.height_spin.value(),
            outline_enabled=self.outline_group.isChecked(),
            shading_enabled=self.shade_group.isChecked(),
            bg_detect_enabled=self.bg_group.isChecked(),
            bg_tolerance=self.bg_tolerance.value(),
            canny_low=self.canny_low.value(),
            canny_high=self.canny_high.value(),
            shading_levels=self.shading_levels.value(),
            hatch_spacing_mm=self.hatch_spacing.value(),
            shading_spacing_mm=self.dot_spacing.value() if self.shading_method.currentIndex() == 1 else self.hatch_spacing.value(),
            shading_method="infill" if self.shading_method.currentIndex() == 1 else "crosshatch",
            dot_threshold=self.dot_threshold.value(),
            front_view_correction=True,
            pen_up_z=self.pen_up.value(),
            pen_down_z=self.pen_down.value(),
            draw_feed=self.draw_feed.value(),
            travel_feed=self.travel_feed.value(),
            bed_width_mm=self.bed_width.value(),
            bed_height_mm=self.bed_height.value(),
            unusable_right_mm=self.unusable_right.value(),
            center_on_bed=self.center_on_bed.isChecked(),
            origin_x=self.origin_x.value(),
            origin_y=self.origin_y.value(),
        )

    def update_preview(self):
        if not self.image_path:
            return
        if self._preview_busy:
            # A worker is already running -- don't touch self.preview_worker
            # while it's alive (destroying a running QThread aborts the
            # process). Just remember to regenerate once it finishes.
            self._preview_dirty = True
            return
        self._preview_busy = True
        self._preview_dirty = False
        self.statusBar().showMessage("Generating preview\u2026")
        self.preview_btn.setEnabled(False)
        params = self._collect_params()
        self.preview_worker = PreviewWorker(self.image_path, params)
        self.preview_worker.finished_ok.connect(self._on_preview_ready)
        self.preview_worker.failed.connect(self._on_preview_failed)
        self.preview_worker.finished.connect(self._on_preview_thread_finished)
        self.preview_worker.start()

    def _on_preview_thread_finished(self):
        # QThread's own 'finished' signal -- guaranteed to fire after run()
        # returns, so it's always safe to let this thread object go now.
        self._preview_busy = False
        if self._preview_dirty:
            self._preview_dirty = False
            QTimer.singleShot(0, self.update_preview)

    def _on_preview_ready(self, paths, canvas, stats):
        self.current_paths = paths
        self.current_canvas = canvas
        self.current_stats = stats
        pix = cv_to_qpixmap(canvas)
        scaled = pix.scaled(self.canvas_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.canvas_label.setPixmap(scaled)
        self.canvas_label.setText("")
        est_min = stats["draw_len_mm"] / max(self.draw_feed.value(), 1)
        self.stats_label.setText(
            f'{stats["paths"]} paths \u00b7 {stats["points"]} points \u00b7 '
            f'{stats["draw_len_mm"]/1000:.1f} m of line \u00b7 '
            f'~{est_min:.1f} min draw time (feed-rate estimate) \u00b7 '
            f'{stats["width_mm"]:.0f}\u00d7{stats["height_mm"]:.0f} mm'
        )
        self.export_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        self.statusBar().showMessage("Preview updated", 3000)

    def _on_preview_failed(self, msg):
        self.preview_btn.setEnabled(True)
        self.statusBar().showMessage("Preview failed", 4000)
        QMessageBox.warning(self, "Preview failed", msg)

    def _resolve_origin(self, p, stats):
        if p["center_on_bed"]:
            origin_x, origin_y = core.compute_centered_origin(
                p["bed_width_mm"], p["bed_height_mm"], stats["width_mm"], stats["height_mm"],
                p["unusable_right_mm"]
            )
            return origin_x, origin_y
        origin_x, origin_y = p["origin_x"], p["origin_y"]
        core.validate_drawing_placement(
            origin_x, origin_y, stats["width_mm"], stats["height_mm"],
            p["bed_width_mm"], p["bed_height_mm"], p["unusable_right_mm"]
        )
        return origin_x, origin_y

    def _build_gcode(self):
        p = self._collect_params()
        stats = self.current_stats
        origin_x, origin_y = self._resolve_origin(p, stats)
        gcode = core.paths_to_gcode(
            self.current_paths, stats["px_x"], stats["px_y"],
            p["pen_up_z"], p["pen_down_z"],
            p["draw_feed"], p["travel_feed"],
            origin_x, origin_y,
        )
        return gcode

    def on_export_gcode(self):
        if not self.current_paths:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export G-code", "drawing.gcode", "G-code (*.gcode)")
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
        self.send_worker.finished_ok.connect(lambda: self.statusBar().showMessage("G-code sent", 4000))
        self.send_worker.failed.connect(self._on_send_failed_msg)
        self.send_worker.finished.connect(self._on_send_thread_finished)
        self.send_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.send_worker.start()

    def _on_send_thread_finished(self):
        # Same rule as the preview worker: only safe to let this thread
        # object go once QThread itself reports finished.
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
