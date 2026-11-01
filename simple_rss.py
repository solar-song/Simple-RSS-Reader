#!/usr/bin/env python3
"""Simple RSS/Atom reader with folders, reader typography and a manual
HTTP/SOCKS5 proxy toggle.

Requires: PySide6, feedparser
Run:      python3 simple_rss.py
"""
import calendar
import html
import json
import re
import sys
from datetime import datetime, timedelta

import feedparser
from PySide6.QtCore import QRect, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QImage,
    QKeySequence,
    QPalette,
    QTextDocument,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontDialog,
    QFormLayout,
    QFrame,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
)

APP_NAME = "SimpleRSS"
USER_AGENT = b"SimpleRSS/1.0 (+Qt)"

# Custom item-data roles (== Qt.UserRole, +1, +2)
ROLE_KIND = 0x0100   # "all" | "folder" | "feed"
ROLE_KEY = 0x0101    # folder name or feed URL
ROLE_META = 0x0102   # second line of an article row


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def enum_value(x) -> int:
    return x.value if hasattr(x, "value") else int(x)


def themed(*names: str) -> QIcon:
    """First icon that exists in the current icon theme."""
    for n in names:
        icon = QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
    return QIcon()


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
            if m.get("url") and (
                key == "media_thumbnail"
                or m.get("medium") == "image"
                or str(m.get("type", "")).startswith("image")
            ):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    return ""


_IMG_SIZE_ATTR = re.compile(
    r"(<img\b[^>]*?)\s(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I
)


def strip_img_size(text: str) -> str:
    """Fixed width/height attributes break image scaling in Qt's rich text."""
    prev = None
    while prev != text:
        prev = text
        text = _IMG_SIZE_ATTR.sub(r"\1", text)
    return text


# --------------------------------------------------------------------------
# Dialogs
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


