"""一键配置角色包对话框（2026-09-17 用户需求：托盘入口）。

流程：选源文件夹 → scan_card 组件清单预览 → 填 package_id / 选 TTS 权重 →
「一键配置」= import_card（拷贝+校验+回滚保护+参考音频补拷+TTS 权重接线）
→ 可选激活写回（switcher.write_active_package_id）→ 「重启生效」
（注入回调 = 托盘完全退出[含 TTS] + schedule_restart 重拉启动 bat）。

大目录拷贝放 _ImportWorker 工作线程（UI 不冻结）；donor（参考音频供体）
在对话框打开时捕获——激活写回会改 settings，供体必须取写回前的现役包。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from client.card_importer import (CardScan, TtsWiring, import_card,
                                  is_valid_package_id, sanitize_package_id,
                                  scan_card)
from server.constants import SETTINGS_PATH, load_settings, resolve_character_package

logger = logging.getLogger(__name__)


class _ImportWorker(QThread):
    """import_card 工作线程（360MB 级拷贝不冻 UI）。

    run() 全捕获：任何异常转为 failed 信号上报——pythonw 下未捕获异常只会
    消失在黑洞里，表现为「配置中」永久卡死（2026-09-18 自查修正）。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, kwargs: dict, parent=None) -> None:
        super().__init__(parent)
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = import_card(**self._kwargs)
        except Exception:
            import traceback

            logger.error("角色包导入线程异常", exc_info=True)
            self.failed.emit(traceback.format_exc())
            return
        self.done.emit(result)


