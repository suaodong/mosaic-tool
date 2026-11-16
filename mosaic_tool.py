"""极简图片打码工具

打开本地图片后，按住鼠标左键拖拽框选区域，松开即对选区打码；
支持马赛克 / 高斯模糊两种方式、强度调节、撤销与保存。
纯本地运行，无需联网。
"""

from __future__ import annotations

import os
import sys

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
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


class ImageLabel(QLabel):
    """图片显示控件：负责缩放显示、拖拽框选与界面坐标 -> 原图坐标映射。

    选区完成后通过 selectionMade 信号发出【原图像素坐标】下的矩形 (QRect)。
    """

    selectionMade = pyqtSignal(QRect)

    def __init__(self):
        super().__init__()
        self._image: Image.Image | None = None  # 原图（PIL）
        self._scaled: QPixmap | None = None     # 当前窗口尺寸下的缩放缓存
        self._draw_rect = QRect()            # 缩放图在 label 内的居中绘制区域
        self._layout_key = None              # 缩放缓存的有效性标识
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
                self.rect(), Qt.AlignmentFlag.AlignCenter, "请点击上方按钮打开图片"
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

        self.image: Image.Image | None = None  # 当前图像（PIL，打码直接作用于它）
        self.history: list[Image.Image] = []   # 撤销栈

        self._init_ui()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 顶部工具区
        bar = QHBoxLayout()
        self.btn_open = QPushButton("打开图片")
        self.btn_save = QPushButton("保存图片")
        self.btn_clear = QPushButton("清除选区")
        self.btn_undo = QPushButton("撤销")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["马赛克", "高斯模糊"])
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(4, 30)
        self.strength_slider.setValue(10)
        self.strength_slider.setFixedWidth(140)
        self.strength_label = QLabel("强度: 10")

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
        layout.addLayout(bar)

        # 图片显示区
        self.image_label = ImageLabel()
        layout.addWidget(self.image_label, stretch=1)

        # 事件绑定
        self.btn_open.clicked.connect(self.open_image)
        self.btn_save.clicked.connect(self.save_image)
        self.btn_clear.clicked.connect(self.image_label.clear_selection)
        self.btn_undo.clicked.connect(self.undo)
        self.strength_slider.valueChanged.connect(
            lambda v: self.strength_label.setText(f"强度: {v}")
        )
        self.image_label.selectionMade.connect(self.apply_redaction)

        self._update_buttons()

    def _update_buttons(self):
        self.btn_save.setEnabled(self.image is not None)
        self.btn_undo.setEnabled(bool(self.history))

    # ---------- 打开 / 保存 ----------
    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not path:
            return
        try:
            self.image = Image.open(path).convert("RGB")
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开图片：\n{exc}")
            return
        self.history.clear()
        self._refresh_display()
        self._update_buttons()

    def save_image(self):
        if self.image is None:
            return
        filters = (
            "PNG 图片 (*.png);;"
            "JPEG 图片 (*.jpg *.jpeg);;"
            "BMP 位图 (*.bmp);;"
            "WEBP 图片 (*.webp);;"
            "TIFF 图片 (*.tif *.tiff)"
        )
        path, selected = QFileDialog.getSaveFileName(
            self, "保存图片", "output.png", filters
        )
        if not path:
            return
        path = self._resolve_save_path(path, selected)
        try:
            if path.lower().endswith((".jpg", ".jpeg")):
                self.image.save(path, quality=95)
            else:
                self.image.save(path)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", f"无法保存图片：\n{exc}")
            return
        QMessageBox.information(self, "保存成功", f"图片已保存到：\n{path}")

    @staticmethod
    def _resolve_save_path(path: str, selected_filter: str) -> str:
        """用户未输入扩展名时，按所选的格式过滤器补全扩展名。"""
        if os.path.splitext(path)[1]:
            return path
        ext_map = [
            ("PNG", ".png"),
            ("JPEG", ".jpg"),
            ("BMP", ".bmp"),
            ("WEBP", ".webp"),
            ("TIFF", ".tif"),
        ]
        for key, ext in ext_map:
            if key in selected_filter:
                return path + ext
        return path + ".png"

    # ---------- 打码 / 撤销 ----------
    def apply_redaction(self, rect: QRect):
        """对【原图坐标】下的 rect 区域打码（马赛克或高斯模糊）。"""
        if self.image is None:
            return
        img_w, img_h = self.image.size
        x1, y1 = max(0, rect.x()), max(0, rect.y())
        x2 = min(img_w, rect.x() + rect.width())
        y2 = min(img_h, rect.y() + rect.height())
        if x2 - x1 < 2 or y2 - y1 < 2:
            return

        self._push_history()

        region = self.image.crop((x1, y1, x2, y2))
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

        self.image.paste(region, (x1, y1))
        self._refresh_display()
        self._update_buttons()

    def undo(self):
        if not self.history:
            return
        self.image = self.history.pop()
        self._refresh_display()
        self._update_buttons()

    def _push_history(self):
        self.history.append(self.image.copy())
        if len(self.history) > self.MAX_HISTORY:
            self.history.pop(0)

    # ---------- 显示刷新 ----------
    def _refresh_display(self):
        self.image_label.set_image(self.image)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MosaicTool()
    window.show()
    sys.exit(app.exec())
