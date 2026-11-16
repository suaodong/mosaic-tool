"""极简图片打码工具

打开本地图片或 PDF，按住鼠标左键拖拽框选区域，松开即对选区打码；
支持马赛克 / 高斯模糊两种方式、强度调节、撤销；
「保存 / 导出」按钮下拉直接选择格式：PNG / JPEG / BMP / WEBP / TIFF / PDF。
纯本地运行，无需联网。
"""

from __future__ import annotations

import sys

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

PDF_RENDER_DPI = 200  # PDF 页面渲染清晰度（同时作为导出 PDF 的页面分辨率）

# 可导出的格式：(格式键, 菜单显示名, 文件对话框过滤器)
SAVE_FORMATS = [
    ("png",  "PNG 图片",  "PNG 图片 (*.png)"),
    ("jpg",  "JPEG 图片", "JPEG 图片 (*.jpg *.jpeg)"),
    ("bmp",  "BMP 位图",  "BMP 位图 (*.bmp)"),
    ("webp", "WEBP 图片", "WEBP 图片 (*.webp)"),
    ("tif",  "TIFF 图片", "TIFF 图片 (*.tif *.tiff)"),
    ("pdf",  "PDF 文档",  "PDF 文档 (*.pdf)"),
]
# 各格式可接受的扩展名（用户手动输入时识别）
EXT_VARIANTS = {"jpg": (".jpg", ".jpeg"), "tif": (".tif", ".tiff")}


