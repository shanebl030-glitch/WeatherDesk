"""RBSB-Weather main window."""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QByteArray, QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent, QFont, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QDialog, QPlainTextEdit, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QFileDialog, QScrollArea, QSplitter, QSystemTrayIcon, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import APP_NAME, AUTHOR_CREDIT, COFFEE_TEXT, COFFEE_URL, DATA_CREDIT, __version__, units, weather_codes
from .storage import AUTOSTART_ENTRY, autostart_path   # noqa: F401  (autostart_path is used by tests too)
from .api import OpenMeteoClient
from . import providers
from .providers.manager import ProviderManager
from .models import Forecast, Location
from .storage import REFRESH_CHOICES, ForecastCache, Settings, SettingsStore
from .scene import (AnimationClock, Conditions, WeatherIcon, WeatherScene, conditions_from_forecast,
                    hourly_at, local_time, playback_seconds)
from .playback import PlaybackBar
from .desktop_widget import DesktopWidget
from .moon_panel import MoonPanel, MoonReport
from .earth_panel import EarthPanel, Marker
from .globe_panel import GlobePanel
from .wind_panel import WindPanel
from .widgets import HourlyChart, app_icon, render_tray_icon
from .workers import Bridge, Worker, retire
from . import forecast_strip

log = logging.getLogger(__name__)

STALE_AFTER_S = 10 * 60          # selecting a location re-fetches if older than this
OLD_DATA_WARN_S = 3 * 60 * 60    # status bar warns if shown data is older than this
RETRY_AFTER_FAIL_S = 5 * 60      # longest wait before retrying a failed update (not a full interval)
RETRY_STEPS_S = (30, 60, 120, RETRY_AFTER_FAIL_S)   # waits after the 1st, 2nd, 3rd, later failures in a row
SEARCH_DEBOUNCE_MS = 450
# At startup every saved location is updated, not just the selected one (1.16). The others
# go one at a time so a small free allowance isn't hit by a burst; a location updated in the
# last couple of minutes (a quick restart) is skipped; and at boot, when the network often
# isn't up yet, a failed one is retried on the usual steps (30 s, 1, 2, 5 min) then left.
STARTUP_STAGGER_MS = 1500
STARTUP_FRESH_S = 2 * 60
DAYS_SHOWN = 7


def _hhmm(iso: Optional[str]) -> str:
    return units.fmt_iso_clock(iso)


def _day_label(date_str: str, today: Optional[dt.date]) -> str:
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        return date_str
    if today is not None:
        if d == today:
            return "Today"
        if d == today + dt.timedelta(days=1):
            return "Tomorrow"
    return d.strftime("%a %d %b")


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