class CardImportDialog(QDialog):
    """托盘「一键配置角色包」：选文件夹 → 校验 → 一键导入/接线/激活。"""

    def __init__(self, *, restart_callback=None, settings_path=None,
                 characters_root=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("一键配置角色包")
        self.setMinimumWidth(560)
        self._settings_path = Path(settings_path) if settings_path else SETTINGS_PATH
        self._characters_root = (Path(characters_root)
                                 if characters_root else None)
        self._restart_callback = restart_callback
        self._scan: CardScan | None = None
        self._worker: _ImportWorker | None = None
        self._donor = self._current_donor()
        self._build_ui()
        self._load_service_yaml_prefill()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        form = QFormLayout()

        row = QHBoxLayout()
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText("选择包含 character_card.yaml 的角色卡文件夹")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(self._source_edit)
        row.addWidget(browse)
        form.addRow("源文件夹", row)

        self._preview = QListWidget()
        self._preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        form.addRow("组件检查", self._preview)

        self._package_edit = QLineEdit()
        form.addRow("package_id", self._package_edit)

        self._weights_combo = QComboBox()
        self._weights_combo.addItem("不接线（仅导入卡）", None)
        self._weights_combo.setEnabled(False)
        form.addRow("TTS 权重接线", self._weights_combo)

        self._service_yaml_edit = QLineEdit()
        self._service_yaml_edit.setPlaceholderText(
            r"GPT-SoVITS 服务推理 yaml（如 E:\game\gal\tool\GPT-SoVITS"
            r"\_setup\tts_infer_miyako.yaml）；留空=不接线")
        form.addRow("服务 yaml", self._service_yaml_edit)

        self._backfill_box = QCheckBox("源卡缺参考音频池时，从当前激活包补拷候选池")
        self._backfill_box.setChecked(True)
        form.addRow("", self._backfill_box)

        self._activate_box = QCheckBox("导入后设为激活角色（写入 settings.package_id）")
        self._activate_box.setChecked(True)
        form.addRow("", self._activate_box)

        self._overwrite_box = QCheckBox("目标已存在时覆盖（转正/重装；当前角色先有备份再动）")
        form.addRow("", self._overwrite_box)

        self._import_btn = QPushButton("一键配置")
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._start_import)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._import_btn)
        btn_row.addWidget(self._close_btn)
        form.addRow(btn_row)

        self._result = QLabel("")
        self._result.setWordWrap(True)
        form.addRow(self._result)

        self._restart_btn = QPushButton("重启 miyako 生效（完全退出含 TTS → 重拉启动 bat）")
        self._restart_btn.setVisible(False)
        self._restart_btn.clicked.connect(self._restart_now)
        form.addRow(self._restart_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

    def reject(self) -> None:                        # X 与「关闭」都走这里
        """配置进行中禁止关闭——工作线程向已销毁窗口发信号会崩（防误关）。"""
        if self._worker is not None and self._worker.isRunning():
            self._result.setText("⚠️ 正在配置中，请稍候（拷贝完成前不可关闭）")
            return
        super().reject()

    # ---------------- 交互 ----------------

    def _current_donor(self) -> Path | None:
        """参考音频供体 = 打开对话框时的现役激活包（写回前捕获）。"""
        try:
            donor = resolve_character_package(load_settings())
            return donor if donor.is_dir() else None
        except Exception:
            return None

    def _load_service_yaml_prefill(self) -> None:
        try:
            cfg = load_settings()
            prefill = str(((cfg.get("providers") or {})
                           .get("tts_service_yaml")) or "")
        except Exception:
            prefill = ""
        if prefill:
            self._service_yaml_edit.setText(prefill)

    def _browse(self) -> None:
        start = self._source_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择角色卡文件夹", start)
        if not chosen:
            return
        self._source_edit.setText(chosen)
        self._on_source_changed(chosen)

    def _on_source_changed(self, path_text: str) -> None:
        """扫描源文件夹 → 预览组件清单（同步：纯存在性/解析检查，快）。"""
        self._preview.clear()
        self._scan = None
        self._import_btn.setEnabled(False)
        self._weights_combo.clear()
        self._weights_combo.addItem("不接线（仅导入卡）", None)
        self._weights_combo.setEnabled(False)
        try:
            scan = scan_card(Path(path_text))
        except Exception as exc:
            self._preview.addItem(f"✗ 扫描异常：{exc.__class__.__name__}: {exc}")
            return
        self._scan = scan
        for name, ok, detail in scan.checks:
            self._preview.addItem(f"{'✓' if ok else '✗'} {name}"
                                  + (f"（{detail}）" if detail else ""))
        if not scan.source.is_dir():
            self._package_edit.setText("")
            return
        suggested = sanitize_package_id(scan.package_id_hint or "")
        self._package_edit.setText(suggested)
        # 下拉只列 SoVITS 权重（接线主对象）；GPT ckpt 有则随 t2s 自动附带
        for rel in scan.sovits_weights:
            self._weights_combo.addItem(f"SoVITS：{rel.name}", rel)
        if scan.sovits_weights:
            self._weights_combo.setEnabled(True)
            self._weights_combo.setCurrentIndex(1)
        self._import_btn.setEnabled(scan.ok)

    def _selected_weights(self) -> tuple[Path | None, Path | None]:
        """combo 选 SoVITS 权重；GPT ckpt 有则随接线一并带上（t2s 同步指向）。"""
        if not self._scan:
            return None, None
        data = self._weights_combo.currentData()
        if data is None:
            return None, None
        vits = next((p for p in self._scan.sovits_weights
                     if str(p) == str(data)), None)
        t2s = self._scan.gpt_weights[0] if self._scan.gpt_weights else None
        return vits, t2s

    def _start_import(self) -> None:
        if self._worker is not None:
            return
        source = Path(self._source_edit.text())
        package_id = sanitize_package_id(self._package_edit.text())
        if not is_valid_package_id(package_id):
            self._result.setText(f"✗ 非法 package_id：{self._package_edit.text()!r}")
            return
        kwargs: dict = {
            "source": source,
            "package_id": package_id,
            "characters_root": self._characters_root,   # None=生产默认；测试注入隔离
            "overwrite": self._overwrite_box.isChecked(),
            "reference_donor": (self._donor
                                if self._backfill_box.isChecked() else None),
        }
        vits, t2s = self._selected_weights()
        service_yaml = self._service_yaml_edit.text().strip()
        if vits is not None and service_yaml:
            root = Path(service_yaml).resolve().parents[1]
            kwargs["wiring"] = TtsWiring(gptsovits_root=root,
                                         source_root=source,
                                         vits_src=vits, t2s_src=t2s)
        self._import_btn.setEnabled(False)
        self._result.setText("配置中（拷贝卡片，勿关闭窗口）……")
        self._worker = _ImportWorker(kwargs)
        self._worker.done.connect(self._on_import_done)
        self._worker.failed.connect(self._on_import_crashed)
        self._worker.start()

    def _on_import_done(self, result) -> None:
        """导入结果上报（2026-09-18 强化：失败必须给出可行动的原因）。"""
        self._worker = None
        self._import_btn.setEnabled(bool(self._scan and self._scan.ok))
        if not result.ok:
            steps_txt = "".join(f"\n· {s}" for s in result.steps)
            detail = f"失败原因：{result.error}\n已执行步骤：{steps_txt or '无'}"
            self._result.setText(f"✗ 配置失败\n{detail}")
            QMessageBox.critical(self, "一键配置失败", detail
                                 + "\n\n当前角色未受影响，修正后可重试。")
            return
        log = "\n".join(f"· {s}" for s in result.steps)
        if result.wiring_error:
            warn = (f"⚠️ TTS 权重接线失败：{result.wiring_error}\n"
                    f"卡片已导入并保留（语音暂走纯气泡降级）；可检查服务 yaml 路径"
                    f"与磁盘/权限后重新配置。")
            self._result.setText(f"✓ 卡片导入完成（TTS 接线失败）\n{log}\n{warn}")
            QMessageBox.warning(self, "TTS 权重接线失败",
                                f"TTS 权重接线失败，卡片已导入保留：\n\n"
                                f"{result.wiring_error}\n\n"
                                "语音暂以纯气泡降级；修正后重新一键配置即可补接线。")
            return
        self._result.setText(f"✓ 配置完成\n{log}")
        if self._activate_box.isChecked():
            try:
                from client.switcher import write_active_package_id

                write_active_package_id(self._package_edit.text(),
                                        self._settings_path)
            except Exception as exc:
                self._result.setText(
                    f"✗ 卡片已导入，但激活写回失败：{exc.__class__.__name__}: {exc}"
                    f"\n可手动把 settings.yaml 的 character.package_id 改为 "
                    f"「{self._package_edit.text()}」后重启。")
                QMessageBox.warning(self, "激活写回失败",
                                    f"卡片已导入保留，但激活写回失败：\n{exc}"
                                    f"\n\n可手动编辑 settings.yaml 的 "
                                    f"character.package_id 后重启。")
                return
            self._result.setText(self._result.text()
                                 + f"\n· 已设为激活角色：{self._package_edit.text()}")
            if self._restart_callback is not None:
                self._restart_btn.setVisible(True)
                return
        self._result.setText(self._result.text()
                             + "\n重启 miyako 后生效（托盘「完全退出」→ 启动miyako.bat）")

    def _on_import_crashed(self, tb_text: str) -> None:
        """工作线程未预期异常（import_card 之外的开发期缺陷）：完整上报不吞。"""
        self._worker = None
        self._import_btn.setEnabled(bool(self._scan and self._scan.ok))
        head = tb_text.strip().splitlines()[-1] if tb_text.strip() else "未知异常"
        self._result.setText(f"✗ 导入过程异常（已中止，未改动现有角色）\n{head}"
                             f"\n完整堆栈已写入 logs/client.log")
        QMessageBox.critical(self, "一键配置异常",
                             f"导入过程发生未预期异常，已中止（现有角色不受影响）：\n\n"
                             f"{head}\n\n完整堆栈见 logs/client.log，请反馈排查。")

    def _restart_now(self) -> None:
        """保存 + 重启：完全退出（含 TTS，服务重启后吃新 yaml）→ 重拉启动 bat。"""
        if self._restart_callback is None:
            return
        self._restart_btn.setEnabled(False)
        self._result.setText("正在重启 miyako（完全退出 → 重拉启动 bat）……")
        try:
            self._restart_callback()
        except Exception as exc:
            logger.error("重启编排失败", exc_info=True)
            QMessageBox.warning(self, "重启失败",
                                f"重启指令执行失败：{exc}\n\n"
                                "请手动托盘「完全退出」后双击 启动miyako.bat。")
            self._restart_btn.setEnabled(True)
            return
        self.accept()
