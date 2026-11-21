"""Help → Check for Updates: the dialog (2.1.0).

The dialog only shows things and reports button presses. The main window
does the checking and downloading (in the background) and calls the show_*
methods.

Created By Shane BlackbuRn
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QTextBrowser, QVBoxLayout)

from . import APP_NAME, AUTHOR_CREDIT


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


class UpdateDialog(QDialog):
    download_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    skip_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    github_requested = pyqtSignal()

    def __init__(self, parent=None, current_version: str = ""):
        super().__init__(parent)
        self.current = current_version
        self.info = None
        self.setWindowTitle(f"{APP_NAME} updates")
        self.resize(620, 480)
        lay = QVBoxLayout(self)
        self.headline = QLabel()
        self.headline.setWordWrap(True)
        self.headline.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.headline)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.detail)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        lay.addWidget(self.notes, 1)
        from PyQt6.QtWidgets import QWidget
        self.filler = QWidget()            # takes the space when there are no notes to show
        lay.addWidget(self.filler, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        foot = QLabel(f"<small>{html.escape(AUTHOR_CREDIT)}</small>")
        lay.addWidget(foot)

        row = QHBoxLayout()
        self.btn_install = QPushButton("Download and Install")
        self.btn_github = QPushButton("View on GitHub")
        self.btn_skip = QPushButton("Skip This Version")
        self.btn_retry = QPushButton("Try Again")
        self.btn_cancel = QPushButton("Cancel Download")
        self.btn_copy = QPushButton("Copy Command")
        self.btn_close = QPushButton("Close")
        for b in (self.btn_install, self.btn_github, self.btn_skip, self.btn_retry, self.btn_cancel, self.btn_copy):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.btn_install.clicked.connect(self.download_requested)
        self.btn_github.clicked.connect(self.github_requested)
        self.btn_skip.clicked.connect(self.skip_requested)
        self.btn_retry.clicked.connect(self.retry_requested)
        self.btn_cancel.clicked.connect(self.cancel_requested)
        self.btn_copy.clicked.connect(self._copy_command)
        self.btn_close.clicked.connect(self.close)
        self._command = ""
        self.downloading = False
        self.show_checking()

    # ------------------------------------------------------------- states
    def _set_notes_visible(self, on: bool) -> None:
        self.notes.setVisible(on)
        self.filler.setVisible(not on)

    def _buttons(self, *shown) -> None:
        for b in (self.btn_install, self.btn_github, self.btn_skip, self.btn_retry, self.btn_cancel, self.btn_copy):
            b.setVisible(b in shown)
        self.btn_close.setEnabled(self.btn_cancel not in shown)

    def show_checking(self) -> None:
        self.downloading = False
        self.headline.setText("<b>Checking for updates…</b>")
        self.detail.setText(f"You have {APP_NAME} {self.current}.")
        self._set_notes_visible(False)
        self.progress.setVisible(False)
        self._buttons()

    def show_up_to_date(self, latest: str) -> None:
        self.downloading = False
        self.headline.setText(f"<b>You have the latest version ({html.escape(self.current)}).</b>")
        extra = f" The newest release on GitHub is {latest}." if latest and latest != self.current else ""
        self.detail.setText("There's nothing to install." + extra)
        self._set_notes_visible(False)
        self.progress.setVisible(False)
        self._buttons(self.btn_github)

    def show_error(self, message: str) -> None:
        self.downloading = False
        self.headline.setText("<b>Couldn't check for updates.</b>")
        self.detail.setText(message)
        self._set_notes_visible(False)
        self.progress.setVisible(False)
        self._buttons(self.btn_retry, self.btn_github)

    def show_available(self, info, skipped: bool = False) -> None:
        self.info = info
        self.downloading = False
        self.headline.setText(f"<b>{html.escape(info.title)} is available.</b>")
        when = f" Published {info.published[:10]}." if info.published else ""
        size = f" Download: {_mb(info.deb_size)}." if info.deb_size else ""
        note = " You chose to skip this version; you can still install it here." if skipped else ""
        from .osplat import IS_WINDOWS
        if info.installable and IS_WINDOWS:
            how = (" It's downloaded to your Downloads folder and checked against its checksum; "
                   "then you replace the program files with it.")
        elif info.installable:
            how = (" It's downloaded to your Downloads folder, checked against its checksum, "
                   "then opened in your package installer (which asks for your password).")
        else:
            how = " This release has no installer RBSB-Weather can check, so download it from GitHub."
        self.detail.setText(f"You have {self.current}.{when}{size}{how}{note}")
        self.btn_install.setText("Download and Install")
        self.notes.setMarkdown(info.notes or "*No release notes.*")
        self._set_notes_visible(True)
        self.progress.setVisible(False)
        shown = [self.btn_github] + ([self.btn_install] if info.installable else []) + \
                ([] if skipped else [self.btn_skip])
        self._buttons(*shown)

    def show_downloading(self) -> None:
        self.downloading = True
        self.headline.setText(f"<b>Downloading {html.escape(self.info.title if self.info else 'the update')}…</b>")
        self.detail.setText("The file is checked against the release's checksum before it's kept.")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)            # busy until the size is known
        self._buttons(self.btn_cancel)

    def set_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(min(done, total) * 1000 / total))
            self.progress.setFormat(f"{_mb(done)} of {_mb(total)}")
        else:
            self.progress.setRange(0, 0)
        self.progress.setTextVisible(total > 0)

    def show_downloaded(self, path: str, opened: bool) -> None:
        from .osplat import IS_WINDOWS
        if IS_WINDOWS or path.endswith(".zip"):
            self._show_downloaded_zip(path, opened)
            return
        self.downloading = False
        self._command = f"sudo apt install '{path}'"
        self.progress.setVisible(False)
        self.headline.setText("<b>Downloaded and checked.</b>")
        if opened:
            msg = (f"Saved to {path}.\n\nYour package installer is opening: click Install, and enter your "
                   f"password when asked. Then start {APP_NAME} again (or open it from the menu) and the "
                   "new version takes over from this one.")
        else:
            msg = (f"Saved to {path}.\n\nYour package installer couldn't be opened automatically. "
                   f"To install, run this in a terminal, then start {APP_NAME} again:\n\n{self._command}")
        self.detail.setText(msg)
        self._buttons(self.btn_copy)

    def _show_downloaded_zip(self, path: str, opened: bool) -> None:
        """Windows portable: the new version is replaced by hand once the app is closed."""
        self.downloading = False
        self.progress.setVisible(False)
        self.headline.setText("<b>Downloaded and checked.</b>")
        where = "It's selected in the Explorer window that just opened." if opened else ""
        self.detail.setText(
            f"Saved to {path}. {where}\n\nTo update: close {APP_NAME} (tray icon → Quit), then "
            f"unzip this file into your {APP_NAME} folder and choose to replace the files. Your "
            f"places and settings, in the RBSB-Weather-data folder, are kept. Then start "
            f"RBSB-Weather.exe again.")
        self._buttons()

    def show_download_failed(self, message: str) -> None:
        self.downloading = False
        self.progress.setVisible(False)
        self.headline.setText("<b>The update wasn't downloaded.</b>")
        self.detail.setText(message)
        shown = [self.btn_github] + ([self.btn_install] if self.info and self.info.installable else [])
        self._buttons(*shown)
        self.btn_install.setText("Try Again")

    def _copy_command(self) -> None:
        if self._command:
            QApplication.clipboard().setText(self._command)

    def closeEvent(self, e) -> None:
        if self.downloading:
            self.cancel_requested.emit()       # closing the window stops the download
        super().closeEvent(e)