class MainWindow(QMainWindow):
    def __init__(self, client: Optional[OpenMeteoClient] = None,
                 store: Optional[SettingsStore] = None,
                 cache: Optional[ForecastCache] = None,
                 enable_tray: bool = True, update_client=None, wind_client=None):
        super().__init__()
        self.store = store or SettingsStore()
        self.cache = cache or ForecastCache()
        self.settings: Settings = self.store.load()
        self.client: ProviderManager = self._make_manager(client)
        self._providers_dialog = None
        units.set_time_format(self.settings.time_format)
        self.bridge = Bridge()          # no parent: see workers.retire()
        self.worker = Worker(self.client, self.bridge)
        self.forecasts: dict[str, Forecast] = {}
        self.errors: dict[str, str] = {}          # last error per location key
        self._pending: dict[str, int] = {}         # location key -> token in flight
        self._pending_since: dict[str, float] = {}
        self._token = 0
        self._search_token = 0
        self._quitting = False
        self._tray_allowed = enable_tray and QSystemTrayIcon.isSystemTrayAvailable()
        self.tray: Optional[QSystemTrayIcon] = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1000, 700)
        self.setMinimumSize(QSize(760, 560))

        self._build_ui()
        self._build_menus()
        self._build_tray()

        self.bridge.forecast_ok.connect(self._on_forecast_ok)
        self.bridge.forecast_failed.connect(self._on_forecast_failed)
        self.bridge.search_ok.connect(self._on_search_ok)
        self.bridge.search_failed.connect(self._on_search_failed)
        self.bridge.diag_line.connect(self._on_diag_line)
        self.bridge.diag_done.connect(self._on_diag_done)
        self.bridge.moon_ok.connect(self._on_moon_ok)
        self.bridge.moon_saved.connect(self._on_moon_saved)
        self.bridge.earth_ok.connect(self._on_earth_ok)
        self._earth_token = 0
        self._moon_token = 0
        self._moon_key: Optional[tuple] = None
        self._diag_dialog: Optional[QDialog] = None
        self._diag_running = False

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self.search_timer.timeout.connect(self._run_search)

        self._last_attempt: dict[str, float] = {}
        self._startup_due: dict[str, float] = {}     # location key -> when to try (startup round)
        self._startup_tries: dict[str, int] = {}     # location key -> attempts so far
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.timeout.connect(self._startup_step)
        self._fail_count: dict[str, int] = {}      # failed updates in a row, per location
        # a failed update is retried soon (30 s, 1, 2, then every 5 min) rather than
        # waiting for the next scheduled refresh
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry_failed)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_selected)
        self._apply_refresh_interval()

        # "Updated x min ago" ticks over without network traffic.
        self.age_timer = QTimer(self)
        self.age_timer.setInterval(60_000)
        self.age_timer.timeout.connect(self._check_refresh_due)  # wall clock: survives sleep
        self.age_timer.timeout.connect(self._update_status)
        self.age_timer.timeout.connect(self._update_scene)      # keeps the sun moving
        self.age_timer.timeout.connect(self.request_moon)       # refreshes moon data every 10 min
        self.age_timer.timeout.connect(self._maybe_check_updates)  # once a day (2.1)
        self.age_timer.start()

        # ---- check for updates (2.1, see updates.py)
        from .updates import UpdateClient
        self.update_client = update_client or UpdateClient()
        self._update_token = 0                # current check
        self._update_dl_token = 0             # current download (separate, so a check can't cancel it)
        self._update_checking = False
        self._update_manual = False
        self._update_info = None              # a newer release we know about
        self._update_dialog = None
        self._update_cancel = None            # threading.Event of a download in progress
        self._started_mono = time.monotonic()
        self.open_file = self._open_with_desktop   # replaced in tests (never launches anything)
        self.open_url = self._open_url_in_browser  # likewise (Help → Buy Me a Coffee)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.wind_ok.connect(self._on_wind_ok)
        self._wind_store = None
        self._wind_client = wind_client
        self._wind_busy = False
        self._wind_token = 0
        self.bridge.update_progress.connect(self._on_update_progress)
        self.bridge.update_downloaded.connect(self._on_update_downloaded)

        if self.settings.window_geometry:
            try:
                self.restoreGeometry(QByteArray.fromHex(self.settings.window_geometry.encode()))
            except Exception:
                pass

        # Load cached forecasts so something useful shows immediately, even offline.
        for loc in self.settings.locations:
            fc = self.cache.load(loc)
            if fc is not None:
                self.forecasts[loc.key] = fc
        self.cache.clean_orphans(self.settings.locations)

        self.desktop_widget: Optional[DesktopWidget] = None
        AnimationClock.instance().set_enabled(self.settings.animations)
        self._populate_locations()
        self.render()
        if self.settings.desktop_widget:
            self.set_desktop_widget(True)
        if self.store.last_error:
            QTimer.singleShot(0, lambda: self.statusBar().showMessage(self.store.last_error, 15000))
        QTimer.singleShot(0, self.refresh_selected)
        QTimer.singleShot(0, self.start_startup_round)
        QTimer.singleShot(0, self._restore_known_update)

    # ================================================================ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)

        # --- search bar
        bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search for a city or town…  (Ctrl+L)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textEdited.connect(self._on_search_text)
        self.search_edit.returnPressed.connect(self._on_search_enter)
        bar.addWidget(self.search_edit, 1)
        self.refresh_btn = QPushButton("⟳ Refresh")
        self.refresh_btn.setToolTip("Refresh now (F5)")
        self.refresh_btn.clicked.connect(lambda: self.refresh_selected(force=True))
        bar.addWidget(self.refresh_btn)
        root.addLayout(bar)

        self.search_status = QLabel("")
        self.search_status.setVisible(False)
        root.addWidget(self.search_status)
        self.results = QListWidget()
        self.results.setMaximumHeight(180)
        self.results.setVisible(False)
        self.results.itemActivated.connect(self._on_result_chosen)
        self.results.itemClicked.connect(self._on_result_chosen)
        root.addWidget(self.results)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # --- saved locations sidebar
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        hdr = QLabel("Saved locations")
        hdr.setStyleSheet("font-weight: bold;")
        sl.addWidget(hdr)
        self.loc_list = QListWidget()
        self.loc_list.currentItemChanged.connect(self._on_location_changed)
        sl.addWidget(self.loc_list, 1)
        btns = QHBoxLayout()
        self.up_btn = QToolButton(text="▲", toolTip="Move up")
        self.down_btn = QToolButton(text="▼", toolTip="Move down")
        self.remove_btn = QToolButton(text="Remove", toolTip="Remove the selected location")
        self.up_btn.clicked.connect(lambda: self._move_selected(-1))
        self.down_btn.clicked.connect(lambda: self._move_selected(1))
        self.remove_btn.clicked.connect(self.remove_selected)
        btns.addWidget(self.up_btn)
        btns.addWidget(self.down_btn)
        btns.addStretch(1)
        btns.addWidget(self.remove_btn)
        sl.addLayout(btns)
        split.addWidget(side)

        # --- main weather area
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(6, 0, 0, 0)

        card = QFrame()
        card.setObjectName("card")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        cl = QHBoxLayout(card)
        cl.setContentsMargins(6, 6, 6, 6)
        # Animated sky; the current-conditions text sits on top of it.
        self.scene = WeatherScene(seed=7, scrim=True, rounded=10)
        self.scene.setMinimumSize(300, 200)
        left = QVBoxLayout(self.scene)
        left.setContentsMargins(16, 12, 16, 12)
        self.place_label = QLabel()
        f = QFont(self.font()); f.setPointSizeF(f.pointSizeF() * 1.4); f.setBold(True)
        self.place_label.setFont(f)
        self.place_label.setWordWrap(True)
        left.addWidget(self.place_label)
        self.symbol_label = QLabel(self.scene)      # kept for compatibility; the scene shows the sky
        self.symbol_label.hide()
        self.temp_label = QLabel()
        f = QFont(self.font()); f.setPointSizeF(f.pointSizeF() * 3.6); f.setBold(True)
        self.temp_label.setFont(f)
        left.addWidget(self.temp_label)
        self.desc_label = QLabel()
        f = QFont(self.font()); f.setPointSizeF(f.pointSizeF() * 1.2)
        self.desc_label.setFont(f)
        left.addWidget(self.desc_label)
        left.addStretch(1)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        left.addWidget(self.message_label)
        for lbl in (self.place_label, self.temp_label, self.desc_label, self.message_label):
            lbl.setStyleSheet("color: white; background: transparent;")
        # the mini forecast in the scene's bottom-right corner keeps clear of this text
        self.scene.avoid_widgets = [self.place_label, self.temp_label, self.desc_label, self.message_label]
        cl.addWidget(self.scene, 3)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        self.detail: dict[str, QLabel] = {}
        names = [("feels", "Feels like"), ("humidity", "Humidity"), ("wind", "Wind"),
                 ("gusts", "Gusts"), ("pressure", "Pressure"), ("cloud", "Cloud cover"),
                 ("precip", "Precipitation"), ("uv", "UV index (max)"),
                 ("sunrise", "Sunrise"), ("sunset", "Sunset")]
        for i, (key, title) in enumerate(names):
            t = QLabel(title)
            t.setStyleSheet("color: palette(placeholder-text);")
            v = QLabel("–")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(t, i // 2, (i % 2) * 2)
            grid.addWidget(v, i // 2, (i % 2) * 2 + 1)
            self.detail[key] = v
        cl.addLayout(grid, 2)
        ml.addWidget(card)
        # forecast playback (1.18): play or scrub the next 24 hours in the sky above
        self.playback = PlaybackBar()
        self.playback.offset_changed.connect(self._on_playback)
        ml.addWidget(self.playback)

        # ---- tabs: Forecast | Moon
        self.tabs = QTabWidget()
        fpage = QWidget()
        fl = QVBoxLayout(fpage)
        fl.setContentsMargins(4, 6, 4, 4)
        h = QLabel("Next 24 hours")
        h.setStyleSheet("font-weight: bold;")
        fl.addWidget(h)
        self.chart = HourlyChart()
        fl.addWidget(self.chart, 1)

        h = QLabel(f"{DAYS_SHOWN}-day forecast")
        h.setStyleSheet("font-weight: bold;")
        fl.addWidget(h)
        days = QFrame()
        days.setFrameShape(QFrame.Shape.StyledPanel)
        dg = QGridLayout(days)
        dg.setVerticalSpacing(4)
        self.day_rows: list[list[QLabel]] = []
        self.day_icons: list[WeatherIcon] = []
        for r in range(DAYS_SHOWN):
            labels = [QLabel() for _ in range(6)]
            labels[3].setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            icon = WeatherIcon(30, phase=r * 0.37)
            self.day_icons.append(icon)
            for c, lbl in enumerate(labels):
                if c == 1:                       # column 1 shows the animated icon instead
                    lbl.setParent(days)
                    lbl.hide()
                    dg.addWidget(icon, r, c)
                else:
                    dg.addWidget(lbl, r, c)
            self.day_rows.append(labels)
        dg.setColumnStretch(2, 1)
        fl.addWidget(days)
        self.tabs.addTab(fpage, "Forecast")
        self.moon_panel = MoonPanel()
        self.moon_panel.save_requested.connect(self.save_moon_image_dialog)
        # scrolls instead of squashing when the window is short
        self.moon_scroll = QScrollArea()
        self.moon_scroll.setWidgetResizable(True)
        self.moon_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.moon_scroll.setWidget(self.moon_panel)
        self.tabs.addTab(self.moon_scroll, "🌙 Moon")
        self.earth_panel = EarthPanel()
        self.earth_panel.location_clicked.connect(self._on_earth_location_clicked)
        self.earth_panel.compose_requested.connect(self.request_earth)
        self.tabs.addTab(self.earth_panel, "🗺 Earth Flat")
        self.globe_panel = GlobePanel()
        self.globe_panel.location_clicked.connect(self._on_earth_location_clicked)
        self.globe_panel.compose_requested.connect(self.request_earth)
        self.tabs.addTab(self.globe_panel, "🌐 Earth Globe")
        self.wind_panel = WindPanel()                       # world wind on the globe (2.2)
        self.wind_panel.location_clicked.connect(self._on_earth_location_clicked)
        self.wind_panel.refresh_requested.connect(self.request_wind)
        self.wind_panel.set_wind_unit(self.settings.wind_unit)
        # streaks move only when both View → Animations and the tab's "Moving streaks" are on
        self.wind_panel.streaks_box.toggled.disconnect()
        self.wind_panel.streaks_box.toggled.connect(
            lambda on: self.wind_panel.view.set_animate(on and self.settings.animations))
        self.wind_panel.view.set_animate(self.settings.animations)
        self.tabs.addTab(self.wind_panel, "🌬 Wind")
        from .clouds import CloudStore
        self.cloud_store = CloudStore(self.cache.dir)
        for panel in (self.earth_panel, self.globe_panel):
            panel.clouds_box.setChecked(self.settings.earth_clouds)
            panel.clouds_toggled.connect(self.set_earth_clouds)
        ml.addWidget(self.tabs, 1)
        split.addWidget(main)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 770])

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self.update_button = QPushButton()      # "Update available: x.y.z" (2.1)
        self.update_button.setFlat(True)
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setToolTip("A newer version of RBSB-Weather is available. Click to see what's new.")
        self.update_button.clicked.connect(lambda: self.show_update_dialog(self._update_info))
        self.update_button.setVisible(False)
        self.statusBar().addPermanentWidget(self.update_button)
        credit = QLabel(AUTHOR_CREDIT)
        credit.setStyleSheet("color: palette(placeholder-text); padding-right: 6px;")
        self.statusBar().addPermanentWidget(credit)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self.act_refresh = QAction("&Refresh", self, shortcut=QKeySequence("F5"),
                                   triggered=lambda: self.refresh_selected(force=True))
        m.addAction(self.act_refresh)
        m.addAction(QAction("&Find location…", self, shortcut=QKeySequence("Ctrl+L"),
                            triggered=self._focus_search))
        m.addSeparator()
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence("Ctrl+Q"), triggered=self.quit_app))

        v = mb.addMenu("&View")
        self.unit_actions: dict[str, dict[str, QAction]] = {}
        for attr, title, choices, labels in (
            ("temp_unit", "&Temperature", units.TEMP_UNITS, {"C": "Celsius (°C)", "F": "Fahrenheit (°F)"}),
            ("wind_unit", "&Wind speed", units.WIND_UNITS,
             {"km/h": "km/h", "m/s": "m/s", "mph": "mph", "kn": "knots"}),
            ("precip_unit", "&Precipitation", units.PRECIP_UNITS, {"mm": "millimetres", "in": "inches"}),
            ("time_format", "T&ime format", units.TIME_FORMATS,
             {"24": "24-hour (19:30)", "12": "12-hour (7:30 pm)"}),
        ):
            sub = v.addMenu(title)
            grp = QActionGroup(self)
            self.unit_actions[attr] = {}
            for ch in choices:
                a = QAction(labels[ch], self, checkable=True)
                a.setChecked(getattr(self.settings, attr) == ch)
                a.triggered.connect(lambda _=False, at=attr, c=ch: self.set_unit(at, c))
                grp.addAction(a)
                sub.addAction(a)
                self.unit_actions[attr][ch] = a

        sub = v.addMenu("&Auto refresh")
        grp = QActionGroup(self)
        self.refresh_actions: dict[int, QAction] = {}
        for mins in REFRESH_CHOICES:
            a = QAction("Off" if mins == 0 else f"Every {mins} minutes", self, checkable=True)
            a.setChecked(self.settings.refresh_minutes == mins)
            a.triggered.connect(lambda _=False, m_=mins: self.set_refresh_minutes(m_))
            grp.addAction(a)
            sub.addAction(a)
            self.refresh_actions[mins] = a
        sub.addSeparator()
        self.act_startup_all = QAction("Update all locations at &startup", self, checkable=True,
                                       checked=self.settings.startup_update_all,
                                       triggered=self.set_startup_update_all)
        sub.addAction(self.act_startup_all)
        v.addSeparator()
        self.act_tray = QAction("Show &tray icon", self, checkable=True,
                                checked=self.settings.show_tray, triggered=self.set_show_tray)
        self.act_tray.setEnabled(self._tray_allowed)
        v.addAction(self.act_tray)
        self.act_close_tray = QAction("&Close to tray", self, checkable=True,
                                      checked=self.settings.close_to_tray,
                                      triggered=self.set_close_to_tray)
        self.act_close_tray.setEnabled(self._tray_allowed)
        v.addAction(self.act_close_tray)
        v.addSeparator()
        self.act_anim = QAction("&Animated weather", self, checkable=True,
                                checked=self.settings.animations, triggered=self.set_animations)
        v.addAction(self.act_anim)
        self.act_widget = QAction("&Desktop widget", self, checkable=True,
                                  checked=self.settings.desktop_widget,
                                  triggered=self.set_desktop_widget)
        v.addAction(self.act_widget)
        v.addSeparator()
        self.act_autostart = QAction("Start at &login (minimised)", self, checkable=True,
                                     checked=self._autostart_on(),
                                     triggered=self.set_autostart)
        v.addAction(self.act_autostart)

        sm = mb.addMenu("&Settings")
        self.act_providers = QAction("Weather &Providers…", self, shortcut=QKeySequence("Ctrl+,"),
                                     triggered=self.show_providers)
        sm.addAction(self.act_providers)
        sm.addSeparator()
        self.act_update_auto = QAction("Check for &updates automatically", self, checkable=True,
                                       checked=self.settings.update_check,
                                       triggered=self.set_update_check)
        sm.addAction(self.act_update_auto)

        hm = mb.addMenu("&Help")
        hm.addAction(QAction("&User Guide", self, shortcut=QKeySequence("F1"), triggered=self.show_guide))
        hm.addAction(QAction("&Test connection…", self, triggered=self.show_connection_test))
        hm.addAction(QAction("Check for &Updates…", self, triggered=self.check_for_updates))
        hm.addSeparator()
        hm.addAction(QAction("☕ Buy Me a &Coffee…", self, triggered=self.buy_coffee))
        hm.addAction(QAction("&About RBSB-Weather", self, triggered=self.show_about))

    def _build_tray(self) -> None:
        if not self._tray_allowed:
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu(self)
        menu.addAction("Show / Hide", self.toggle_visible)
        menu.addAction("Refresh", lambda: self.refresh_selected(force=True))
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(lambda: self._update_info and self.show_update_dialog(self._update_info))
        self.tray.setVisible(self.settings.show_tray)

    # ======================================================= Locations
    def _populate_locations(self) -> None:
        self.loc_list.blockSignals(True)
        try:
            self.loc_list.clear()
            sel = self.settings.selected_location()
            for loc in self.settings.locations:
                it = QListWidgetItem(self._location_item_text(loc))
                it.setData(Qt.ItemDataRole.UserRole, loc.key)
                it.setToolTip(f"{loc.display_name}\n{loc.latitude:.3f}, {loc.longitude:.3f}")
                self.loc_list.addItem(it)
                if sel and loc.key == sel.key:
                    self.loc_list.setCurrentItem(it)
        finally:
            self.loc_list.blockSignals(False)
        has = bool(self.settings.locations)
        for b in (self.up_btn, self.down_btn, self.remove_btn, self.refresh_btn):
            b.setEnabled(has)
        self.act_refresh.setEnabled(has)

    def _location_item_text(self, loc: Location) -> str:
        fc = self.forecasts.get(loc.key)
        if fc is None:
            return loc.name
        return (f"{weather_codes.symbol(fc.current.weather_code, fc.current.is_day)}  "
                f"{loc.name}   {units.fmt_temp(fc.current.temperature, self.settings.temp_unit)}")

    def _refresh_location_texts(self) -> None:
        for i in range(self.loc_list.count()):
            it = self.loc_list.item(i)
            key = it.data(Qt.ItemDataRole.UserRole)
            loc = next((l for l in self.settings.locations if l.key == key), None)
            if loc:
                it.setText(self._location_item_text(loc))

    def _on_location_changed(self, cur: Optional[QListWidgetItem], _prev) -> None:
        if cur is None:
            return
        self.select_location(cur.data(Qt.ItemDataRole.UserRole))

    def select_location(self, key: str) -> None:
        if not any(l.key == key for l in self.settings.locations):
            return
        self.settings.selected = key
        self.save_settings()
        self.playback.reset()                      # a new place starts at now
        self.render()
        fc = self.forecasts.get(key)
        if fc is None or fc.from_cache or time.time() - fc.fetched_at > STALE_AFTER_S:
            self.refresh_selected()

    def add_location(self, loc: Location) -> None:
        try:
            self.settings.add_location(loc)
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.save_settings()
        cached = self.cache.load(loc)
        if cached and loc.key not in self.forecasts:
            self.forecasts[loc.key] = cached
        self._populate_locations()
        self.render()
        self.refresh_selected()

    def remove_selected(self) -> None:
        loc = self.settings.selected_location()
        if loc is None:
            return
        self.settings.remove_location(loc.key)
        self.forecasts.pop(loc.key, None)
        self.errors.pop(loc.key, None)
        self._pending.pop(loc.key, None)
        self.cache.remove(loc)
        self.save_settings()
        self._populate_locations()
        self.render()
        self.statusBar().showMessage(f"Removed {loc.name}", 5000)
        nxt = self.settings.selected_location()
        if nxt and nxt.key not in self.forecasts:
            self.refresh_selected()

    def _move_selected(self, delta: int) -> None:
        loc = self.settings.selected_location()
        if loc and self.settings.move_location(loc.key, delta):
            self.save_settings()
            self._populate_locations()

    # ========================================================== Search
    def _focus_search(self) -> None:
        self.show_normal()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _on_search_text(self, text: str) -> None:
        if len(text.strip()) < 2:
            self.search_timer.stop()
            self._search_token += 1          # invalidate in-flight searches
            self._hide_results()
            return
        self.search_timer.start()

    def _on_search_enter(self) -> None:
        if self.results.isVisible() and self.results.count():
            item = self.results.currentItem() or self.results.item(0)
            self._on_result_chosen(item)
        elif len(self.search_edit.text().strip()) >= 2:
            self.search_timer.stop()
            self._run_search()

    def _run_search(self) -> None:
        q = self.search_edit.text().strip()
        if len(q) < 2:
            return
        self._search_token += 1
        self.search_status.setText(f"Searching for “{q}”…")
        self.search_status.setVisible(True)
        self.worker.search(self._search_token, q)

    def _on_search_ok(self, token: int, results: list) -> None:
        if token != self._search_token:
            return
        self.results.clear()
        if not results:
            self.search_status.setText("No places found. Check the spelling or try a nearby town.")
            self.search_status.setVisible(True)
            self.results.setVisible(False)
            return
        self.search_status.setVisible(False)
        for loc in results:
            it = QListWidgetItem(f"{loc.display_name}   ({loc.latitude:.2f}, {loc.longitude:.2f})")
            it.setData(Qt.ItemDataRole.UserRole, loc)
            self.results.addItem(it)
        self.results.setCurrentRow(0)
        self.results.setVisible(True)

    def _on_search_failed(self, token: int, msg: str) -> None:
        if token != self._search_token:
            return
        self.results.clear()
        self.results.setVisible(False)
        self.search_status.setText(f"Search failed: {msg}")
        self.search_status.setVisible(True)

    def _on_result_chosen(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            return
        loc = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(loc, Location):
            return
        self.search_edit.clear()
        self._search_token += 1
        self._hide_results()
        self.add_location(loc)

    def _hide_results(self) -> None:
        self.results.clear()
        self.results.setVisible(False)
        self.search_status.setVisible(False)

    # ====================================================== Forecasts
    def refresh_selected(self, force: bool = False) -> None:
        loc = self.settings.selected_location()
        if loc is None:
            return
        self._fetch(loc)

    def _fetch(self, loc: Location) -> bool:
        """Ask for one location's forecast. False if it's already being fetched."""
        if loc.key in self._pending and time.time() - self._pending_since.get(loc.key, 0) < 60:
            return False  # already in flight; a duplicate request adds nothing (even if forced)
        # (after 60 s a request is treated as lost and superseded - a safety net only)
        self._token += 1
        self._pending[loc.key] = self._token
        self._pending_since[loc.key] = time.time()
        self._last_attempt[loc.key] = time.time()
        self.worker.fetch_forecast(self._token, loc)
        sel = self.settings.selected_location()
        if sel is not None and sel.key == loc.key and loc.key not in self.forecasts:
            self.render()          # show "Loading weather…" instead of "No data"
        else:
            self._update_status()
        return True

    # ----------------------------------------------------- startup round
    def start_startup_round(self) -> None:
        """Queue every saved location except the selected one (which updates straight
        away) for one update each, unless the setting is off."""
        self._startup_due.clear()
        self._startup_tries.clear()
        if not self.settings.startup_update_all or getattr(self, "_quitting", False):
            return
        sel = self.settings.selected_location()
        now = time.time()
        for loc in self.settings.locations:
            if sel is not None and loc.key == sel.key:
                continue
            fc = self.forecasts.get(loc.key)
            if fc is not None and fc.fetched_at and now - fc.fetched_at < STARTUP_FRESH_S:
                continue                           # updated moments ago (a quick restart)
            self._startup_due[loc.key] = now
            self._startup_tries[loc.key] = 0
        if self._startup_due:
            log.info("startup: updating %d other saved location(s)", len(self._startup_due))
            self.startup_timer.start(STARTUP_STAGGER_MS)

    def _startup_step(self) -> None:
        """Startup timer: fetch the next location that's due, then wait again."""
        if getattr(self, "_closed", False):
            self._startup_due.clear()
            return
        keys = [l.key for l in self.settings.locations]
        for k in list(self._startup_due):
            if k not in keys:                      # removed meanwhile
                self._startup_due.pop(k)
                self._startup_tries.pop(k, None)
        now = time.time()
        due = [k for k in keys if self._startup_due.get(k, float("inf")) <= now and k not in self._pending]
        if due:
            k = due[0]
            loc = next(l for l in self.settings.locations if l.key == k)
            del self._startup_due[k]
            self._startup_tries[k] = self._startup_tries.get(k, 0) + 1
            self._fetch(loc)
        self._schedule_startup_step()

    def _schedule_startup_step(self) -> None:
        if not self._startup_due:
            return
        wait = (min(self._startup_due.values()) - time.time()) * 1000
        self.startup_timer.start(int(max(STARTUP_STAGGER_MS, wait)))

    def _startup_result(self, key: str, ok: bool) -> None:
        """Note how a startup-round fetch went; a failure is retried a few times."""
        if key not in self._startup_tries or key in self._startup_due:
            return
        if ok:
            self._startup_tries.pop(key, None)
            return
        n = self._startup_tries[key]
        if n <= len(RETRY_STEPS_S):
            self._startup_due[key] = time.time() + RETRY_STEPS_S[n - 1]
            self._schedule_startup_step()
        else:
            self._startup_tries.pop(key, None)
            log.info("startup: gave up on %s after %d tries", key, n)

    def _on_forecast_ok(self, token: int, fc: Forecast) -> None:
        key = fc.location.key
        if self._pending.get(key) != token:
            return                          # stale or location removed
        del self._pending[key]
        self._startup_result(key, True)
        # The user may have renamed/changed metadata; keep the saved Location object.
        saved = next((l for l in self.settings.locations if l.key == key), None)
        if saved is None:
            return
        fc.location = saved
        self.forecasts[key] = fc
        self.errors.pop(key, None)
        self._fail_count.pop(key, None)
        if self._providers_dialog is not None:
            self._providers_dialog.refresh_status()
        self.cache.save(fc)
        self._refresh_location_texts()
        sel = self.settings.selected_location()
        if sel and sel.key == key:
            self.render()
        else:
            self.update_earth_markers()
            self._update_status()

    def _on_forecast_failed(self, token: int, key: str, msg: str) -> None:
        if self._pending.get(key) != token:
            return
        del self._pending[key]
        self._startup_result(key, False)
        self.errors[key] = msg
        self._fail_count[key] = self._fail_count.get(key, 0) + 1
        if self._providers_dialog is not None:
            self._providers_dialog.refresh_status()
        log.info("forecast failed for %s (%d in a row): %s", key, self._fail_count[key], msg)
        if self.settings.refresh_minutes > 0:
            self.retry_timer.start(int(self._retry_delay(key) * 1000))
        sel = self.settings.selected_location()
        if sel and sel.key == key:
            self.render()

    # ========================================================= Render
    def render(self) -> None:
        s = self.settings
        loc = s.selected_location()
        fc = self.forecasts.get(loc.key) if loc else None
        self.setWindowTitle(f"{APP_NAME} — {loc.name}" if loc else APP_NAME)

        self.request_moon()
        self.update_earth_markers()
        if loc is None:
            self.place_label.setText("Welcome to RBSB-Weather")
            self.symbol_label.setText("🌤️")
            self.temp_label.setText("")
            self.desc_label.setText("Search for a city or town above to get started.")
            self.message_label.setText("")
            self._clear_details()
            self.scene.set_conditions(Conditions())
            self.scene.set_forecast([], None, s.temp_unit)
            self._stop_playback()
            self._update_tray(None)
            self._update_widget(None)
            self._update_status()
            return

        self.place_label.setText(loc.display_name)
        err = self.errors.get(loc.key)
        if fc is None:
            self.symbol_label.setText("⏳" if loc.key in self._pending else "⚠️")
            self.temp_label.setText("")
            self.desc_label.setText("Loading weather…" if loc.key in self._pending
                                    else "No weather data yet.")
            self.message_label.setText(err or "")
            self._clear_details()
            self.scene.set_conditions(Conditions())
            self.scene.set_forecast([], None, s.temp_unit)
            self._stop_playback()
            self._update_tray(None)
            self._update_widget(None)
            self._update_status()
            return

        c = fc.current
        self.scene.set_forecast(forecast_strip.days_from_forecast(fc), forecast_strip.location_today(fc),
                                s.temp_unit)
        self.symbol_label.setText(weather_codes.symbol(c.weather_code, c.is_day))
        self.temp_label.setText(units.fmt_temp(c.temperature, s.temp_unit))
        self.desc_label.setText(weather_codes.describe(c.weather_code))
        age = time.time() - fc.fetched_at if fc.fetched_at else None
        old = age is None or age > OLD_DATA_WARN_S
        if err:
            shown = f"Showing weather from {_age_text(age)}" if age is not None else "Showing saved weather"
            retry = "; retrying automatically" if self.settings.refresh_minutes > 0 else ""
            text = f"⚠️ Couldn't update: {err.rstrip('.')}. {shown}{retry}."
        else:
            text = f"Local time at location: {_hhmm(c.time)}" if c.time else ""
        self.message_label.setText(text)
        # recent data still on screen: a quiet note; old data: the bold warning
        self.message_label.setStyleSheet(
            "color: #ffd2c8; background: transparent; font-weight: bold;" if err and old else
            "color: #ffe9e3; background: transparent;" if err else
            "color: white; background: transparent;")
        if self.playback.offset <= 0:              # (during playback the sky shows that hour)
            self.scene.set_conditions(conditions_from_forecast(fc))

        today = fc.daily[0] if fc.daily else None
        d = self.detail
        d["feels"].setText(units.fmt_temp(c.apparent_temperature, s.temp_unit))
        d["humidity"].setText("–" if c.humidity is None else f"{c.humidity:.0f}%")
        wind = units.fmt_wind(c.wind_speed, s.wind_unit)
        direction = units.compass(c.wind_direction)
        d["wind"].setText(f"{wind} {direction}".strip())
        d["gusts"].setText(units.fmt_wind(c.wind_gusts, s.wind_unit))
        d["pressure"].setText("–" if c.pressure is None else f"{c.pressure:.0f} hPa")
        d["cloud"].setText("–" if c.cloud_cover is None else f"{c.cloud_cover:.0f}%")
        d["precip"].setText(units.fmt_precip(c.precipitation, s.precip_unit))
        d["uv"].setText("–" if not today or today.uv_index_max is None else f"{today.uv_index_max:.1f}")
        d["sunrise"].setText(_hhmm(today.sunrise) if today else "–")
        d["sunset"].setText(_hhmm(today.sunset) if today else "–")

        # next 24 hours, starting at the current hour at the location
        start = 0
        cur_hour = c.time[:13] if c.time else ""
        for i, hp in enumerate(fc.hourly):
            if hp.time[:13] >= cur_hour:
                start = i
                break
        self.chart.empty_text = ("No hourly data" if fc.hourly else
                                 f"{fc.source} doesn't provide an hourly forecast")
        self.chart.set_data(fc.hourly[start:start + 24], s.temp_unit)

        try:
            today_date = dt.date.fromisoformat(c.time[:10]) if c.time else None
        except ValueError:
            today_date = None
        for r, labels in enumerate(self.day_rows):
            if r < len(fc.daily):
                dp = fc.daily[r]
                labels[0].setText(_day_label(dp.date, today_date))
                labels[1].setText(weather_codes.symbol(dp.weather_code, True))
                self.day_icons[r].set_code(dp.weather_code, True)
                self.day_icons[r].setToolTip(weather_codes.describe(dp.weather_code))
                labels[2].setText(weather_codes.describe(dp.weather_code))
                labels[3].setText(f"<b>{units.fmt_temp(dp.temp_max, s.temp_unit)}</b> / "
                                  f"{units.fmt_temp(dp.temp_min, s.temp_unit)}")
                pp = dp.precipitation_probability_max
                labels[4].setText(("💧 " + (f"{pp:.0f}%" if pp is not None else "–") +
                                   f"  {units.fmt_precip(dp.precipitation_sum, s.precip_unit)}"))
                labels[5].setText(f"💨 {units.fmt_wind(dp.wind_speed_max, s.wind_unit)}")
            else:
                for lbl in labels:
                    lbl.setText("")
                self.day_icons[r].set_code(None)
        self.scene.clock = time.time
        self._apply_playback(fc)                   # a refresh mid-playback stays on that hour
        self._update_tray(fc)
        self._update_widget(fc)
        self._update_status()

    # ------------------------------------------------- forecast playback (1.18)
    def _stop_playback(self) -> None:
        self.playback.reset()
        self.playback.set_range(0.0, "")
        self.scene.clock = time.time

    def _on_playback(self, offset: float) -> None:
        loc = self.settings.selected_location()
        fc = self.forecasts.get(loc.key) if loc else None
        if fc is None:
            return
        if offset <= 0:
            self.render()                          # back to the live weather, all of it
        else:
            self._apply_playback(fc)               # just the sky and the card's text

    def _apply_playback(self, fc: Forecast) -> None:
        """Show the forecast `self.playback.offset` seconds ahead in the sky and the card
        (offset 0: leave the live view render() drew). The rest of the window, the tray and
        the desktop widget stay on the weather now."""
        if not fc.hourly:
            unavailable = f"{fc.source} doesn't provide an hourly forecast"
        else:
            unavailable = "No hourly forecast ahead"
        self.playback.set_range(playback_seconds(fc), unavailable)
        off = self.playback.offset
        if off <= 0:
            self.playback.set_time_text("Now")
            return
        s = self.settings
        when = local_time(fc)
        when = when + dt.timedelta(seconds=off) if when else None
        hp = hourly_at(fc, when)
        self.scene.clock = lambda o=off: time.time() + o
        self.scene.set_conditions(conditions_from_forecast(fc, offset_s=off))
        stamp = units.fmt_clock(when, day=True) if when else "–"
        self.playback.set_time_text(stamp)
        if hp is None:
            return
        self.temp_label.setText(units.fmt_temp(hp.temperature, s.temp_unit))
        rain = hp.precipitation_probability
        self.desc_label.setText(weather_codes.describe(hp.weather_code) +
                                (f"  ·  💧 {rain:.0f}%" if rain is not None else ""))
        # kept short: a longer line here would push the mini forecast down to fewer days
        self.message_label.setText(f"▶ Forecast for {stamp}")
        self.message_label.setStyleSheet("color: white; background: transparent;")

    def _clear_details(self) -> None:
        for icon in self.day_icons:
            icon.set_code(None)
        for v in self.detail.values():
            v.setText("–")
        self.chart.set_data([], self.settings.temp_unit)
        for labels in self.day_rows:
            for lbl in labels:
                lbl.setText("")

    def _update_status(self) -> None:
        loc = self.settings.selected_location()
        self._update_widget_status(loc)
        if loc is None:
            self.status_label.setText("No location selected")
            return
        parts = []
        if loc.key in self._pending:
            parts.append("Updating…")
        fc = self.forecasts.get(loc.key)
        if fc is not None and fc.fetched_at:
            age = time.time() - fc.fetched_at
            stamp = units.fmt_clock(dt.datetime.fromtimestamp(fc.fetched_at))
            txt = f"Updated {stamp} ({_age_text(age)})"
            if fc.from_cache:
                txt += " · saved copy"
            elif fc.provider_id != self.client.primary:
                txt += f" · from {fc.source} (backup)"
            if age > OLD_DATA_WARN_S:
                txt += " · ⚠️ data may be out of date"
            parts.append(txt)
        if self.errors.get(loc.key):
            parts.append("Offline / update failed")
        parts.append(self._credit(fc))
        self.status_label.setText("   ·   ".join(parts))

    def _update_tray(self, fc: Optional[Forecast]) -> None:
        if self.tray is None:
            return
        if fc is None:
            self.tray.setIcon(app_icon())
            self.tray.setToolTip(APP_NAME)
            return
        c = fc.current
        self.tray.setIcon(render_tray_icon(c.temperature, self.settings.temp_unit, c.weather_code))
        self.tray.setToolTip(f"{fc.location.name}: {units.fmt_temp(c.temperature, self.settings.temp_unit)}"
                             f", {weather_codes.describe(c.weather_code)}")

    # =========================================================== Earth
    def update_earth_markers(self) -> None:
        markers = []
        for loc in self.settings.locations:
            fc = self.forecasts.get(loc.key)
            tz_name = fc.timezone if fc and fc.timezone and fc.timezone != "auto" else loc.timezone
            m = Marker(loc.key, loc.name, loc.latitude, loc.longitude, tz_name,
                       fc.utc_offset_seconds if fc else None)
            if fc is not None:
                m.temp_c, m.code = fc.current.temperature, fc.current.weather_code
                m.desc = weather_codes.describe(fc.current.weather_code)
            markers.append(m)
        sel = self.settings.selected_location()
        self.earth_panel.set_markers(markers, sel.key if sel else None, self.settings.temp_unit)
        self.globe_panel.set_markers(markers, sel.key if sel else None, self.settings.temp_unit)
        self.wind_panel.set_markers(markers, sel.key if sel else None, self.settings.temp_unit)

    def request_earth(self) -> None:
        # one shared day/night image for both the flat map and the globe
        self.earth_panel.last_compose = self.globe_panel.last_compose = time.time()
        self._earth_token += 1
        from . import clouds
        self.worker.earth_compose(self._earth_token,
                                  self.cloud_store if self.settings.earth_clouds else None,
                                  clouds.CLOUD_URL)

    def request_wind(self) -> None:
        """Wind tab shown (and every 5 minutes while it is): the saved world wind straight
        away, and a newer one from NOAA when one is due (every 3 hours)."""
        if self._wind_busy or getattr(self, "_quitting", False):
            return
        if self._wind_store is None:
            from . import wind
            from .storage import cache_dir
            self._wind_store = wind.WindStore(cache_dir() / "wind")
            self._wind_client = self._wind_client or wind.WindClient()
        if self.wind_panel.view.field is None:
            fld = self._wind_store.load()               # instant, even offline
            if fld is not None:
                self.wind_panel.set_field(fld)
        if self.wind_panel.view.field is not None and not self._wind_store.due():
            return
        self._wind_busy = True
        self._wind_token += 1
        self.worker.wind_update(self._wind_token, self._wind_store, self._wind_client)

    def _on_wind_ok(self, token: int, fld, note: str) -> None:
        if token != self._wind_token:
            return
        self._wind_busy = False
        if fld is None:
            if self.wind_panel.view.field is None:
                self.wind_panel.set_field(None, note)
            return
        if fld is not self.wind_panel.view.field:
            self.wind_panel.set_field(fld, note)

    def set_earth_clouds(self, on: bool) -> None:
        """Live clouds on/off (shared by both Earth tabs); redraws the Earth."""
        for panel in (self.earth_panel, self.globe_panel):
            if panel.clouds_box.isChecked() != on:
                panel.clouds_box.blockSignals(True)
                panel.clouds_box.setChecked(on)
                panel.clouds_box.blockSignals(False)
        if self.settings.earth_clouds == on:
            return
        self.settings.earth_clouds = on
        self.save_settings()
        if self.tabs.currentWidget() in (self.earth_panel, self.globe_panel):
            self.request_earth()
        else:                                    # redraw next time a tab is shown
            self.earth_panel.last_compose = self.globe_panel.last_compose = 0.0

    def _on_earth_ok(self, token: int, img, when, error: str, cloud_note: str = "") -> None:
        if token != self._earth_token:
            return
        self.earth_panel.set_image(img, when, error, cloud_note)
        self.globe_panel.set_image(img, when, error, cloud_note)

    def _on_earth_location_clicked(self, key: str) -> None:
        for i in range(self.loc_list.count()):
            if self.loc_list.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.loc_list.setCurrentRow(i)          # same as clicking it in Saved locations
                return

    # ============================================================ Moon
    def request_moon(self, force: bool = False) -> None:
        """Recalculate the Moon tab for the selected place (background thread).
        Skipped when nothing relevant changed in the last 10 minutes."""
        loc = self.settings.selected_location()
        if loc is None:
            self._moon_key = None
            self.moon_panel.clear("Select a location to see the Moon.")
            return
        fc = self.forecasts.get(loc.key)
        tz_name = (fc.timezone if fc and fc.timezone and fc.timezone != "auto" else loc.timezone)
        offset = fc.utc_offset_seconds if fc else None
        key = (loc.key, tz_name, offset, int(time.time() // 600))
        if key == self._moon_key and not force:
            return
        self._moon_key = key
        self._moon_token += 1
        if self.moon_panel.report is None or self.moon_panel.report.location.key != loc.key:
            self.moon_panel.clear("Calculating…")
        self.worker.moon_report(self._moon_token, loc, tz_name, offset)

    def _on_moon_ok(self, token: int, rep) -> None:
        if token != self._moon_token:
            return                                   # an older request; a newer one is coming
        if not isinstance(rep, MoonReport):
            self.moon_panel.clear(str(rep))
            return
        sel = self.settings.selected_location()
        if sel is None or sel.key != rep.location.key:
            return
        self.moon_panel.show_report(rep)

    def save_moon_image_dialog(self) -> None:
        rep = self.moon_panel.report
        if rep is None:
            return
        pictures = Path.home() / "Pictures"
        default = (pictures if pictures.is_dir() else Path.home()) / \
            f"moon-{rep.location.name.replace(' ', '_')}-{rep.now:%Y-%m-%d}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save moon image", str(default), "PNG image (*.png)")
        if path:
            self.save_moon_image(path)

    def save_moon_image(self, path: str) -> None:
        rep = self.moon_panel.report
        if rep is None:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        self.statusBar().showMessage("Rendering 2048 × 2048 moon image…")
        self.moon_panel.save_btn.setEnabled(False)
        self.worker.save_moon(path, rep.phase.phase_angle, rep.phase.bright_limb, rep.southern)

    def _on_moon_saved(self, path: str, error: str) -> None:
        self.moon_panel.save_btn.setEnabled(self.moon_panel.report is not None)
        if error:
            self.statusBar().showMessage(f"Could not save the moon image: {error}", 10000)
        else:
            self.statusBar().showMessage(f"Saved moon image: {path}", 10000)
        self.last_moon_save = (path, error)

    # ============================================ Scene / desktop widget
    def _update_scene(self) -> None:
        """Re-derive sky conditions (sun position, daylight) as time passes."""
        loc = self.settings.selected_location()
        fc = self.forecasts.get(loc.key) if loc else None
        if fc is not None:
            cond = conditions_from_forecast(fc)
            if self.playback.offset > 0:           # the main sky is showing a forecast hour
                self.scene.set_conditions(conditions_from_forecast(fc, offset_s=self.playback.offset))
            else:
                self.scene.set_conditions(cond)
            if self.desktop_widget is not None:
                self.desktop_widget.scene.set_conditions(cond)

    def _update_widget_status(self, loc) -> None:
        """The desktop widget's "↻ 19:30" line: when the shown weather was downloaded."""
        dw = self.desktop_widget
        if dw is None:
            return
        fc = self.forecasts.get(loc.key) if loc else None
        if loc is None or fc is None or not fc.fetched_at:
            dw.set_updated("Updating…" if loc is not None and loc.key in self._pending else "")
            return
        age = time.time() - fc.fetched_at
        stamp = units.fmt_clock(dt.datetime.fromtimestamp(fc.fetched_at))
        if loc.key in self._pending:
            text = f"↻ {stamp} · updating…"
        else:
            text = f"↻ {stamp}"
        mins = self.settings.refresh_minutes
        late = age > max(2 * mins * 60, 90 * 60) if mins > 0 else age > OLD_DATA_WARN_S
        tip = f"Weather updated at {stamp} ({_age_text(age)})"
        if mins > 0:
            tip += f"; updates every {mins} min"
        if fc.from_cache:
            tip += " · saved copy (offline)"
        if self.errors.get(loc.key):
            tip += " · the last update failed; retrying"
        warn = late or bool(self.errors.get(loc.key)) or fc.from_cache
        if warn and loc.key not in self._pending:
            text += f" ({_age_text(age)})"
        dw.set_updated(text, tip, warn)

    def _update_widget(self, fc: Optional[Forecast]) -> None:
        dw = self.desktop_widget
        if dw is None:
            return
        if fc is None:
            dw.set_empty()
            dw.scene.set_conditions(Conditions())
            dw.scene.set_forecast([], None, self.settings.temp_unit)
            return
        s = self.settings
        c = fc.current
        today = fc.daily[0] if fc.daily else None
        hilo = (f"H {units.fmt_temp(today.temp_max, s.temp_unit)}  ·  "
                f"L {units.fmt_temp(today.temp_min, s.temp_unit)}") if today else ""
        dw.set_weather(fc.location.name, units.fmt_temp(c.temperature, s.temp_unit),
                       weather_codes.describe(c.weather_code), hilo, conditions_from_forecast(fc))
        dw.scene.set_forecast(forecast_strip.days_from_forecast(fc), forecast_strip.location_today(fc),
                              s.temp_unit)
        dw.set_source(*self._widget_source_text(fc))

    def _widget_source_text(self, fc: Forecast) -> tuple[str, str]:
        """The desktop widget's small provider line (2.1.2): who supplied this weather."""
        name = fc.source or "the weather service"
        if fc.from_cache:
            return f"{name} · saved copy", f"Last forecast from {name}, saved on this computer (offline)"
        if fc.provider_id != self.client.primary:
            return f"{name} (backup)", (f"Weather from {name}, because the main provider "
                                         f"couldn't be reached")
        return name, f"Weather from {name}"

    def set_animations(self, on: bool) -> None:
        self.settings.animations = bool(on)
        self.act_anim.setChecked(self.settings.animations)
        AnimationClock.instance().set_enabled(self.settings.animations)
        self.wind_panel.view.set_animate(self.settings.animations and self.wind_panel.streaks_box.isChecked())
        self.save_settings()

    def set_desktop_widget(self, on: bool) -> None:
        self.settings.desktop_widget = bool(on)
        self.act_widget.setChecked(self.settings.desktop_widget)
        if on:
            if self.desktop_widget is None:
                dw = DesktopWidget(self.settings.widget_size, self.settings.widget_on_top,
                                   self.settings.widget_locked)
                dw.open_requested.connect(self.show_normal)
                dw.refresh_requested.connect(lambda: self.refresh_selected(force=True))
                dw.hide_requested.connect(lambda: self.set_desktop_widget(False))
                dw.size_changed.connect(self._on_widget_size)
                dw.on_top_changed.connect(self._on_widget_on_top)
                dw.locked_changed.connect(self._on_widget_locked)
                dw.moved.connect(self._on_widget_moved)
                self.desktop_widget = dw
                loc = self.settings.selected_location()
                self._update_widget(self.forecasts.get(loc.key) if loc else None)
                self._update_widget_status(loc)
            self.desktop_widget.place_at(self.settings.widget_x, self.settings.widget_y)
            self.desktop_widget.show()
        elif self.desktop_widget is not None:
            self._remember_widget_pos()
            self.desktop_widget.close()
            self.desktop_widget.deleteLater()
            self.desktop_widget = None
        self.save_settings()

    def _remember_widget_pos(self) -> None:
        if self.desktop_widget is not None:
            self.settings.widget_x = self.desktop_widget.x()
            self.settings.widget_y = self.desktop_widget.y()

    def _on_widget_moved(self, x: int, y: int) -> None:
        self.settings.widget_x, self.settings.widget_y = x, y
        self.save_settings()

    def _on_widget_size(self, name: str) -> None:
        self.settings.widget_size = name
        self.save_settings()

    def _on_widget_on_top(self, on: bool) -> None:
        self.settings.widget_on_top = on
        self.save_settings()

    def _on_widget_locked(self, on: bool) -> None:
        self.settings.widget_locked = on
        self._remember_widget_pos()            # locked where it stands now
        self.save_settings()

    # ======================================================= Settings
    def save_settings(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError as e:
            log.error("could not save settings: %s", e)
            self.statusBar().showMessage(f"Could not save settings: {e}", 10000)

    def set_unit(self, attr: str, value: str) -> None:
        setattr(self.settings, attr, value)
        self.unit_actions[attr][value].setChecked(True)
        self.save_settings()
        if attr == "time_format":
            units.set_time_format(value)
        if attr == "wind_unit":
            self.wind_panel.set_wind_unit(value)
        self._refresh_location_texts()
        self.render()
        if attr == "time_format":
            self._apply_time_format_everywhere()

    def _apply_time_format_everywhere(self) -> None:
        """Re-show every clock time after a 12/24-hour change (no network, no recalculation
        except the Earth tab's cloud line, which is redrawn in the background)."""
        if self.moon_panel.report is not None:
            self.moon_panel.show_report(self.moon_panel.report)
        for panel in (self.earth_panel, self.globe_panel, self.wind_panel):
            panel.retime()
        if self.tabs.currentWidget() in (self.earth_panel, self.globe_panel):
            self.request_earth()
        else:
            self.earth_panel.last_compose = self.globe_panel.last_compose = 0.0
        self.chart.update()

    def set_refresh_minutes(self, minutes: int) -> None:
        if minutes not in REFRESH_CHOICES:
            return
        self.settings.refresh_minutes = minutes
        self.refresh_actions[minutes].setChecked(True)
        self.save_settings()
        self._apply_refresh_interval()

    def set_startup_update_all(self, on: bool) -> None:
        self.settings.startup_update_all = bool(on)
        self.act_startup_all.setChecked(self.settings.startup_update_all)
        self.save_settings()
        if not on:                                 # stop a round that's still going
            self._startup_due.clear()
            self._startup_tries.clear()
            self.startup_timer.stop()

    def _check_refresh_due(self) -> None:
        """Runs every minute. Updates the weather when it's older than the refresh interval,
        going by the wall clock - so after the computer sleeps, or the window has been
        hidden in the tray for hours, it catches up within a minute instead of waiting
        for the interval timer (which doesn't count time asleep). A failed update is
        retried after 5 minutes rather than a whole interval."""
        mins = self.settings.refresh_minutes
        loc = self.settings.selected_location()
        if mins <= 0 or loc is None or loc.key in self._pending:
            return
        now = time.time()
        fc = self.forecasts.get(loc.key)
        age = now - fc.fetched_at if fc is not None and fc.fetched_at else float("inf")
        since_try = now - self._last_attempt.get(loc.key, 0.0)
        if self.errors.get(loc.key) and since_try < self._retry_delay(loc.key):
            return
        if age >= mins * 60 - 20 and since_try >= 60:
            log.info("weather is %s old; updating", _age_text(age).replace(" ago", ""))
            self.refresh_selected()
            self._apply_refresh_interval()        # restart the interval from now

    def _retry_delay(self, key: str) -> float:
        n = max(1, self._fail_count.get(key, 1))
        return RETRY_STEPS_S[min(n, len(RETRY_STEPS_S)) - 1]

    def _retry_failed(self) -> None:
        """Retry timer: try the selected location again if its last update failed."""
        loc = self.settings.selected_location()
        if loc is None or self.settings.refresh_minutes <= 0 or loc.key in self._pending:
            return
        if self.errors.get(loc.key):
            log.info("retrying the failed update for %s", loc.name)
            self.refresh_selected()

    # ================================================= Weather providers
    def _make_manager(self, client) -> ProviderManager:
        """The weather client. Normally a ProviderManager built from the saved chain. A
        plain Open-Meteo client (as the tests pass) becomes a one-provider manager, so
        nothing else is ever contacted."""
        if isinstance(client, ProviderManager):
            return client
        if client is not None:
            from .providers.openmeteo import OpenMeteoProvider
            return ProviderManager(chain=["open-meteo"],
                                   overrides={"open-meteo": OpenMeteoProvider(client=client)},
                                   ssl_context=getattr(client, "ssl_context", None))
        return ProviderManager(chain=self.settings.provider_chain, recovery=self.settings.provider_recovery,
                               status_file=Path(self.cache.dir) / "providers.json",
                               options=self.settings.provider_options)

    def _credit(self, fc: Optional[Forecast]) -> str:
        inf = providers.info(fc.provider_id) if fc is not None else None
        return inf.credit if inf else DATA_CREDIT

    def show_providers(self) -> None:
        from .providers_dialog import ProvidersDialog
        if self._providers_dialog is None:
            self._providers_dialog = ProvidersDialog(self)
        self._providers_dialog.reload()
        self._providers_dialog.show()
        self._providers_dialog.raise_()
        self._providers_dialog.activateWindow()

    def set_provider_option(self, pid: str, name: str, value: str) -> None:
        """From the Weather Providers dialog (e.g. a station ID). Raises ValueError if invalid."""
        self.client.set_option(pid, name, value)
        self.settings.provider_options = {k: dict(v) for k, v in self.client.options.items()}
        self.save_settings()
        if pid == getattr(self.client, "active", None) or pid == self.client.primary:
            self.refresh_selected(force=True)

    def apply_provider_settings(self, chain: list[str], recovery: str) -> None:
        """From the Weather Providers dialog: save the order and recovery, then update."""
        from .storage import WEATHER_SOURCES
        old = (list(self.client.chain), self.client.recovery)
        self.client.set_chain(chain)
        self.client.set_recovery(recovery)
        self.settings.provider_chain = list(self.client.chain)
        self.settings.provider_recovery = self.client.recovery
        self.settings.weather_source = self.client.primary if self.client.primary in WEATHER_SOURCES \
            else "open-meteo"
        self.save_settings()
        if old[0] != self.client.chain:
            self.client.try_primary_now()
            self.refresh_selected(force=True)
        self._update_status()

    def _apply_refresh_interval(self) -> None:
        self.refresh_timer.stop()
        if self.settings.refresh_minutes > 0:
            self.refresh_timer.start(self.settings.refresh_minutes * 60_000)

    def set_show_tray(self, on: bool) -> None:
        self.settings.show_tray = bool(on)
        if self.tray:
            self.tray.setVisible(self.settings.show_tray)
        if not on and self.settings.close_to_tray:
            self.set_close_to_tray(False)
        self.save_settings()

    def set_close_to_tray(self, on: bool) -> None:
        self.settings.close_to_tray = bool(on)
        self.act_close_tray.setChecked(self.settings.close_to_tray)
        if on and not self.settings.show_tray:
            self.act_tray.setChecked(True)
            self.set_show_tray(True)
        self.save_settings()

    @staticmethod
    def _autostart_on() -> bool:
        from . import osplat
        try:
            return osplat.autostart_enabled()
        except Exception:
            return False

    def set_autostart(self, on: bool) -> None:
        """View → Start at login: a .desktop file on Linux, the Run key on Windows (osplat.py)."""
        from . import osplat
        try:
            osplat.set_autostart(on)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not change the login setting:\n{e}")
        self.act_autostart.setChecked(self._autostart_on())

    # ================================================== Help / About
    def show_guide(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{APP_NAME} User Guide")
        dlg.resize(680, 620)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        guide = Path(__file__).with_name("USER_GUIDE.md")
        try:
            tb.setMarkdown(guide.read_text(encoding="utf-8"))
        except OSError:
            tb.setPlainText("The user guide file could not be found.\n\n" + AUTHOR_CREDIT)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()
        self._guide_dialog = dlg

    def show_connection_test(self) -> None:
        if self._diag_dialog is not None:
            self._diag_dialog.show(); self._diag_dialog.raise_()
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Test connection")
        dlg.resize(640, 460)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Checks how quickly RBSB-Weather can reach the weather service."))
        self.diag_text = QPlainTextEdit(readOnly=True)
        f = QFont("monospace"); f.setStyleHint(QFont.StyleHint.Monospace)
        self.diag_text.setFont(f)
        lay.addWidget(self.diag_text, 1)
        row = QHBoxLayout()
        self.diag_run_btn = QPushButton("Run again")
        self.diag_run_btn.clicked.connect(self._start_diag)
        copy_btn = QPushButton("Copy report")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.diag_text.toPlainText()))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        row.addWidget(self.diag_run_btn); row.addWidget(copy_btn); row.addStretch(1); row.addWidget(close_btn)
        lay.addLayout(row)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.destroyed.connect(lambda *_: setattr(self, "_diag_dialog", None))
        self._diag_dialog = dlg
        dlg.show()
        self._start_diag()

    def _start_diag(self) -> None:
        if self._diag_running or self._diag_dialog is None:
            return
        self._diag_running = True
        self.diag_run_btn.setEnabled(False)
        self.diag_text.setPlainText("Running connection test… (up to about 20 seconds)\n")
        self.worker.diagnose()

    def _on_diag_line(self, line: str) -> None:
        if self._diag_dialog is not None:
            self.diag_text.appendPlainText(line)

    def _on_diag_done(self, report: str) -> None:
        self._diag_running = False
        self.last_diag_report = report
        log.info("connection test:\n%s", report)
        if self._diag_dialog is not None:
            self.diag_text.setPlainText(report)
            self.diag_run_btn.setEnabled(True)

    def about_html(self) -> str:
        return (f"<h2>{APP_NAME} {__version__}</h2>"
                f"<p>A desktop weather app for Linux (formerly WeatherDesk).</p>"
                f"<p><b>{AUTHOR_CREDIT}</b></p>"
                f"<p>☕ {COFFEE_TEXT}: <a href='{COFFEE_URL}'>Buy me a coffee with PayPal</a></p>"
                f"<p>{self._credits_html()}</p>"
                f"<p>{self._moon_credit()}</p>"
                f"<p>Settings: {self.store.path}<br>Cache: {self.cache.dir}</p>")

    def _credits_html(self) -> str:
        """Every provider in use (primary and backups), with links."""
        lines = []
        for pid in self.client.chain:
            inf = providers.info(pid)
            if inf:
                host = inf.website.split("//")[-1].strip("/")
                lines.append(f"{inf.credit} – <a href='{inf.website}'>{host}</a>")
        return "<br>".join(lines) or DATA_CREDIT

    @staticmethod
    def _moon_credit() -> str:
        try:
            from .moon_render import texture_meta
            credit = texture_meta().get("credit")
        except Exception:
            credit = ""
        from .earth import CREDIT as EARTH_CREDIT, SOURCE_URL
        base = "Moon phases and rise/set times calculated on this computer."
        from .clouds import CREDIT as CLOUD_CREDIT, PROJECT_URL
        from .wind import CREDIT as WIND_CREDIT, PROJECT_URL as WIND_URL
        earth = (f"<br>{EARTH_CREDIT} – <a href='{SOURCE_URL}'>solarsystemscope.com/textures</a>"
                 f"<br>{CLOUD_CREDIT} – <a href='{PROJECT_URL}'>github.com/matteason/live-cloud-maps</a>"
                 f"<br>{WIND_CREDIT} – <a href='{WIND_URL}'>ncei.noaa.gov</a>")
        return (f"{base}<br>{credit}" if credit else base) + earth

    def buy_coffee(self) -> None:
        """Help → Buy Me a Coffee: opens the PayPal payment page in the browser (2.1.1)."""
        if not self.open_url(COFFEE_URL):
            QMessageBox.information(self, APP_NAME, f"{COFFEE_TEXT}:\n\n{COFFEE_URL}")

    @staticmethod
    def _open_url_in_browser(url: str) -> bool:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        return QDesktopServices.openUrl(QUrl(url))

    def show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}", self.about_html())

    # =============================================== Window / tray
    def show_normal(self) -> None:
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_normal()

    # ================================================== Updates (2.1)
    def set_update_check(self, on: bool) -> None:
        self.settings.update_check = bool(on)
        self.act_update_auto.setChecked(self.settings.update_check)
        self.save_settings()
        if not on:
            self.update_button.setVisible(False)

    def _maybe_check_updates(self) -> None:
        """Runs every minute: starts the automatic check when it's due (once a day)."""
        from . import updates
        if (not self.settings.update_check or self._quitting or self._update_checking
                or time.monotonic() - self._started_mono < updates.FIRST_CHECK_DELAY_S):
            return
        if updates.due(updates.load_state()):
            self._start_update_check(manual=False)

    def check_for_updates(self) -> None:
        """Help → Check for Updates…: check now and show the answer."""
        dlg = self._open_update_dialog()
        dlg.show_checking()
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        from . import updates
        self._update_token += 1
        self._update_checking = True
        self._update_manual = self._update_manual or manual
        st = updates.load_state()
        st["last_attempt"] = time.time()
        updates.save_state(st)
        self.worker.check_update(self._update_token, self.update_client)

    def _on_update_checked(self, token: int, info, error: str) -> None:
        if token != self._update_token:
            return
        from . import updates
        from dataclasses import asdict
        manual, self._update_manual = self._update_manual, False
        self._update_checking = False
        st = updates.load_state()
        if error:
            st["last_error"] = error
            log.info("update check failed: %s", error)
        else:
            st["last_ok"] = time.time()
            st.pop("last_error", None)
            if updates.is_newer(info):
                st["latest"] = asdict(info)
            else:
                st.pop("latest", None)
        updates.save_state(st)
        dlg = self._update_dialog if manual else None
        if error:
            if dlg is not None:
                dlg.show_error(error)
            return
        if not updates.is_newer(info):
            self._update_info = None
            self.update_button.setVisible(False)
            if dlg is not None:
                dlg.show_up_to_date(info.version)
            return
        self._update_info = info
        skipped = info.version == self.settings.update_skip
        if dlg is not None:
            dlg.show_available(info, skipped=skipped)
        if not skipped:
            self._announce_update(info, popup=not manual)

    def _restore_known_update(self) -> None:
        """At startup: show a newer version found by an earlier check, without the network."""
        from . import updates
        if not self.settings.update_check:
            return
        latest = updates.load_state().get("latest")
        if not isinstance(latest, dict):
            return
        try:
            info = updates.UpdateInfo(**{k: latest[k] for k in updates.UpdateInfo.__dataclass_fields__
                                         if k in latest})
        except TypeError:
            return
        if (updates.version_from_tag(info.version) and updates.is_newer(info)
                and info.version != self.settings.update_skip):
            self._update_info = info
            self._announce_update(info, popup=False)

    def _announce_update(self, info, popup: bool) -> None:
        from . import updates
        self.update_button.setText(f"⬆ Update available: {info.version}")
        self.update_button.setVisible(True)
        if not popup:
            return
        st = updates.load_state()
        if st.get("notified") == info.version:      # tell once per version, not every day
            return
        self.statusBar().showMessage(f"{info.title} is available. Click \"Update available\" to see what's new.", 30000)
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(f"{APP_NAME} update", f"{info.title} is available. Click to see what's new.",
                                  QSystemTrayIcon.MessageIcon.Information, 15000)
        st["notified"] = info.version
        updates.save_state(st)

    def _open_update_dialog(self):
        from .update_dialog import UpdateDialog
        if self._update_dialog is None:
            # Kept (hidden) and reused rather than deleted on close: a "destroyed" callback
            # into this window could run while the window itself is being destroyed,
            # which crashed Qt in testing.
            dlg = UpdateDialog(self, __version__)
            dlg.download_requested.connect(self._start_update_download)
            dlg.cancel_requested.connect(self._cancel_update_download)
            dlg.skip_requested.connect(self._skip_update)
            dlg.retry_requested.connect(self.check_for_updates)
            dlg.github_requested.connect(self._open_release_page)
            self._update_dialog = dlg
        self._update_dialog.show()
        self._update_dialog.raise_()
        self._update_dialog.activateWindow()
        return self._update_dialog

    def show_update_dialog(self, info) -> None:
        if info is None:
            self.check_for_updates()
            return
        dlg = self._open_update_dialog()
        if not dlg.downloading:
            dlg.show_available(info, skipped=info.version == self.settings.update_skip)

    def _skip_update(self) -> None:
        if self._update_info is None:
            return
        self.settings.update_skip = self._update_info.version
        self.save_settings()
        self.update_button.setVisible(False)
        self.statusBar().showMessage(f"Version {self._update_info.version} will be skipped. "
                                     "Help → Check for Updates still shows it.", 10000)
        if self._update_dialog is not None:
            self._update_dialog.close()

    def _open_release_page(self) -> None:
        from . import updates
        url = self._update_info.page_url if self._update_info else updates.RELEASES_PAGE
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    def _start_update_download(self) -> None:
        import threading
        from . import updates
        if self._update_info is None or self._update_dialog is None or self._update_cancel is not None:
            return
        self._update_dl_token += 1
        self._update_cancel = threading.Event()
        self._update_dialog.show_downloading()
        self.worker.download_update(self._update_dl_token, self.update_client, self._update_info,
                                    updates.downloads_folder(), self._update_cancel)

    def _cancel_update_download(self) -> None:
        if self._update_cancel is not None:
            self._update_cancel.set()

    def _on_update_progress(self, token: int, done, total) -> None:
        if token == self._update_dl_token and self._update_dialog is not None:
            self._update_dialog.set_progress(int(done), int(total))

    def _on_update_downloaded(self, token: int, path: str, error: str) -> None:
        if token != self._update_dl_token:
            return
        self._update_cancel = None
        dlg = self._update_dialog
        if error:
            log.info("update download: %s", error)
            if dlg is not None:
                dlg.show_download_failed(error)
            return
        opened = False
        try:
            opened = bool(self.open_file(path))
        except Exception as e:
            log.warning("could not open the package installer: %s", e)
        if dlg is not None:
            dlg.show_downloaded(path, opened)
        else:
            self.statusBar().showMessage(f"Update downloaded and checked: {path}", 30000)

    @staticmethod
    def _open_with_desktop(path: str) -> bool:
        """Linux: open the .deb in the package installer. Windows: show the portable zip in
        Explorer (it's unzipped over the current folder once RBSB-Weather is closed)."""
        from . import osplat
        if osplat.IS_WINDOWS:
            return osplat.show_in_file_manager(path)
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        return QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_visible()

    def quit_app(self) -> None:
        """Quit cleanly. Safe to call more than once (e.g. a second SIGTERM or a
        hand-over request arriving while already shutting down)."""
        if getattr(self, "_closed", False) or getattr(self, "_quit_started", False):
            return
        self._quit_started = True
        self._quitting = True
        self.close()

    def closeEvent(self, e: QCloseEvent) -> None:
        if (not self._quitting and self.settings.close_to_tray and self.tray is not None
                and self.tray.isVisible()):
            self.hide()
            e.ignore()
            return
        self.settings.window_geometry = bytes(self.saveGeometry().toHex()).decode()
        self._remember_widget_pos()
        self.save_settings()
        if self.desktop_widget is not None:
            self.desktop_widget.close()
            self.desktop_widget.deleteLater()
            self.desktop_widget = None
        if self._update_cancel is not None:
            self._update_cancel.set()      # stop a download; its partial file is deleted
        if self._update_dialog is not None:
            self._update_dialog.close()
        retire(self.bridge, self.worker)   # worker threads may still be running
        self.refresh_timer.stop()
        self.age_timer.stop()
        self.search_timer.stop()
        self.startup_timer.stop()
        self.retry_timer.stop()
        self._startup_due.clear()
        if self.tray is not None:
            self.tray.hide()
        e.accept()
        self._closed = True
        app = QApplication.instance()
        if app is not None:
            app.quit()
