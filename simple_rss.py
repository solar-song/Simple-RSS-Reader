#!/usr/bin/env python3
"""Simple RSS/Atom reader with a manual HTTP/SOCKS5 proxy toggle.

Requires: PySide6, feedparser
Run:      python3 simple_rss.py
"""
import json
import re
import sys
from datetime import timedelta

import feedparser
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QImage, QTextDocument
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QSplitter,
    QTextBrowser,
)

APP_NAME = "SimpleRSS"
USER_AGENT = b"SimpleRSS/1.0 (+Qt)"


# --------------------------------------------------------------------------
# Proxy settings dialog
# --------------------------------------------------------------------------
class ProxyDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Proxy Settings")
        self.settings = settings

        self.type_box = QComboBox()
        self.type_box.addItems(["HTTP", "SOCKS5"])
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("optional")
        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("optional")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout(self)
        form.addRow("Type:", self.type_box)
        form.addRow("Host:", self.host_edit)
        form.addRow("Port:", self.port_spin)
        form.addRow("Username:", self.user_edit)
        form.addRow("Password:", self.pass_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        # Load saved values
        self.type_box.setCurrentText(settings.value("proxy/type", "HTTP"))
        self.host_edit.setText(settings.value("proxy/host", ""))
        self.port_spin.setValue(int(settings.value("proxy/port", 8080)))
        self.user_edit.setText(settings.value("proxy/user", ""))
        self.pass_edit.setText(settings.value("proxy/password", ""))

    def _accept(self):
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "Proxy", "Please enter a host.")
            return
        s = self.settings
        s.setValue("proxy/type", self.type_box.currentText())
        s.setValue("proxy/host", self.host_edit.text().strip())
        s.setValue("proxy/port", self.port_spin.value())
        s.setValue("proxy/user", self.user_edit.text())
        # NOTE: stored in plain text in ~/.config. Fine for a simple app;
        # use QtKeychain / KWallet if you need real secrecy.
        s.setValue("proxy/password", self.pass_edit.text())
        self.accept()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def apply_timeout(req: QNetworkRequest, seconds: int = 20):
    """PySide6 versions differ on whether this takes int ms or a timedelta."""
    try:
        req.setTransferTimeout(seconds * 1000)
    except TypeError:
        req.setTransferTimeout(timedelta(seconds=seconds))


def prepare_request(req: QNetworkRequest):
    """Common request setup.

    HTTP/2 is disabled on purpose: Qt's HTTP/2 client often gets GOAWAY /
    "unable to maintain the header compression context" errors when many
    requests (e.g. article images) hit the same host at once.
    """
    req.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
    req.setRawHeader(b"User-Agent", USER_AGENT)
    apply_timeout(req)


def media_image(entry) -> str:
    """Find an image URL in media:thumbnail / media:content / enclosures."""
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key, []) or []:
            if m.get("url") and (key == "media_thumbnail" or m.get("medium") == "image"
                                 or str(m.get("type", "")).startswith("image")):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    return ""