class AddFeedDialog(QDialog):
    def __init__(self, folders: list[str], current_folder: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Feed")
        self.setMinimumWidth(440)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/feed.xml")
        self.url_edit.setClearButtonEnabled(True)
        self.folder_box = QComboBox()
        self.folder_box.addItem("(No folder)", "")
        for name in folders:
            self.folder_box.addItem(name, name)
        idx = self.folder_box.findData(current_folder)
        if idx >= 0:
            self.folder_box.setCurrentIndex(idx)

        form = QFormLayout(self)
        form.addRow("Feed URL (RSS or Atom):", self.url_edit)
        form.addRow("Folder:", self.folder_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        if self.url_edit.text().strip():
            self.accept()

    def values(self) -> tuple[str, str]:
        return self.url_edit.text().strip(), self.folder_box.currentData() or ""


class EditFeedDialog(QDialog):
    def __init__(self, feed: dict, folders: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Feed")
        self.setMinimumWidth(460)

        self.title_edit = QLineEdit(feed.get("title", ""))
        self.title_edit.setPlaceholderText("Leave empty to use the feed's own title")
        self.title_edit.setClearButtonEnabled(True)
        self.url_edit = QLineEdit(feed["url"])
        self.url_edit.setClearButtonEnabled(True)
        self.folder_box = QComboBox()
        self.folder_box.addItem("(No folder)", "")
        for name in folders:
            self.folder_box.addItem(name, name)
        idx = self.folder_box.findData(feed.get("folder", ""))
        if idx >= 0:
            self.folder_box.setCurrentIndex(idx)

        form = QFormLayout(self)
        form.addRow("Name:", self.title_edit)
        form.addRow("Feed URL:", self.url_edit)
        form.addRow("Folder:", self.folder_box)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self):
        if self.url_edit.text().strip():
            self.accept()

    def values(self) -> tuple[str, str, str]:
        return (
            self.title_edit.text().strip(),
            self.url_edit.text().strip(),
            self.folder_box.currentData() or "",
        )


# --------------------------------------------------------------------------
# Feed tree (folders + feeds) with drag & drop of feeds onto folders
# --------------------------------------------------------------------------
class FeedTree(QTreeWidget):
    feed_dropped = Signal(str, str)  # feed url, target folder ("" = none)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIconSize(QSize(20, 20))
        self.setIndentation(16)
        self.setUniformRowHeights(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def dropEvent(self, event):
        src = self.currentItem()
        if src is None or src.data(0, ROLE_KIND) != "feed":
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        folder = ""
        if target is not None:
            kind = target.data(0, ROLE_KIND)
            if kind == "folder":
                folder = target.data(0, ROLE_KEY)
            elif kind == "feed" and target.parent() is not None:
                folder = target.parent().data(0, ROLE_KEY)
        self.feed_dropped.emit(src.data(0, ROLE_KEY), folder)
        # We rebuild the tree ourselves; tell Qt not to move/delete items.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()


# --------------------------------------------------------------------------
# Article list row: bold title (2 lines max) + muted "source · date" line
# --------------------------------------------------------------------------
class ArticleDelegate(QStyledItemDelegate):
    PAD_X, PAD_Y = 12, 8

    @staticmethod
    def _fonts(base: QFont):
        title = QFont(base)
        title.setBold(True)
        meta = QFont(base)
        size = base.pointSizeF()
        meta.setPointSizeF(max(7.0, (size if size > 0 else 10.0) * 0.85))
        return title, meta

    def sizeHint(self, option, index):
        tf, mf = self._fonts(option.font)
        h = QFontMetrics(tf).height() * 2 + QFontMetrics(mf).height() + 2 * self.PAD_Y + 4
        return QSize(0, h)

    def paint(self, painter, option, index):
        painter.save()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        fg = option.palette.color(role)
        muted = QColor(fg)
        muted.setAlphaF(0.7)

        tf, mf = self._fonts(option.font)
        tfm, mfm = QFontMetrics(tf), QFontMetrics(mf)
        r = option.rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)

        title_rect = QRect(r.left(), r.top(), r.width(), tfm.height() * 2)
        painter.setClipRect(title_rect)
        painter.setFont(tf)
        painter.setPen(fg)
        flags = (
            enum_value(Qt.AlignmentFlag.AlignLeft)
            | enum_value(Qt.AlignmentFlag.AlignTop)
            | enum_value(Qt.TextFlag.TextWordWrap)
        )
        painter.drawText(title_rect, flags, index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setClipping(False)

        meta = index.data(ROLE_META) or ""
        meta_rect = QRect(r.left(), title_rect.bottom() + 4, r.width(), mfm.height())
        painter.setFont(mf)
        painter.setPen(muted)
        painter.drawText(
            meta_rect,
            enum_value(Qt.AlignmentFlag.AlignLeft) | enum_value(Qt.AlignmentFlag.AlignVCenter),
            mfm.elidedText(meta, Qt.TextElideMode.ElideRight, r.width()),
        )
        painter.restore()


# --------------------------------------------------------------------------
# Article view: typography, centered reading column, image downloading
# --------------------------------------------------------------------------
class ArticleBrowser(QTextBrowser):
    MAX_IMAGE_BYTES = 15 * 1024 * 1024
    MAX_COLUMN = 760  # px, width of the reading column

    def __init__(self, nam: QNetworkAccessManager, parent=None):
        super().__init__(parent)
        self.nam = nam
        self.base_url = QUrl()
        self.reader_font = QFont()
        self._entry: dict | None = None
        self._html = ""

        self._images: dict[str, QImage] = {}   # resolved url -> original image
        self._names: dict[str, QUrl] = {}      # resolved url -> name used in the HTML
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._placeholder = QImage(1, 1, QImage.Format.Format_ARGB32)
        self._placeholder.fill(0)

        self._reload_timer = QTimer(self)  # batches several images into one redraw
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)
        self._reload_timer.timeout.connect(self._render)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(200)
        self._resize_timer.timeout.connect(self._relayout)

        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

    # ---- typography -----------------------------------------------------
    def set_reader_font(self, font: QFont):
        self.reader_font = QFont(font)
        self.setFont(self.reader_font)
        self.document().setDefaultFont(self.reader_font)
        self.document().setDefaultStyleSheet(self._css())
        if self._entry:
            self._render()

    def _base_pt(self) -> float:
        size = self.reader_font.pointSizeF()
        return size if size > 0 else 11.0

    def _css(self) -> str:
        pal = self.palette()
        link = pal.color(QPalette.ColorRole.Link).name()
        muted = pal.color(QPalette.ColorRole.PlaceholderText).name()
        code_bg = pal.color(QPalette.ColorRole.AlternateBase).name()
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        b = self._base_pt()
        return f"""
            a {{ color: {link}; text-decoration: none; }}
            p, li {{ line-height: 150%; }}
            p {{ margin-top: 0px; margin-bottom: 12px; }}
            h1 {{ font-size: {b * 1.6:.1f}pt; font-weight: 700; margin-top: 22px; margin-bottom: 8px; }}
            h2 {{ font-size: {b * 1.4:.1f}pt; font-weight: 700; margin-top: 20px; margin-bottom: 8px; }}
            h3 {{ font-size: {b * 1.2:.1f}pt; font-weight: 700; margin-top: 18px; margin-bottom: 6px; }}
            h4, h5, h6 {{ font-size: {b * 1.05:.1f}pt; font-weight: 700; margin-top: 14px; margin-bottom: 6px; }}
            blockquote {{ margin-left: 16px; margin-top: 8px; margin-bottom: 8px; color: {muted}; }}
            pre {{ font-family: '{mono}'; font-size: {b * 0.92:.1f}pt; background-color: {code_bg}; }}
            code {{ font-family: '{mono}'; font-size: {b * 0.92:.1f}pt; background-color: {code_bg}; }}
        """

    def _margin(self) -> int:
        """Side margin that keeps the text in a comfortable centered column."""
        return max(28, (self.viewport().width() - self.MAX_COLUMN) // 2)

    def _fit(self, img: QImage) -> QImage:
        max_w = max(120, self.viewport().width() - 2 * self._margin())
        if img.width() > max_w:
            return img.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
        return img

    # ---- rendering ------------------------------------------------------
    def show_entry(self, entry: dict):
        self._entry = entry
        self.base_url = QUrl(entry.get("link", ""))
        self._render(keep_scroll=False)

    def clear_entry(self):
        self._entry = None
        self._html = ""
        self.clear()

    def _build_html(self) -> str:
        e = self._entry
        b = self._base_pt()
        muted = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        title = html.escape(e["title"])
        meta = html.escape("  ·  ".join(p for p in (e["feed_title"], e["date_str"]) if p))
        body = strip_img_size(e["html"]) or (
            "<p><i>This feed item has no content. Use “Open in Browser”.</i></p>"
        )
        footer = ""
        if e["link"]:
            footer = (
                f'<p><a href="{html.escape(e["link"], quote=True)}">'
                "Read the original article →</a></p>"
            )
        return (
            f'<div style="font-size:{b * 1.75:.1f}pt; font-weight:700; line-height:125%;">{title}</div>'
            f'<p style="color:{muted}; font-size:{b * 0.85:.1f}pt; '
            f'margin-top:6px; margin-bottom:10px;">{meta}</p><hr>'
            f"{body}{footer}"
        )

    def _render(self, keep_scroll: bool = True):
        if not self._entry:
            return
        bar = self.verticalScrollBar()
        pos = bar.value() if keep_scroll else 0
        self.document().setDocumentMargin(self._margin())
        self._html = self._build_html()
        self.setHtml(self._html)
        bar.setValue(pos)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start()

    def _relayout(self):
        """After a resize: re-fit already-downloaded images, then redraw."""
        doc = self.document()
        for key, img in self._images.items():
            doc.addResource(QTextDocument.ResourceType.ImageResource, self._names[key], self._fit(img))
        self._render()

    # ---- image loading --------------------------------------------------
    def loadResource(self, rtype, name):
        if enum_value(rtype) != enum_value(QTextDocument.ResourceType.ImageResource):
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
        self._names[key] = orig_name
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
        self.resize(1280, 760)

        self.settings = QSettings("simple-rss", APP_NAME)
        self.nam = QNetworkAccessManager(self)
        self.nam.setRedirectPolicy(QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)

        self.feeds: list[dict] = []          # [{"url", "title", "folder"}]
        self.folders: list[str] = []         # folder names (order = display order)
        self.collapsed: set[str] = set()     # folders the user collapsed
        self.entries: dict[str, list] = {}   # feed url -> list of entry dicts
        self.current_items: list[dict] = []  # entries shown in the article list

        self._build_ui()
        self._build_toolbar()
        self._load_data()
        self._load_reader_font()
        self._apply_proxy()
        self._restore_window_state()
        self._rebuild_tree(select=("all", ""))
        self.refresh_all()

    # ---- UI construction ------------------------------------------------
    def _build_ui(self):
        self.tree = FeedTree()
        self.article_list = QListWidget()
        self.article_list.setFrameShape(QFrame.Shape.NoFrame)
        self.article_list.setItemDelegate(ArticleDelegate(self.article_list))
        self.article_list.setUniformItemSizes(True)
        self.article_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.browser = ArticleBrowser(self.nam)
        self.browser.setPlaceholderText("Select an article to read it.")

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.article_list)
        self.splitter.addWidget(self.browser)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([250, 380, 650])
        self.setCentralWidget(self.splitter)

        self.tree.currentItemChanged.connect(self._on_tree_changed)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemExpanded.connect(self._on_folder_toggled)
        self.tree.itemCollapsed.connect(self._on_folder_toggled)
        self.tree.feed_dropped.connect(
            lambda url, folder: QTimer.singleShot(0, lambda: self._move_feed(url, folder))
        )
        self.article_list.currentRowChanged.connect(self._show_article)
        self.statusBar().showMessage("Ready")

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def act(text, icon, slot, shortcut=None, tip=None):
            a = QAction(themed(*icon) if icon else QIcon(), text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.setToolTip(tip or text)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        act("Add Feed", ("list-add",), self.add_feed, "Ctrl+N")
        act("New Folder", ("folder-new",), self.new_folder, "Ctrl+Shift+N")
        self.act_rename = act("Edit", ("edit-rename",), self.rename_selected, "F2",
                              "Edit feed / rename folder (F2)")
        self.act_delete = act("Delete", ("edit-delete",), self.delete_selected,
                              QKeySequence.StandardKey.Delete, "Delete folder or feed (Del)")
        tb.addSeparator()
        act("Refresh", ("view-refresh",), self.refresh_all, "F5")
        act("Open in Browser", ("internet-web-browser", "text-html"), self.open_in_browser)
        tb.addSeparator()

        self.proxy_action = QAction(
            themed("preferences-system-network-proxy", "network-connect"), "Use Proxy", self
        )
        self.proxy_action.setCheckable(True)
        self.proxy_action.setChecked(self.settings.value("proxy/enabled", False, type=bool))
        self.proxy_action.toggled.connect(self._on_proxy_toggled)
        tb.addAction(self.proxy_action)
        act("Proxy Settings…", ("configure",), self._edit_proxy)
        tb.addSeparator()

        act("Reader Font…", ("preferences-desktop-font", "format-text-bold"), self.choose_font)
        zoom_out = act("Smaller text", ("zoom-out", "format-font-size-less"),
                       lambda: self.zoom(-1), QKeySequence.StandardKey.ZoomOut)
        zoom_in = act("Larger text", ("zoom-in", "format-font-size-more"),
                      lambda: self.zoom(+1), QKeySequence.StandardKey.ZoomIn)
        reset = QAction(self)
        reset.setShortcut("Ctrl+0")
        reset.triggered.connect(self.zoom_reset)
        self.addAction(reset)

        # keep the toolbar compact: icon-only for these
        for a in (self.act_rename, self.act_delete, zoom_out, zoom_in):
            btn = tb.widgetForAction(a)
            if btn is not None and not a.icon().isNull():
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

    # ---- Persistence ----------------------------------------------------
    @staticmethod
    def _json_list(raw) -> list:
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    def _load_data(self):
        self.feeds = []
        for f in self._json_list(self.settings.value("feeds", "[]")):
            if isinstance(f, dict) and f.get("url"):
                self.feeds.append(
                    {"url": f["url"], "title": f.get("title") or f["url"],
                     "folder": f.get("folder", "") or "",
                     "custom_title": bool(f.get("custom_title", False))}
                )
        self.folders = [n for n in self._json_list(self.settings.value("folders", "[]"))
                        if isinstance(n, str) and n]
        for f in self.feeds:  # make sure every referenced folder exists
            if f["folder"] and f["folder"] not in self.folders:
                self.folders.append(f["folder"])
        self.collapsed = set(self._json_list(self.settings.value("ui/collapsed", "[]")))

    def _save_data(self):
        self.settings.setValue("feeds", json.dumps(self.feeds))
        self.settings.setValue("folders", json.dumps(self.folders))
        self.settings.setValue("ui/collapsed", json.dumps(sorted(self.collapsed)))

    def _restore_window_state(self):
        geo = self.settings.value("ui/geometry")
        if geo:
            self.restoreGeometry(geo)
        state = self.settings.value("ui/splitter")
        if state:
            self.splitter.restoreState(state)

    def closeEvent(self, event):
        self.settings.setValue("ui/geometry", self.saveGeometry())
        self.settings.setValue("ui/splitter", self.splitter.saveState())
        super().closeEvent(event)

    # ---- Reader font ----------------------------------------------------
    def _default_reader_font(self) -> QFont:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        size = f.pointSizeF()
        f.setPointSizeF((size if size > 0 else 10.0) + 1.5)
        return f

    def _load_reader_font(self):
        font = QFont()
        saved = self.settings.value("reader/font", "")
        if not (saved and font.fromString(saved)):
            font = self._default_reader_font()
        self.browser.set_reader_font(font)

    def _set_reader_font(self, font: QFont):
        self.browser.set_reader_font(font)
        self.settings.setValue("reader/font", font.toString())

    def choose_font(self):
        res = QFontDialog.getFont(self.browser.reader_font, self, "Reader Font")
        ok, font = res if isinstance(res[0], bool) else (res[1], res[0])
        if ok:
            self._set_reader_font(font)

    def zoom(self, step: int):
        font = QFont(self.browser.reader_font)
        size = max(8.0, min(32.0, self.browser._base_pt() + step))
        font.setPointSizeF(size)
        self._set_reader_font(font)
        self.statusBar().showMessage(f"Reader text size: {size:g} pt")

    def zoom_reset(self):
        font = QFont(self.browser.reader_font)
        font.setPointSizeF(self._default_reader_font().pointSizeF())
        self._set_reader_font(font)

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
            self.nam.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
            self.statusBar().showMessage("Proxy OFF (direct connection)")

    def _on_proxy_toggled(self, checked: bool):
        if checked and not self.settings.value("proxy/host", ""):
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

    # ---- Tree: building / selection --------------------------------------
    def _make_item(self, kind: str, key: str, text: str, icon: QIcon, draggable=False):
        item = QTreeWidgetItem([text])
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_KEY, key)
        item.setIcon(0, icon)
        if not draggable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
        return item

    def _rebuild_tree(self, select=None):
        sel = select or self._current_key()
        self.tree.blockSignals(True)
        self.tree.clear()

        icon_all = themed("view-list-text", "mail-folder-inbox", "folder")
        icon_folder = themed("folder", "inode-directory")
        icon_feed = themed("application-rss+xml", "rss", "text-html")

        self.tree.addTopLevelItem(self._make_item("all", "", "All Feeds", icon_all))
        folder_items = {}
        for name in self.folders:
            item = self._make_item("folder", name, name, icon_folder)
            folder_items[name] = item
            self.tree.addTopLevelItem(item)
        for f in self.feeds:  # feeds without a folder end up after the folders
            item = self._make_item("feed", f["url"], f.get("title") or f["url"],
                                   icon_feed, draggable=True)
            item.setToolTip(0, f["url"])
            parent = folder_items.get(f["folder"])
            if parent is not None:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
        for name, item in folder_items.items():
            item.setExpanded(name not in self.collapsed)

        self.tree.blockSignals(False)
        self._select_key(sel)

    def _iter_items(self):
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            top = root.child(i)
            yield top
            for j in range(top.childCount()):
                yield top.child(j)

    def _select_key(self, sel):
        target = None
        if sel:
            for item in self._iter_items():
                if (item.data(0, ROLE_KIND), item.data(0, ROLE_KEY)) == tuple(sel):
                    target = item
                    break
        self.tree.setCurrentItem(target or self.tree.topLevelItem(0))

    def _current_key(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        return (item.data(0, ROLE_KIND), item.data(0, ROLE_KEY))

    def _on_folder_toggled(self, item: QTreeWidgetItem):
        if item.data(0, ROLE_KIND) != "folder":
            return
        name = item.data(0, ROLE_KEY)
        if item.isExpanded():
            self.collapsed.discard(name)
        else:
            self.collapsed.add(name)
        self.settings.setValue("ui/collapsed", json.dumps(sorted(self.collapsed)))

    def _on_tree_changed(self, current, _previous):
        kind = current.data(0, ROLE_KIND) if current else None
        self.act_rename.setEnabled(kind in ("folder", "feed"))
        self.act_delete.setEnabled(kind in ("folder", "feed"))
        self._populate_articles(preserve=False)

    def _urls_for_selection(self) -> list[str]:
        sel = self._current_key()
        if not sel:
            return []
        kind, key = sel
        if kind == "all":
            return [f["url"] for f in self.feeds]
        if kind == "folder":
            return [f["url"] for f in self.feeds if f["folder"] == key]
        return [key]

    def _folder_context(self) -> str:
        """Folder the user is 'in' right now (used as the default for new feeds)."""
        sel = self._current_key()
        if not sel:
            return ""
        kind, key = sel
        if kind == "folder":
            return key
        if kind == "feed":
            return next((f["folder"] for f in self.feeds if f["url"] == key), "")
        return ""

    # ---- Tree: context menu ------------------------------------------------
    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        kind = item.data(0, ROLE_KIND) if item else None
        key = item.data(0, ROLE_KEY) if item else None

        if kind == "folder":
            menu.addAction(themed("list-add"), "Add Feed Here…",
                           lambda: self._add_feed_dialog(key))
            menu.addAction(themed("view-refresh"), "Refresh Folder",
                           lambda: self._refresh_urls(
                               [f["url"] for f in self.feeds if f["folder"] == key]))
            menu.addSeparator()
            menu.addAction(themed("edit-rename"), "Rename…", lambda: self._rename_folder(key))
            menu.addAction(themed("edit-delete"), "Delete Folder…",
                           lambda: self._delete_folder(key))
        elif kind == "feed":
            menu.addAction(themed("view-refresh"), "Refresh Feed",
                           lambda: self._refresh_urls([key]))
            menu.addAction(themed("edit-rename"), "Edit Feed…", lambda: self._edit_feed(key))
            current = next((f["folder"] for f in self.feeds if f["url"] == key), "")
            move = menu.addMenu(themed("folder-move", "go-next"), "Move to")
            a = move.addAction("(No folder)")
            a.setEnabled(current != "")
            a.triggered.connect(lambda: self._move_feed(key, ""))
            for name in self.folders:
                a = move.addAction(name)
                a.setEnabled(name != current)
                a.triggered.connect(lambda _=False, n=name: self._move_feed(key, n))
            menu.addSeparator()
            menu.addAction(themed("edit-delete"), "Remove Feed…", lambda: self._delete_feed(key))
        else:
            menu.addAction(themed("list-add"), "Add Feed…", self.add_feed)
            menu.addAction(themed("folder-new"), "New Folder…", self.new_folder)
            if kind == "all":
                menu.addAction(themed("view-refresh"), "Refresh All", self.refresh_all)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---- Folder / feed management ------------------------------------------
    def _ask_folder_name(self, title: str, initial: str = "") -> str | None:
        name = initial
        while True:
            name, ok = QInputDialog.getText(self, title, "Folder name:", text=name)
            if not ok:
                return None
            name = name.strip()
            if not name:
                QMessageBox.warning(self, title, "The folder name can't be empty.")
                continue
            if any(f.lower() == name.lower() and f != initial for f in self.folders):
                QMessageBox.warning(self, title, f"A folder named “{name}” already exists.")
                continue
            return name

    def new_folder(self):
        name = self._ask_folder_name("New Folder")
        if name is None:
            return
        self.folders.append(name)
        self._save_data()
        self._rebuild_tree(select=("folder", name))

    def rename_selected(self):
        sel = self._current_key()
        if not sel:
            return
        if sel[0] == "folder":
            self._rename_folder(sel[1])
        elif sel[0] == "feed":
            self._edit_feed(sel[1])

    def _rename_folder(self, old: str):
        new = self._ask_folder_name("Rename Folder", initial=old)
        if new is None or new == old:
            return
        self.folders[self.folders.index(old)] = new
        for f in self.feeds:
            if f["folder"] == old:
                f["folder"] = new
        if old in self.collapsed:
            self.collapsed.discard(old)
            self.collapsed.add(new)
        self._save_data()
        self._rebuild_tree(select=("folder", new))

    def _edit_feed(self, url: str):
        feed = next((f for f in self.feeds if f["url"] == url), None)
        if feed is None:
            return
        dlg = EditFeedDialog(feed, self.folders, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        title, new_url, folder = dlg.values()
        if "://" not in new_url:
            new_url = "https://" + new_url
        if new_url != url and any(f["url"] == new_url for f in self.feeds):
            QMessageBox.warning(self, "Edit Feed", "You're already subscribed to that URL.")
            return

        refetch = False
        if not title:
            if feed.get("custom_title"):  # back to the feed's own title
                feed["custom_title"] = False
                feed["title"] = new_url   # real title is picked up on the next fetch
                refetch = True
        elif title != feed["title"]:
            feed["title"] = title
            feed["custom_title"] = True

        if new_url != url:
            self.entries.pop(url, None)   # old articles belong to the old URL
            feed["url"] = new_url
            if not feed.get("custom_title"):
                feed["title"] = new_url
            refetch = True

        feed["folder"] = folder
        if folder:
            self.collapsed.discard(folder)
        for e in self.entries.get(feed["url"], []):
            e["feed_title"] = feed["title"]

        self._save_data()
        self._rebuild_tree(select=("feed", feed["url"]))
        if refetch:
            self._fetch(feed["url"])

    def delete_selected(self):
        sel = self._current_key()
        if not sel:
            return
        if sel[0] == "folder":
            self._delete_folder(sel[1])
        elif sel[0] == "feed":
            self._delete_feed(sel[1])

    def _delete_folder(self, name: str):
        members = [f for f in self.feeds if f["folder"] == name]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Delete Folder")
        box.setText(f"Delete the folder “{name}”?")
        keep_btn = None
        if members:
            n = len(members)
            box.setInformativeText(f"It contains {n} feed{'s' if n != 1 else ''}.")
            keep_btn = box.addButton("Keep Feeds", QMessageBox.ButtonRole.AcceptRole)
            wipe_btn = box.addButton("Delete Feeds Too", QMessageBox.ButtonRole.DestructiveRole)
        else:
            wipe_btn = box.addButton("Delete", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is None or clicked not in (keep_btn, wipe_btn):
            return
        if clicked is wipe_btn:
            for f in members:
                self.entries.pop(f["url"], None)
            self.feeds = [f for f in self.feeds if f["folder"] != name]
        else:  # keep feeds: they move to the top level
            for f in members:
                f["folder"] = ""
        self.folders.remove(name)
        self.collapsed.discard(name)
        self._save_data()
        self._rebuild_tree(select=("all", ""))

    def _delete_feed(self, url: str):
        feed = next((f for f in self.feeds if f["url"] == url), None)
        if feed is None:
            return
        answer = QMessageBox.question(
            self, "Remove Feed", f"Remove “{feed['title']}”?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.feeds.remove(feed)
        self.entries.pop(url, None)
        self._save_data()
        self._rebuild_tree(select=("folder", feed["folder"]) if feed["folder"] else ("all", ""))

    def _move_feed(self, url: str, folder: str):
        feed = next((f for f in self.feeds if f["url"] == url), None)
        if feed is None or feed["folder"] == folder:
            return
        if folder and folder not in self.folders:
            return
        feed["folder"] = folder
        if folder:
            self.collapsed.discard(folder)  # make the moved feed visible
        self._save_data()
        self._rebuild_tree(select=("feed", url))

    def add_feed(self):
        self._add_feed_dialog(self._folder_context())

    def _add_feed_dialog(self, folder: str):
        dlg = AddFeedDialog(self.folders, folder, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        url, folder = dlg.values()
        if "://" not in url:
            url = "https://" + url
        if any(f["url"] == url for f in self.feeds):
            QMessageBox.information(self, "Add Feed", "You're already subscribed to that feed.")
            return
        self.feeds.append({"url": url, "title": url, "folder": folder})
        if folder:
            self.collapsed.discard(folder)
        self._save_data()
        self._rebuild_tree(select=("feed", url))
        self._fetch(url)

    # ---- Networking -----------------------------------------------------
    def refresh_all(self):
        self._refresh_urls([f["url"] for f in self.feeds])

    def _refresh_urls(self, urls: list[str]):
        for u in urls:
            self._fetch(u)

    def _fetch(self, url: str):
        req = QNetworkRequest(QUrl(url))
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
        feed = next((f for f in self.feeds if f["url"] == url), None)
        if feed is None:  # removed while the request was in flight
            return

        parsed = feedparser.parse(bytes(reply.readAll().data()))
        if parsed.bozo and not parsed.entries:
            self.statusBar().showMessage(f"Not a valid RSS/Atom feed: {url}")
            return

        title = parsed.feed.get("title", "").strip()
        if title and feed["title"] != title and not feed.get("custom_title"):
            feed["title"] = title
            self._save_data()
            for item in self._iter_items():
                if item.data(0, ROLE_KIND) == "feed" and item.data(0, ROLE_KEY) == url:
                    item.setText(0, title)
        feed_title = feed["title"] or url

        items = []
        for e in parsed.entries:
            content = ""
            if e.get("content"):
                content = e["content"][0].get("value", "")
            content = content or e.get("summary", "") or e.get("description", "")
            if "<img" not in content.lower():
                pic = media_image(e)
                if pic:
                    content = f'<p><img src="{html.escape(pic, quote=True)}"></p>' + content
            tm = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(tm) if tm else 0
            date_str = (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                if ts else e.get("published", e.get("updated", ""))
            )
            items.append(
                {
                    "title": e.get("title", "(untitled)"),
                    "link": e.get("link", ""),
                    "date_str": date_str,
                    "ts": ts,
                    "html": content,
                    "feed_url": url,
                    "feed_title": feed_title,
                }
            )
        self.entries[url] = items
        self.statusBar().showMessage(f"Loaded {len(items)} articles from {feed_title}")

        if url in self._urls_for_selection():
            self._populate_articles(preserve=True)

    # ---- Article list / display -----------------------------------------
    def _populate_articles(self, preserve: bool):
        keep_link = None
        if preserve:
            cur = self._current_entry()
            keep_link = cur["link"] if cur else None

        urls = self._urls_for_selection()
        items = []
        for u in urls:
            items.extend(self.entries.get(u, []))
        multi = len(urls) > 1
        if multi:  # newest first across feeds (stable for undated items)
            items.sort(key=lambda i: i["ts"], reverse=True)
        self.current_items = items

        vbar = self.article_list.verticalScrollBar()
        scroll = vbar.value() if preserve else 0
        row_to_select = -1
        self.article_list.blockSignals(True)
        self.article_list.clear()
        for i, it in enumerate(items):
            li = QListWidgetItem(it["title"])
            meta = "  ·  ".join(p for p in ((it["feed_title"] if multi else ""), it["date_str"]) if p)
            li.setData(ROLE_META, meta)
            li.setToolTip(it["title"])
            self.article_list.addItem(li)
            if keep_link and row_to_select < 0 and it["link"] == keep_link:
                row_to_select = i
        if row_to_select >= 0:
            self.article_list.setCurrentRow(row_to_select)
        self.article_list.blockSignals(False)
        vbar.setValue(scroll)

        if row_to_select < 0:
            self.browser.clear_entry()

    def _current_entry(self):
        row = self.article_list.currentRow()
        if 0 <= row < len(self.current_items):
            return self.current_items[row]
        return None

    def _show_article(self, _row: int):
        e = self._current_entry()
        if e:
            self.browser.show_entry(e)

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
