"""
DNVT Flashpoint — PySide6 Management GUI

Shows real-time state of all 4 phone lines with call status,
audio levels, and SIP registration info.
"""

import sys
import time
import threading
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QFrame, QGroupBox, QProgressBar, QTextEdit,
    QStatusBar,
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QColor, QPalette, QIcon

import dnvt_bridge_py as bridge


# ============================================================================
# Color scheme
# ============================================================================

COLORS = {
    "idle":       "#555555",
    "dial":       "#2196F3",  # blue
    "traffic":    "#4CAF50",  # green
    "ring":       "#FF9800",  # orange
    "connected":  "#4CAF50",  # green
    "calling":    "#2196F3",  # blue
    "ringing_in": "#FF9800",  # orange
    "dialing":    "#2196F3",  # blue
    "error":      "#F44336",  # red
    "unreachable":"#F44336",  # red
    "transition": "#9E9E9E",  # gray
}

HW_STATE_NAMES = {
    0: "Idle", 1: "Dial", 2: "Traffic", 3: "Ring",
    4: "Await Ring", 5: "Unreachable", 6: "Req Ring", 7: "Transition",
}


# ============================================================================
# Line Widget — one per phone line
# ============================================================================

class LineWidget(QFrame):
    def __init__(self, line_num, parent=None):
        super().__init__(parent)
        self.line_num = line_num
        self.setFrameStyle(QFrame.Box | QFrame.Raised)
        self.setLineWidth(2)
        self.setMinimumWidth(220)
        self.setMinimumHeight(200)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Header
        self.header = QLabel(f"LINE {line_num + 1}")
        self.header.setAlignment(Qt.AlignCenter)
        self.header.setFont(QFont("Consolas", 16, QFont.Bold))
        self.header.setStyleSheet("color: white; padding: 6px;")
        layout.addWidget(self.header)

        # State indicator
        self.state_label = QLabel("IDLE")
        self.state_label.setAlignment(Qt.AlignCenter)
        self.state_label.setFont(QFont("Consolas", 12, QFont.Bold))
        self.state_label.setStyleSheet(
            f"background-color: {COLORS['idle']}; color: white; "
            "border-radius: 4px; padding: 4px;"
        )
        layout.addWidget(self.state_label)

        # Info grid
        info = QGridLayout()
        info.setSpacing(3)

        self.hw_label = self._info_row(info, 0, "HW State:")
        self.dialed_label = self._info_row(info, 1, "Dialed:")
        self.sip_label = self._info_row(info, 2, "SIP:")
        self.mode_label = self._info_row(info, 3, "Mode:")

        layout.addLayout(info)

        # Audio levels
        audio_box = QHBoxLayout()
        audio_box.setSpacing(4)

        rx_layout = QVBoxLayout()
        rx_layout.addWidget(QLabel("RX"))
        self.rx_bar = QProgressBar()
        self.rx_bar.setRange(0, 100)
        self.rx_bar.setTextVisible(False)
        self.rx_bar.setFixedHeight(12)
        self.rx_bar.setStyleSheet(
            "QProgressBar { background: #333; border-radius: 2px; }"
            "QProgressBar::chunk { background: #4CAF50; border-radius: 2px; }"
        )
        rx_layout.addWidget(self.rx_bar)
        audio_box.addLayout(rx_layout)

        tx_layout = QVBoxLayout()
        tx_layout.addWidget(QLabel("TX"))
        self.tx_bar = QProgressBar()
        self.tx_bar.setRange(0, 100)
        self.tx_bar.setTextVisible(False)
        self.tx_bar.setFixedHeight(12)
        self.tx_bar.setStyleSheet(
            "QProgressBar { background: #333; border-radius: 2px; }"
            "QProgressBar::chunk { background: #2196F3; border-radius: 2px; }"
        )
        tx_layout.addWidget(self.tx_bar)
        audio_box.addLayout(tx_layout)

        layout.addLayout(audio_box)
        layout.addStretch()

    def _info_row(self, grid, row, label_text):
        label = QLabel(label_text)
        label.setFont(QFont("Consolas", 9))
        label.setStyleSheet("color: #aaa;")
        value = QLabel("—")
        value.setFont(QFont("Consolas", 9))
        value.setStyleSheet("color: white;")
        grid.addWidget(label, row, 0)
        grid.addWidget(value, row, 1)
        return value

    def update_state(self, hw_state, sw_state="idle", dialed="", sip_info="",
                     mode="", rx_words=0, tx_words=0):
        # State label
        hw_name = HW_STATE_NAMES.get(hw_state, f"?{hw_state}")
        display_state = sw_state.upper() if sw_state != "idle" else hw_name.upper()

        color_key = sw_state if sw_state in COLORS else "idle"
        if hw_state == 5:
            color_key = "unreachable"
        elif hw_state == 2 and sw_state == "idle":
            color_key = "traffic"

        self.state_label.setText(display_state)
        self.state_label.setStyleSheet(
            f"background-color: {COLORS.get(color_key, COLORS['idle'])}; "
            "color: white; border-radius: 4px; padding: 4px;"
        )

        # Header glow for active lines
        if sw_state not in ("idle", "") and hw_state != 0:
            self.header.setStyleSheet(
                f"color: {COLORS.get(color_key, '#fff')}; padding: 6px;"
            )
        else:
            self.header.setStyleSheet("color: #888; padding: 6px;")

        # Info
        self.hw_label.setText(hw_name)
        self.dialed_label.setText(dialed if dialed else "—")
        self.sip_label.setText(sip_info if sip_info else "—")
        self.mode_label.setText(mode if mode else "—")

        # Audio bars (scale rx/tx words to 0-100, ~1000 words/s is full)
        self.rx_bar.setValue(min(int(rx_words / 10), 100))
        self.tx_bar.setValue(min(int(tx_words / 10), 100))


