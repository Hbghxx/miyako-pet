"""酒馆卡导入对话框（2026-09-18 用户需求：SillyTavern 卡适配）。

与「一键配置角色包」（card_import_dialog）**平行的独立模式**——原卡导入
链路零改动。流程：选 .json/.png 酒馆卡 → tavern_card 转换为临时 .char 包
→ scan_card 预览 → import_card（拷贝+校验+回滚保护）→ 可选激活写回 →
重启生效。

音色（用户定案：能正常用 miyako 的语音和 TTS 链路跟酒馆角色聊天即可，
不加额外的东西）：**无条件**从当前激活包拷参考音频池（import_card 的
reference_donor 既有机制，无开关）——GptsovitsTts 候选池兜底自动接管，
GPT-SoVITS 服务里加载的正是 miyako 权重、参考音频同源 → 酒馆角色说
日文台词时就是 miyako 的音色；服务未启动/不可用时按既有降级矩阵走。

不做的事：无 TTS 权重接线（酒馆卡无权重）、无服务 yaml、无补拷开关。
立绘（2026-09-18 用户需求）：默认自动取图（PNG 卡面 → json 同名图 →
内置占位图），「立绘图片」行可自选任意图片覆盖、「用卡面」恢复自动。
大目录拷贝放工作线程（与 card_import_dialog 同工艺）。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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

from client.card_importer import (TtsWiring, import_card, is_valid_package_id,
                                  sanitize_package_id, scan_card)
from server.constants import SETTINGS_PATH, load_settings, resolve_character_package

logger = logging.getLogger(__name__)


class _ImportWorker(QThread):
    """import_card 工作线程（全捕获：任何异常转 failed 信号上报不吞）。"""

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

            logger.error("酒馆卡导入线程异常", exc_info=True)
            self.failed.emit(traceback.format_exc())
            return
        self.done.emit(result)


class TavernImportDialog(QDialog):
    """托盘「导入酒馆卡…」：选卡 → 转换预览 → 一键导入/激活。"""

    def __init__(self, *, restart_callback=None, settings_path=None,
                 characters_root=None, default_tts_root=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导入酒馆卡（SillyTavern）")
        self.setMinimumWidth(560)
        self._settings_path = Path(settings_path) if settings_path else SETTINGS_PATH
        self._characters_root = (Path(characters_root)
                                 if characters_root else None)
        self._default_tts_root = (Path(default_tts_root)
                                  if default_tts_root else None)   # None=data/tts
        self._restart_callback = restart_callback
        self._worker: _ImportWorker | None = None
        self._staging_root: Path | None = None     # 转换临时目录（关窗清理）
        self._staged_source: Path | None = None
        self._donor = self._current_donor()        # 音色供体=打开时的激活包
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        form = QFormLayout()

        row = QHBoxLayout()
        self._source_edit = QLineEdit()
        self._source_edit.setPlaceholderText("选择酒馆角色卡（.json 或 .png）")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(self._source_edit)
        row.addWidget(browse)
        form.addRow("酒馆卡文件", row)

        self._preview = QListWidget()
        self._preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        form.addRow("转换预览", self._preview)

        self._package_edit = QLineEdit()
        form.addRow("package_id", self._package_edit)

        sprite_row = QHBoxLayout()
        self._sprite_edit = QLineEdit()
        self._sprite_edit.setPlaceholderText(
            "自动：卡面图（PNG 卡）→ 同名图片 → 内置占位图；可选任意图片作立绘")
        sprite_pick = QPushButton("选择…")
        sprite_pick.clicked.connect(self._pick_sprite)
        sprite_auto = QPushButton("用卡面")
        sprite_auto.setToolTip("清除自选，恢复自动取图（卡面/同名图/占位图）")
        sprite_auto.clicked.connect(self._use_card_sprite)
        sprite_row.addWidget(self._sprite_edit)
        sprite_row.addWidget(sprite_pick)
        sprite_row.addWidget(sprite_auto)
        form.addRow("立绘图片", sprite_row)

        voice_note = QLabel("语音：借用当前角色（激活包）的音色与既有 TTS 链路——"
                            "酒馆角色直接用 miyako 的声音说日文台词")
        voice_note.setWordWrap(True)
        form.addRow("", voice_note)

        self._activate_box = QCheckBox("导入后设为激活角色（写入 settings.package_id）")
        self._activate_box.setChecked(True)
        form.addRow("", self._activate_box)

        self._overwrite_box = QCheckBox("目标已存在时覆盖（重导入/转正；当前角色先有备份再动）")
        form.addRow("", self._overwrite_box)

        self._import_btn = QPushButton("导入")
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
        """导入进行中禁止关闭——工作线程向已销毁窗口发信号会崩（防误关）。"""
        if self._worker is not None and self._worker.isRunning():
            self._result.setText("⚠️ 正在导入中，请稍候（拷贝完成前不可关闭）")
            return
        self._cleanup_staging()
        super().reject()

    # ---------------- 交互 ----------------
    def _current_donor(self) -> Path | None:
        """音色供体 = 打开对话框时的现役激活包（激活写回前捕获）。"""
        try:
            donor = resolve_character_package(load_settings())
            return donor if donor.is_dir() else None
        except Exception:
            return None

    def _cleanup_staging(self) -> None:
        if self._staging_root is not None:
            shutil.rmtree(self._staging_root, ignore_errors=True)
            self._staging_root = None
            self._staged_source = None

    def _browse(self) -> None:
        start = self._source_edit.text() or str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择酒馆角色卡（.json 或 .png）", start,
            "酒馆角色卡 (*.json *.png);;所有文件 (*)")
        if not chosen:
            return
        self._stage_file(chosen)

    # ---------------- 立绘自选（2026-09-18 用户需求） ----------------
    def _pick_sprite(self) -> None:
        start = self._sprite_edit.text().strip() \
            or (self._source_edit.text() or str(Path.home()))
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择立绘图片（整图作为宠物立绘，九情绪共用）", start,
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp);;所有文件 (*)")
        if not chosen:
            return
        self._sprite_edit.setText(chosen)
        self._restage_if_loaded()

    def _use_card_sprite(self) -> None:
        self._sprite_edit.clear()
        self._restage_if_loaded()

    def _restage_if_loaded(self) -> None:
        """立绘选择变化 → 已载入的卡重转换（纯解析+小拷贝，快）。"""
        src = self._source_edit.text().strip()
        if src:
            self._stage_file(src)

    def _stage_file(self, path_text: str) -> bool:
        """酒馆卡文件 → 临时 .char 包并预览（同步：纯解析+小文件拷贝，快）。

        返回是否成功。解析类失败弹窗给可行动原因；其余异常完整上报不吞。
        """
        from client.tavern_card import TavernCardError, stage_tavern_package

        self._cleanup_staging()
        staging = Path(tempfile.mkdtemp(prefix="miyako-tavern-"))
        art = self._sprite_edit.text().strip() or None   # 自选立绘（None=自动取图）
        try:
            staged, package_id, display_name = stage_tavern_package(
                Path(path_text), staging, art_path=art)
        except TavernCardError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            QMessageBox.warning(self, "酒馆卡解析失败",
                                f"无法解析所选文件：\n\n{exc}")
            return False
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            logger.error("酒馆卡转换异常", exc_info=True)
            import traceback

            QMessageBox.critical(self, "酒馆卡转换异常",
                                 "转换过程发生未预期异常（已中止）：\n\n"
                                 + traceback.format_exc(limit=3))
            return False
        self._staging_root = staging
        self._staged_source = staged
        self._source_edit.setText(path_text)
        self._refresh_preview()
        self._package_edit.setText(package_id)
        art = next(iter(staged.joinpath("sprites").glob("card.*")), None)
        logger.info("酒馆卡预览：%s（%s → %s，立绘=%s）",
                    display_name, path_text, package_id,
                    art.name if art else "内置占位图")
        return True

    def _refresh_preview(self) -> None:
        """扫描转换产物 → 预览组件清单（与原卡导入同一 scan，只读、快）。"""
        self._preview.clear()
        self._import_btn.setEnabled(False)
        if self._staged_source is None:
            return
        try:
            scan = scan_card(self._staged_source)
        except Exception as exc:
            self._preview.addItem(f"✗ 扫描异常：{exc.__class__.__name__}: {exc}")
            return
        self._preview.addItem("✓ 酒馆卡已转换为角色包（下列为转换结果检查）")
        for name, ok, detail in scan.checks:
            self._preview.addItem(f"{'✓' if ok else '✗'} {name}"
                                  + (f"（{detail}）" if detail else ""))
        self._preview.addItem(("✓ 音色：导入时自动借用当前角色（激活包）的参考音频池"
                               if self._donor else
                               "⚠️ 当前无激活包可借音色——语音将走纯气泡降级"))
        art = next(iter((self._staged_source / "sprites").glob("card.*")), None)
        self._preview.addItem(f"✓ 立绘：{art.name}（九情绪共用）" if art
                              else "✓ 立绘：内置占位图")
        from client.tavern_card import find_default_tts_weights

        vits, _ = find_default_tts_weights(self._default_tts_root)
        service_yaml = self._read_service_yaml_setting()
        if vits is not None and service_yaml:
            self._preview.addItem("✓ TTS：借当前角色参考池 + 自动接线默认音色权重"
                                  "（data/tts）")
        elif vits is None:
            self._preview.addItem("⚠️ data/tts 无默认权重——仅借参考池"
                                  "（服务沿用当前加载的权重）")
        else:
            self._preview.addItem("⚠️ settings 未配 providers.tts_service_yaml"
                                  "——跳过默认权重接线（仅借参考池）")
        self._import_btn.setEnabled(scan.ok)

    def _read_service_yaml_setting(self) -> str:
        """settings providers.tts_service_yaml 现读（默认权重接线的目标）。"""
        try:
            return str(((load_settings(self._settings_path).get("providers") or {})
                        .get("tts_service_yaml")) or "")
        except Exception:
            return ""

    def _default_wiring(self) -> TtsWiring | None:
        """默认 TTS 接线（2026-09-18 用户需求）：导入的卡无配套权重（酒馆卡
        恒如此）时，把 GPT-SoVITS 服务接线到 data/tts 默认权重=默认音色。

        复用 card_importer 的 TtsWiring/wire_tts_service（零新机制，与原卡
        接线同一代码路径）；条件任缺则跳过（返回 None）——无默认权重 /
        settings 未配服务 yaml。跳过不阻塞导入：参考音频池已借入，服务里
        加载的权重是什么就出什么音色。
        """
        if self._staged_source is None:
            return None
        if (self._staged_source / "tts" / "SoVITS_weights_v2").is_dir():
            return None                     # 卡自带配套权重：尊重卡片，不接线
        from client.tavern_card import find_default_tts_weights

        vits, t2s = find_default_tts_weights(self._default_tts_root)
        if vits is None:
            return None
        service_yaml = self._read_service_yaml_setting()
        if not service_yaml:
            return None
        tts_root = Path(self._default_tts_root) if self._default_tts_root \
            else None
        from client.tavern_card import DEFAULT_TTS_DIR

        source_root = tts_root or DEFAULT_TTS_DIR
        return TtsWiring(gptsovits_root=Path(service_yaml).resolve().parents[1],
                         source_root=source_root,
                         vits_src=vits.relative_to(source_root),
                         t2s_src=(t2s.relative_to(source_root)
                                  if t2s is not None else None))

    def _start_import(self) -> None:
        if self._worker is not None or self._staged_source is None:
            return
        package_id = sanitize_package_id(self._package_edit.text())
        if not is_valid_package_id(package_id):
            self._result.setText(f"✗ 非法 package_id：{self._package_edit.text()!r}")
            return
        kwargs: dict = {
            "source": self._staged_source,
            "package_id": package_id,
            "characters_root": self._characters_root,   # None=生产默认；测试注入隔离
            "overwrite": self._overwrite_box.isChecked(),
            "reference_donor": self._donor,             # 借用音色：恒传供体（用户定案）
        }
        wiring = self._default_wiring()
        if wiring is not None:
            kwargs["wiring"] = wiring       # 无配套 TTS → 接线 data/tts 默认权重
        self._import_btn.setEnabled(False)
        self._result.setText("导入中（拷贝卡片，勿关闭窗口）……")
        self._worker = _ImportWorker(kwargs)
        self._worker.done.connect(self._on_import_done)
        self._worker.failed.connect(self._on_import_crashed)
        self._worker.start()

    def _on_import_done(self, result) -> None:
        self._worker = None
        self._import_btn.setEnabled(self._staged_source is not None)
        if not result.ok:
            steps_txt = "".join(f"\n· {s}" for s in result.steps)
            detail = f"失败原因：{result.error}\n已执行步骤：{steps_txt or '无'}"
            self._result.setText(f"✗ 导入失败\n{detail}")
            QMessageBox.critical(self, "酒馆卡导入失败", detail
                                 + "\n\n现有角色未受影响，修正后可重试。")
            return
        log = "\n".join(f"· {s}" for s in result.steps)
        if result.wiring_error:
            warn = (f"⚠️ 默认 TTS 权重接线失败：{result.wiring_error}\n"
                    f"卡片已导入并保留（参考音频池已借入；服务沿用当前加载的权重）"
                    f"；可检查 settings 的 providers.tts_service_yaml 与磁盘/权限"
                    f"后重新导入补接线。")
            self._result.setText(f"✓ 导入完成（默认 TTS 接线失败）\n{log}\n{warn}")
            QMessageBox.warning(self, "默认 TTS 权重接线失败",
                                f"默认 TTS 权重接线失败，卡片已导入保留：\n\n"
                                f"{result.wiring_error}\n\n"
                                "语音按服务当前权重出声；修正后重新导入即可补接线。")
            return
        self._result.setText(f"✓ 导入完成\n{log}")
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
        self._worker = None
        self._import_btn.setEnabled(self._staged_source is not None)
        head = tb_text.strip().splitlines()[-1] if tb_text.strip() else "未知异常"
        self._result.setText(f"✗ 导入过程异常（已中止，未改动现有角色）\n{head}"
                             f"\n完整堆栈已写入 logs/client.log")
        QMessageBox.critical(self, "酒馆卡导入异常",
                             f"导入过程发生未预期异常，已中止（现有角色不受影响）：\n\n"
                             f"{head}\n\n完整堆栈见 logs/client.log，请反馈排查。")

    def _restart_now(self) -> None:
        """保存 + 重启：完全退出（含 TTS）→ 重拉启动 bat（复用切换角色编排）。"""
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
