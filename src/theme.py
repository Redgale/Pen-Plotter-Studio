"""Dark theme stylesheet for PenPlotter Studio."""

STYLE = """
* {
    font-family: "Segoe UI", "Inter", "Ubuntu", sans-serif;
    font-size: 13px;
    color: #e6e6ea;
}

QMainWindow, QWidget#centralWidget {
    background-color: #1b1d24;
}

QWidget#sidebar {
    background-color: #212430;
    border-right: 1px solid #33374a;
}

QWidget#canvasArea {
    background-color: #15161c;
}

QLabel#appTitle {
    font-size: 17px;
    font-weight: 600;
    color: #ffffff;
    padding: 4px 0px;
}

QLabel#appSubtitle {
    font-size: 11px;
    color: #8b8fa3;
    padding-bottom: 6px;
}

QLabel.sectionHeader {
    font-size: 11px;
    font-weight: 600;
    color: #7c82ff;
    letter-spacing: 1px;
    padding-top: 10px;
    padding-bottom: 2px;
}

QGroupBox {
    border: 1px solid #33374a;
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    background-color: #23263280;
    font-weight: 600;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #b7bbe0;
}

QPushButton {
    background-color: #35394a;
    border: 1px solid #454a60;
    border-radius: 6px;
    padding: 7px 14px;
    color: #f0f0f5;
}
QPushButton:hover { background-color: #3f4459; }
QPushButton:pressed { background-color: #2c2f3d; }
QPushButton:disabled { color: #6a6d7a; background-color: #2a2c36; }

QPushButton#primaryButton {
    background-color: #6c5ce7;
    border: 1px solid #7d6cf0;
    font-weight: 600;
    color: #ffffff;
    padding: 9px 16px;
}
QPushButton#primaryButton:hover { background-color: #7a6af0; }
QPushButton#primaryButton:pressed { background-color: #5c4dd0; }
QPushButton#primaryButton:disabled { background-color: #3a3750; color: #85809c; }

QPushButton#accentButton {
    background-color: #16a085;
    border: 1px solid #1abc9c;
    font-weight: 600;
    color: #ffffff;
}
QPushButton#accentButton:hover { background-color: #19b699; }
QPushButton#accentButton:disabled { background-color: #2a3d3a; color: #7f9490; }

QPushButton#dangerButton {
    background-color: #a83246;
    border: 1px solid #c2405a;
}
QPushButton#dangerButton:hover { background-color: #bb3a50; }

QSlider::groove:horizontal {
    height: 4px;
    background: #3a3e52;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #8c7bff;
    width: 15px;
    height: 15px;
    margin: -6px 0;
    border-radius: 7px;
}
QSlider::sub-page:horizontal {
    background: #6c5ce7;
    border-radius: 2px;
}

QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit {
    background-color: #181a22;
    border: 1px solid #3a3e52;
    border-radius: 5px;
    padding: 4px 6px;
    color: #f0f0f5;
    selection-background-color: #6c5ce7;
}
QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus, QLineEdit:focus {
    border: 1px solid #7d6cf0;
}
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView {
    background-color: #23263a;
    selection-background-color: #6c5ce7;
    border: 1px solid #3a3e52;
}

QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 4px;
    border: 1px solid #4a4e64;
    background: #181a22;
}
QCheckBox::indicator:checked {
    background: #6c5ce7;
    border: 1px solid #7d6cf0;
}

QProgressBar {
    border: 1px solid #3a3e52;
    border-radius: 5px;
    background-color: #181a22;
    text-align: center;
    color: #e6e6ea;
    height: 16px;
}
QProgressBar::chunk {
    background-color: #6c5ce7;
    border-radius: 4px;
}

QPlainTextEdit#logConsole {
    background-color: #101116;
    border: 1px solid #2c2f3d;
    border-radius: 6px;
    color: #9adb8f;
    font-family: "Consolas", "Menlo", monospace;
    font-size: 11px;
}

QStatusBar {
    background-color: #191b23;
    border-top: 1px solid #2c2f3d;
    color: #9296ab;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
}
QScrollBar::handle:vertical {
    background: #40445a;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #565b78; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }

QTabWidget::pane { border: 1px solid #33374a; border-radius: 6px; top: -1px; }
QTabBar::tab {
    background: #23263a;
    padding: 6px 14px;
    border: 1px solid #33374a;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    color: #9296ab;
}
QTabBar::tab:selected {
    background: #2f334a;
    color: #ffffff;
}

QToolTip {
    background-color: #2a2d3d;
    color: #f0f0f5;
    border: 1px solid #454a60;
    padding: 4px 6px;
}
"""