# ============================================================================
# Log Widget
# ============================================================================

class LogWidget(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.setMaximumHeight(150)
        self.setStyleSheet(
            "background-color: #1a1a1a; color: #ccc; border: 1px solid #333;"
        )

    def log(self, msg, color="#ccc"):
        timestamp = time.strftime("%H:%M:%S")
        self.append(f'<span style="color:#666">[{timestamp}]</span> '
                     f'<span style="color:{color}">{msg}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


# ============================================================================
# Main Window
# ============================================================================

class Updater(QObject):
    """Signal bridge for thread-safe GUI updates."""
    log_signal = Signal(str, str)
    status_signal = Signal(list)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DNVT Flashpoint")
        self.setMinimumSize(960, 420)

        # Dark theme
        self._apply_dark_theme()

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)

        # Title bar
        title = QLabel("DNVT FLASHPOINT")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Consolas", 20, QFont.Bold))
        title.setStyleSheet("color: #4CAF50; padding: 4px;")
        main_layout.addWidget(title)

        subtitle = QLabel("SIP Bridge & Management System")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setFont(QFont("Consolas", 10))
        subtitle.setStyleSheet("color: #888;")
        main_layout.addWidget(subtitle)

        # Line widgets
        lines_layout = QHBoxLayout()
        lines_layout.setSpacing(8)
        self.line_widgets = []
        for i in range(4):
            lw = LineWidget(i)
            lines_layout.addWidget(lw)
            self.line_widgets.append(lw)
        main_layout.addLayout(lines_layout)

        # Log
        self.log_widget = LogWidget()
        main_layout.addWidget(self.log_widget)

        # Status bar
        self.status_bar = QStatusBar()
        self.status_bar.setFont(QFont("Consolas", 9))
        self.status_bar.setStyleSheet("color: #888;")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Initializing...")

        # Updater for thread-safe signals
        self.updater = Updater()
        self.updater.log_signal.connect(self.log_widget.log)

        # Bridge state
        self.bridge_ok = False
        self._init_bridge()

        # Poll timer
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(100)  # 10Hz GUI update

    def _apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(30, 30, 30))
        palette.setColor(QPalette.WindowText, QColor(204, 204, 204))
        palette.setColor(QPalette.Base, QColor(20, 20, 20))
        palette.setColor(QPalette.AlternateBase, QColor(40, 40, 40))
        palette.setColor(QPalette.Text, QColor(204, 204, 204))
        palette.setColor(QPalette.Button, QColor(50, 50, 50))
        palette.setColor(QPalette.ButtonText, QColor(204, 204, 204))
        self.setPalette(palette)

    def _init_bridge(self):
        try:
            rc = bridge.init()
            if rc == 0:
                self.bridge_ok = True
                self.log_widget.log("Bridge initialized — USB device connected", "#4CAF50")
                self.status_bar.showMessage("Connected")
            else:
                self.log_widget.log(f"Bridge init failed: {rc}", "#F44336")
                self.status_bar.showMessage("Connection failed")
        except Exception as e:
            self.log_widget.log(f"Bridge error: {e}", "#F44336")
            self.status_bar.showMessage("Error")

    def _poll(self):
        if not self.bridge_ok:
            return

        try:
            statuses = bridge.get_status()
            for i, st in enumerate(statuses):
                state_name = bridge.STATE_NAMES.get(st.state, f"?{st.state}")
                self.line_widgets[i].update_state(
                    hw_state=st.state,
                    sw_state=state_name,
                    rx_words=st.rx_words,
                    tx_words=st.tx_words,
                )

            # Update status bar with packet rates
            total_rx = sum(st.rx_words for st in statuses)
            self.status_bar.showMessage(
                f"Connected  |  RX: {total_rx} words/s  |  "
                f"States: {' '.join(bridge.STATE_NAMES.get(s.state, '?') for s in statuses)}"
            )

        except Exception as e:
            self.log_widget.log(f"Poll error: {e}", "#F44336")

    def closeEvent(self, event):
        if self.bridge_ok:
            bridge.shutdown()
        event.accept()


# ============================================================================
# Entry point
# ============================================================================

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