class ImageLabel(QLabel):
    """图片显示控件：负责缩放显示、拖拽框选与界面坐标 -> 原图坐标映射。

    选区完成后通过 selectionMade 信号发出【原图像素坐标】下的矩形 (QRect)。
    """

    selectionMade = pyqtSignal(QRect)

    def __init__(self):
        super().__init__()
        self._image: Image.Image | None = None  # 原图（PIL）
        self._scaled: QPixmap | None = None     # 当前窗口尺寸下的缩放缓存
        self._draw_rect = QRect()               # 缩放图在 label 内的居中绘制区域
        self._layout_key = None                 # 缩放缓存的有效性标识
        self._drag_start: QPoint | None = None
        self._drag_end: QPoint | None = None
        self.setMinimumSize(200, 150)

    # ---------- 对外接口 ----------
    def set_image(self, image: Image.Image):
        """设置要显示的原图（PIL），并清除选区。"""
        self._image = image
        self._layout_key = None  # 强制重建缩放缓存（图像可能已被原地修改）
        self.clear_selection()

    def clear_selection(self):
        self._drag_start = self._drag_end = None
        self.update()

    def has_image(self) -> bool:
        return self._image is not None

    # ---------- 坐标映射（核心） ----------
    def _layout(self) -> QRect:
        """计算（并缓存）缩放图及其在 label 内的居中绘制区域。

        缩放使用 Pillow LANCZOS 算法（画质优于 Qt 自带缩放），并按屏幕
        devicePixelRatio 输出物理像素尺寸的 pixmap，保证高 DPI 屏幕下清晰。
        """
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr)
        if self._layout_key != key:
            iw, ih = self._image.size
            scale = min(self.width() / iw, self.height() / ih)
            w = max(1, round(iw * scale))
            h = max(1, round(ih * scale))
            self._draw_rect = QRect(
                (self.width() - w) // 2, (self.height() - h) // 2, w, h
            )
            # 高质量缩放到物理像素，再标注 DPR，Qt 会按逻辑尺寸清晰绘制
            pw, ph = max(1, round(w * dpr)), max(1, round(h * dpr))
            resized = self._image.resize((pw, ph), Image.Resampling.LANCZOS)
            data = resized.tobytes("raw", "RGB")
            qimg = QImage(data, pw, ph, pw * 3, QImage.Format.Format_RGB888).copy()
            self._scaled = QPixmap.fromImage(qimg)
            self._scaled.setDevicePixelRatio(dpr)
            self._layout_key = key
        return self._draw_rect

    def _to_image_coords(self, pt: QPoint) -> tuple[int, int]:
        """label 坐标 -> 原图像素坐标（clamp 到图像范围内）。"""
        rect = self._layout()
        pw, ph = self._image.size
        ratio = pw / rect.width()  # 原图像素 / 界面像素
        x = round((pt.x() - rect.x()) * ratio)
        y = round((pt.y() - rect.y()) * ratio)
        return min(max(x, 0), pw), min(max(y, 0), ph)

    def to_image_rect(self, p1: QPoint, p2: QPoint) -> QRect | None:
        """两个 label 坐标点 -> 原图矩形；退化（面积过小）时返回 None。"""
        x1, y1 = self._to_image_coords(p1)
        x2, y2 = self._to_image_coords(p2)
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        return QRect(x1, y1, x2 - x1, y2 - y1)

    # ---------- 鼠标交互 ----------
    def mousePressEvent(self, event: QMouseEvent):
        if self._image is not None and event.button() == Qt.MouseButton.LeftButton:
            pt = event.position().toPoint()
            if self._layout().contains(pt):  # 只有按在图片区域内才开始框选
                self._drag_start = self._drag_end = pt
                self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start is not None:
            self._drag_end = event.position().toPoint()
            self.update()  # 实时重绘红色选框

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_start is not None and event.button() == Qt.MouseButton.LeftButton:
            rect = self.to_image_rect(self._drag_start, event.position().toPoint())
            self._drag_start = self._drag_end = None
            self.update()
            if rect is not None:
                self.selectionMade.emit(rect)

    # ---------- 绘制 ----------
    def paintEvent(self, event: QPaintEvent):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f0f0f0"))
        if self._image is None:
            painter.setPen(QColor("#888888"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter,
                "请点击上方按钮打开图片或 PDF 文件",
            )
        else:
            draw_rect = self._layout()
            painter.drawPixmap(draw_rect.topLeft(), self._scaled)
            # 拖拽中的红色选框（overlay 方式绘制，不修改图像本身）
            if self._drag_start is not None and self._drag_end is not None:
                sel = (
                    QRect(self._drag_start, self._drag_end)
                    .normalized()
                    .intersected(draw_rect)
                )
                if not sel.isEmpty():
                    painter.setPen(QPen(QColor(255, 0, 0), 2, Qt.PenStyle.DashLine))
                    painter.drawRect(sel)
        # 边框
        painter.setPen(QColor("#cccccc"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()


class MosaicTool(QMainWindow):
    MAX_HISTORY = 20  # 撤销栈上限，防止内存膨胀
    MODE_MOSAIC = 0
    MODE_BLUR = 1

    def __init__(self):
        super().__init__()
        self.setWindowTitle("极简图片打码工具")
        self.resize(800, 600)

        self.pages: list[Image.Image] = []  # 全部页面（普通图片 = 单页）
        self.page_index = 0                 # 当前页码
        self.history: list[tuple[int, Image.Image]] = []  # (页码, 打码前该页副本)

        self._init_ui()

    @property
    def current_image(self) -> Image.Image | None:
        return self.pages[self.page_index] if self.pages else None

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 顶部工具区
        bar = QHBoxLayout()
        self.btn_open = QPushButton("打开文件")
        self.btn_save = QPushButton("保存 / 导出")
        self.btn_clear = QPushButton("清除选区")
        self.btn_undo = QPushButton("撤销")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["马赛克", "高斯模糊"])
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(4, 30)
        self.strength_slider.setValue(10)
        self.strength_slider.setFixedWidth(140)
        self.strength_label = QLabel("强度: 10")

        # 翻页导航（仅多页 PDF 时显示）
        self.btn_prev = QPushButton("◀ 上一页")
        self.page_label = QLabel("1 / 1")
        self.btn_next = QPushButton("下一页 ▶")

        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_save)
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_undo)
        bar.addSpacing(20)
        bar.addWidget(QLabel("方式:"))
        bar.addWidget(self.mode_combo)
        bar.addWidget(QLabel("强度:"))
        bar.addWidget(self.strength_slider)
        bar.addWidget(self.strength_label)
        bar.addStretch(1)
        bar.addWidget(self.btn_prev)
        bar.addWidget(self.page_label)
        bar.addWidget(self.btn_next)
        layout.addLayout(bar)

        # 图片显示区
        self.image_label = ImageLabel()
        layout.addWidget(self.image_label, stretch=1)

        # 「保存 / 导出」下拉菜单：直接选择输出格式
        save_menu = QMenu(self)
        for fmt, title, _filter in SAVE_FORMATS:
            action = save_menu.addAction(f"{title} (.{fmt})")
            action.triggered.connect(
                lambda checked=False, f=fmt: self.save_as(f)
            )
        self.btn_save.setMenu(save_menu)

        # 事件绑定
        self.btn_open.clicked.connect(self.open_file)
        self.btn_clear.clicked.connect(self.image_label.clear_selection)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_prev.clicked.connect(lambda: self._goto_page(self.page_index - 1))
        self.btn_next.clicked.connect(lambda: self._goto_page(self.page_index + 1))
        self.strength_slider.valueChanged.connect(
            lambda v: self.strength_label.setText(f"强度: {v}")
        )
        self.image_label.selectionMade.connect(self.apply_redaction)

        self._update_buttons()
        self._update_page_nav()

    def _update_buttons(self):
        self.btn_save.setEnabled(bool(self.pages))
        self.btn_undo.setEnabled(bool(self.history))

    # ---------- 打开 ----------
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片或 PDF",
            "",
            "所有支持 (*.png *.jpg *.jpeg *.bmp *.pdf);;"
            "图片 (*.png *.jpg *.jpeg *.bmp);;PDF 文档 (*.pdf)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".pdf"):
                pages = self._load_pdf(path)
            else:
                pages = [Image.open(path).convert("RGB")]
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开文件：\n{exc}")
            return
        if not pages:
            QMessageBox.warning(self, "打开失败", "该 PDF 中没有页面。")
            return
        self.pages = pages
        self.page_index = 0
        self.history.clear()
        self._refresh_display()
        self._update_buttons()

    @staticmethod
    def _load_pdf(path: str) -> list[Image.Image]:
        """将 PDF 的每一页渲染为 RGB 图片（保留全部页面，供逐页打码）。"""
        doc = pdfium.PdfDocument(path)
        try:
            pages = []
            for i in range(len(doc)):
                page = doc[i]
                bitmap = page.render(scale=PDF_RENDER_DPI / 72)
                pages.append(bitmap.to_pil().convert("RGB"))
                page.close()
            return pages
        finally:
            doc.close()

    # ---------- 翻页 ----------
    def _goto_page(self, idx: int):
        if 0 <= idx < len(self.pages) and idx != self.page_index:
            self.page_index = idx
            self._refresh_display()

    def _update_page_nav(self):
        n = len(self.pages)
        multi = n > 1
        self.btn_prev.setVisible(multi)
        self.page_label.setVisible(multi)
        self.btn_next.setVisible(multi)
        self.page_label.setText(f"{self.page_index + 1} / {n}")
        self.btn_prev.setEnabled(self.page_index > 0)
        self.btn_next.setEnabled(self.page_index < n - 1)

    # ---------- 保存 ----------
    def save_as(self, fmt: str):
        """以指定格式导出（由保存按钮的下拉菜单触发）。"""
        if self.current_image is None:
            return
        title, filt = next((t, f) for k, t, f in SAVE_FORMATS if k == fmt)
        path, _ = QFileDialog.getSaveFileName(
            self, f"保存为 {title}", f"output.{fmt}", filt
        )
        if not path:
            return
        path = self._ensure_ext(path, fmt)
        try:
            self._save_to_path(path, fmt)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"无法保存文件：\n{exc}")
            return
        note = ""
        if fmt != "pdf" and len(self.pages) > 1:
            note = "\n（提示：多页文档导出为图片格式时仅保存当前页）"
        QMessageBox.information(self, "保存成功", f"文件已保存到：\n{path}{note}")

    @staticmethod
    def _ensure_ext(path: str, fmt: str) -> str:
        """用户未输入扩展名时，按所选格式补全。"""
        if not path.lower().endswith(EXT_VARIANTS.get(fmt, (f".{fmt}",))):
            path += f".{fmt}"
        return path

    def _save_to_path(self, path: str, fmt: str):
        if fmt == "pdf":
            # 多页重新组装：打码页替换原页，总页数不丢失
            first, rest = self.pages[0], self.pages[1:]
            first.save(
                path, "PDF", save_all=True, append_images=rest,
                resolution=float(PDF_RENDER_DPI),
            )
        elif fmt == "jpg":
            self.current_image.save(path, quality=95)
        else:
            self.current_image.save(path)

    # ---------- 打码 / 撤销 ----------
    def apply_redaction(self, rect: QRect):
        """对当前页【原图坐标】下的 rect 区域打码（马赛克或高斯模糊）。"""
        img = self.current_image
        if img is None:
            return
        img_w, img_h = img.size
        x1, y1 = max(0, rect.x()), max(0, rect.y())
        x2 = min(img_w, rect.x() + rect.width())
        y2 = min(img_h, rect.y() + rect.height())
        if x2 - x1 < 2 or y2 - y1 < 2:
            return

        self._push_history()

        region = img.crop((x1, y1, x2, y2))
        strength = self.strength_slider.value()
        if self.mode_combo.currentIndex() == self.MODE_MOSAIC:
            # 先极度缩小丢失细节，再用最近邻放大回块状的“马赛克”效果
            small = region.resize(
                (max(1, (x2 - x1) // strength), max(1, (y2 - y1) // strength)),
                Image.Resampling.BILINEAR,
            )
            region = small.resize((x2 - x1, y2 - y1), Image.Resampling.NEAREST)
        else:
            region = region.filter(ImageFilter.GaussianBlur(radius=strength))

        img.paste(region, (x1, y1))
        self._refresh_display()
        self._update_buttons()

    def undo(self):
        if not self.history:
            return
        idx, img = self.history.pop()
        self.pages[idx] = img
        self.page_index = idx
        self._refresh_display()
        self._update_buttons()

    def _push_history(self):
        self.history.append((self.page_index, self.current_image.copy()))
        if len(self.history) > self.MAX_HISTORY:
            self.history.pop(0)

    # ---------- 显示刷新 ----------
    def _refresh_display(self):
        if self.current_image is not None:
            self.image_label.set_image(self.current_image)
        self._update_page_nav()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MosaicTool()
    window.show()
    sys.exit(app.exec())