# --------------------------------------------------------------------------
# Article view that can download images (through the proxied network manager)
# --------------------------------------------------------------------------
class ArticleBrowser(QTextBrowser):
    MAX_IMAGE_BYTES = 15 * 1024 * 1024

    def __init__(self, nam: QNetworkAccessManager, parent=None):
        super().__init__(parent)
        self.nam = nam
        self.base_url = QUrl()
        self._images: dict[str, QImage] = {}  # url -> original image
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._html = ""
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)  # batch several images into one redraw
        self._reload_timer.timeout.connect(self._reload)
        self._placeholder = QImage(1, 1, QImage.Format.Format_ARGB32)
        self._placeholder.fill(0)
        self.setOpenExternalLinks(True)

    def set_article(self, html: str, base: str):
        self.base_url = QUrl(base)
        self._html = html
        self.setHtml(html)

    def _reload(self):
        """Re-render the current article, keeping the scroll position."""
        bar = self.verticalScrollBar()
        pos = bar.value()
        self.setHtml(self._html)
        bar.setValue(pos)

    def _fit(self, img: QImage) -> QImage:
        max_w = max(120, self.viewport().width() - 40)
        if img.width() > max_w:
            return img.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
        return img

    def loadResource(self, rtype, name):
        t = rtype.value if hasattr(rtype, "value") else int(rtype)
        if t != QTextDocument.ResourceType.ImageResource.value:
            return super().loadResource(rtype, name)

        url = self.base_url.resolved(name) if name.isRelative() else name
        if url.scheme() not in ("http", "https"):
            return super().loadResource(rtype, name)

        key = url.toString()
        if key in self._images:
            return self._fit(self._images[key])
        if key not in self._pending and key not in self._failed:
            self._fetch_image(url, key, name)
        return self._placeholder  # blank until the download finishes

    def _fetch_image(self, url: QUrl, key: str, orig_name: QUrl):
        self._pending.add(key)
        req = QNetworkRequest(url)
        req.setRawHeader(b"User-Agent", USER_AGENT)
        if self.base_url.isValid():  # some hosts block hotlinking without this
            req.setRawHeader(b"Referer", self.base_url.toString().encode())
        prepare_request(req)
        reply = self.nam.get(req)
        reply.finished.connect(lambda: self._image_done(reply, key, orig_name))

    def _image_done(self, reply: QNetworkReply, key: str, orig_name: QUrl):
        reply.deleteLater()
        self._pending.discard(key)

        img = QImage()
        error = reply.error()
        ok = error == QNetworkReply.NetworkError.NoError
        if ok:
            data = reply.readAll()
            ok = data.size() <= self.MAX_IMAGE_BYTES and img.loadFromData(data)
            if not ok:
                error = None  # downloaded fine but undecodable / too big: don't retry

        if not ok:
            n = self._attempts.get(key, 0) + 1
            self._attempts[key] = n
            retryable = error not in (None, QNetworkReply.NetworkError.ContentNotFoundError)
            if retryable and n < 3:
                self._fetch_image(QUrl(key), key, orig_name)
            else:
                self._failed.add(key)
                print(f"[image] giving up on {key}: {reply.errorString()}", file=sys.stderr)
            return

        self._images[key] = img
        # addResource() takes priority over anything Qt cached for this URL
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource, orig_name, self._fit(img)
        )
        self._reload_timer.start()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simple RSS Reader")
        self.resize(1200, 700)

        self.settings = QSettings("simple-rss", APP_NAME)
        self.nam = QNetworkAccessManager(self)
        self.nam.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.feeds: list[dict] = []          # [{"url": ..., "title": ...}]
        self.entries: dict[str, list] = {}   # url -> list of entry dicts

        self._build_ui()
        self._build_toolbar()
        self._load_feeds()
        self._apply_proxy()
        if self.feeds:
            self.refresh_all()

    # ---- UI -------------------------------------------------------------
    def _build_ui(self):
        self.feed_list = QListWidget()
        self.article_list = QListWidget()
        self.browser = ArticleBrowser(self.nam)
        self.browser.setPlaceholderText("Select an article to read it.")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.feed_list)
        splitter.addWidget(self.article_list)
        splitter.addWidget(self.browser)
        splitter.setSizes([220, 340, 640])
        self.setCentralWidget(splitter)

        self.feed_list.currentRowChanged.connect(self._show_articles)
        self.article_list.currentRowChanged.connect(self._show_article)
        self.statusBar().showMessage("Ready")

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        add = QAction("Add Feed", self)
        add.setShortcut("Ctrl+N")
        add.triggered.connect(self.add_feed)
        tb.addAction(add)

        remove = QAction("Remove Feed", self)
        remove.triggered.connect(self.remove_feed)
        tb.addAction(remove)

        refresh = QAction("Refresh", self)
        refresh.setShortcut("F5")
        refresh.triggered.connect(self.refresh_all)
        tb.addAction(refresh)

        open_link = QAction("Open in Browser", self)
        open_link.triggered.connect(self.open_in_browser)
        tb.addAction(open_link)

        tb.addSeparator()

        self.proxy_action = QAction("Use Proxy", self)
        self.proxy_action.setCheckable(True)
        self.proxy_action.setChecked(self.settings.value("proxy/enabled", False, type=bool))
        self.proxy_action.toggled.connect(self._on_proxy_toggled)
        tb.addAction(self.proxy_action)

        proxy_cfg = QAction("Proxy Settings…", self)
        proxy_cfg.triggered.connect(self._edit_proxy)
        tb.addAction(proxy_cfg)

    # ---- Persistence ----------------------------------------------------
    def _load_feeds(self):
        try:
            self.feeds = json.loads(self.settings.value("feeds", "[]"))
        except (ValueError, TypeError):
            self.feeds = []
        self._refresh_feed_list()

    def _save_feeds(self):
        self.settings.setValue("feeds", json.dumps(self.feeds))

    def _refresh_feed_list(self, select: int = -1):
        self.feed_list.blockSignals(True)
        self.feed_list.clear()
        for f in self.feeds:
            self.feed_list.addItem(QListWidgetItem(f.get("title") or f["url"]))
        self.feed_list.blockSignals(False)
        if 0 <= select < len(self.feeds):
            self.feed_list.setCurrentRow(select)

    # ---- Proxy ----------------------------------------------------------
    def _apply_proxy(self):
        """Apply (or explicitly clear) the proxy on our own network manager only."""
        if self.proxy_action.isChecked():
            s = self.settings
            ptype = (
                QNetworkProxy.ProxyType.Socks5Proxy
                if s.value("proxy/type", "HTTP") == "SOCKS5"
                else QNetworkProxy.ProxyType.HttpProxy
            )
            proxy = QNetworkProxy(
                ptype,
                s.value("proxy/host", ""),
                int(s.value("proxy/port", 8080)),
                s.value("proxy/user", ""),
                s.value("proxy/password", ""),
            )
            self.nam.setProxy(proxy)
            self.statusBar().showMessage(
                f"Proxy ON: {s.value('proxy/type')} {s.value('proxy/host')}:{s.value('proxy/port')}"
            )
        else:
            # NoProxy = connect directly (don't fall back to system proxy)
            self.nam.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
            self.statusBar().showMessage("Proxy OFF (direct connection)")

    def _on_proxy_toggled(self, checked: bool):
        if checked and not self.settings.value("proxy/host", ""):
            # No proxy configured yet: ask for one first.
            if not self._edit_proxy():
                self.proxy_action.blockSignals(True)
                self.proxy_action.setChecked(False)
                self.proxy_action.blockSignals(False)
                return
        self.settings.setValue("proxy/enabled", checked)
        self._apply_proxy()

    def _edit_proxy(self) -> bool:
        dlg = ProxyDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_proxy()
            return True
        return False

    # ---- Feed actions ---------------------------------------------------
    def add_feed(self):
        url, ok = QInputDialog.getText(self, "Add Feed", "Feed URL (RSS or Atom):")
        url = url.strip()
        if not ok or not url:
            return
        if "://" not in url:
            url = "https://" + url
        if any(f["url"] == url for f in self.feeds):
            QMessageBox.information(self, "Add Feed", "You're already subscribed to that feed.")
            return
        self.feeds.append({"url": url, "title": url})
        self._save_feeds()
        self._refresh_feed_list(select=len(self.feeds) - 1)
        self._fetch(url)

    def remove_feed(self):
        row = self.feed_list.currentRow()
        if row < 0:
            return
        feed = self.feeds.pop(row)
        self.entries.pop(feed["url"], None)
        self._save_feeds()
        self._refresh_feed_list()
        self.article_list.clear()
        self.browser.clear()

    def refresh_all(self):
        for f in self.feeds:
            self._fetch(f["url"])

    # ---- Networking -----------------------------------------------------
    def _fetch(self, url: str):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT)
        req.setRawHeader(
            b"Accept",
            b"application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
        )
        prepare_request(req)
        reply = self.nam.get(req)
        reply.finished.connect(lambda r=reply, u=url: self._on_finished(r, u))
        self.statusBar().showMessage(f"Fetching {url} …")

    def _on_finished(self, reply: QNetworkReply, url: str):
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.statusBar().showMessage(f"Error fetching {url}: {reply.errorString()}")
            return

        parsed = feedparser.parse(bytes(reply.readAll().data()))
        if parsed.bozo and not parsed.entries:
            self.statusBar().showMessage(f"Not a valid RSS/Atom feed: {url}")
            return

        # Update the feed title on first successful fetch
        title = parsed.feed.get("title", "").strip()
        for f in self.feeds:
            if f["url"] == url and title and f.get("title") != title:
                f["title"] = title
                self._save_feeds()
                selected = self.feed_list.currentRow()
                self._refresh_feed_list(select=selected)

        items = []
        for e in parsed.entries:
            content = ""
            if e.get("content"):
                content = e["content"][0].get("value", "")
            content = content or e.get("summary", "") or e.get("description", "")
            if "<img" not in content.lower():
                pic = media_image(e)
                if pic:
                    content = f'<p><img src="{pic}"></p>' + content
            items.append(
                {
                    "title": e.get("title", "(untitled)"),
                    "link": e.get("link", ""),
                    "date": e.get("published", e.get("updated", "")),
                    "html": content,
                }
            )
        self.entries[url] = items
        self.statusBar().showMessage(f"Loaded {len(items)} articles from {title or url}")

        # If this is the currently selected feed, refresh the article list
        row = self.feed_list.currentRow()
        if 0 <= row < len(self.feeds) and self.feeds[row]["url"] == url:
            self._show_articles(row)

    # ---- Article display ------------------------------------------------
    def _show_articles(self, row: int):
        self.article_list.clear()
        self.browser.clear()
        if not (0 <= row < len(self.feeds)):
            return
        for item in self.entries.get(self.feeds[row]["url"], []):
            self.article_list.addItem(item["title"])

    def _current_entry(self):
        frow = self.feed_list.currentRow()
        arow = self.article_list.currentRow()
        if frow < 0 or arow < 0:
            return None
        items = self.entries.get(self.feeds[frow]["url"], [])
        return items[arow] if arow < len(items) else None

    def _show_article(self, _row: int):
        e = self._current_entry()
        if not e:
            return
        html = (
            f"<h2>{e['title']}</h2>"
            f"<p><i>{e['date']}</i></p>"
            f"{e['html']}"
            f"<p><a href='{e['link']}'>Read original article</a></p>"
        )
        # Fixed width/height attributes break scaling in Qt's rich text engine
        html = re.sub(
            r"(<img\b[^>]*?)\s(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
            r"\1", html, flags=re.I,
        )
        html = re.sub(
            r"(<img\b[^>]*?)\s(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
            r"\1", html, flags=re.I,
        )
        self.browser.set_article(html, e["link"])

    def open_in_browser(self):
        e = self._current_entry()
        if e and e["link"]:
            QDesktopServices.openUrl(QUrl(e["link"]))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("simple-rss")  # matches simple-rss.desktop
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
