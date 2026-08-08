import html
import os
import queue
import re
import sys
import threading
import time

LOG_FILE = "迁移日志.txt"

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    HAVE_QT = True
except ImportError:
    QtCore = QtGui = QtWidgets = None
    HAVE_QT = False

CONFIRM_TIMEOUT_MS = 60000


class VersionFetcher(QtCore.QObject):
    done = QtCore.Signal(list, list)

    def run(self):
        from .versions import fetch_mc_versions
        try:
            releases, all_ids = fetch_mc_versions()
        except Exception:
            releases, all_ids = [], []
        self.done.emit(releases, all_ids)


class MigrateWorker:

    def __init__(self, params, cfg):
        from .core import Logger
        self.params = params
        self.cfg = cfg
        self.msg_queue = queue.Queue() 
        self._ans_event = threading.Event()
        self._answer = True
        self.cfg.log = Logger(self.log_sink)
        self.cfg.confirm = self.confirm_sync

    def log_sink(self, msg, level="info"):
        self.msg_queue.put(("log", level, msg))
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass

    def confirm_sync(self, prompt):
        self.msg_queue.put(("confirm", prompt))
        self._ans_event.clear()
        self._ans_event.wait(timeout=None)
        return self._answer

    def set_answer(self, val):
        self._answer = val
        self._ans_event.set()

    def run(self):
        from .migrator import run_migration
        try:
            report, same_client = run_migration(self.params, self.cfg)
            self.msg_queue.put(("done", {"report": report, "same": same_client}))
        except Exception as e:
            self.msg_queue.put(("fail", str(e)))


class MainWindow(QtWidgets.QWidget):
    _log_signal = QtCore.Signal(str, str)

    def __init__(self):
        super().__init__()
        self.worker = None
        self._worker_thread = None
        self._vf_thread = None
        self._vf = None
        self._src_versions = []
        self._target_versions = []
        self._src_vdir = None
        self._dst_vdir = None
        self._mode = None
        self._overwrite_mode = False
        self._mc_releases = []
        self._mc_all = []
        self._loader_touched = False
        self._log_signal.connect(self._render_log)
        self._build_ui()
        self._apply_defaults()
        self._start_version_fetch()

    def _build_ui(self):
        from .core import CHOICE_KEYS, CHOICE_LABELS, LOADERS, LOADER_LABEL

        self.setWindowTitle("MC 模组迁移 / 更新工具")
        self.resize(820, 640)
        root = QtWidgets.QVBoxLayout(self)

        mode_box = QtWidgets.QGroupBox("迁移模式")
        mode = QtWidgets.QHBoxLayout(mode_box)
        self.c2c_radio = QtWidgets.QRadioButton("客户端")
        self.c2c_radio.setChecked(True)
        self.s2s_radio = QtWidgets.QRadioButton("服务端")
        self.c2c_radio.toggled.connect(self._on_mode_changed)
        self.s2s_radio.toggled.connect(self._on_mode_changed)
        mode.addWidget(self.c2c_radio)
        mode.addWidget(self.s2s_radio)
        mode.addStretch(1)
        root.addWidget(mode_box)

        src_box = QtWidgets.QGroupBox("① 源")
        src_grid = QtWidgets.QGridLayout(src_box)
        self.src_root_edit = QtWidgets.QLineEdit()
        self.src_root_edit.setPlaceholderText(".minecraft 根目录 / versions 下版本隔离目录 / 服务端根目录")
        self.src_browse_btn = QtWidgets.QPushButton("浏览...")
        self.src_browse_btn.clicked.connect(self._pick_src_root)
        self.src_version_combo = QtWidgets.QComboBox()
        self.src_version_combo.setMinimumWidth(320)
        self.src_ver_label = QtWidgets.QLabel("加载器: -")
        self.src_version_label = QtWidgets.QLabel("客户端版本")
        self.src_version_row = QtWidgets.QWidget()
        sr = QtWidgets.QHBoxLayout(self.src_version_row)
        sr.setContentsMargins(0, 0, 0, 0)
        sr.addWidget(self.src_version_combo)
        sr.addWidget(self.src_ver_label)
        sr.addStretch(1)
        src_grid.addWidget(QtWidgets.QLabel("目录"), 0, 0)
        src_grid.addWidget(self.src_root_edit, 0, 1)
        src_grid.addWidget(self.src_browse_btn, 0, 2)
        src_grid.addWidget(self.src_version_label, 1, 0)
        src_grid.addWidget(self.src_version_row, 1, 1)
        src_grid.setColumnStretch(1, 1)
        root.addWidget(src_box)

        dst_box = QtWidgets.QGroupBox("② 目标")
        dst_grid = QtWidgets.QGridLayout(dst_box)
        self.dst_root_edit = QtWidgets.QLineEdit()
        self.dst_browse_btn = QtWidgets.QPushButton("浏览...")
        self.dst_browse_btn.clicked.connect(self._pick_dst_root)
        self.dst_version_combo = QtWidgets.QComboBox()
        self.dst_version_combo.setMinimumWidth(320)
        self.dst_version_label = QtWidgets.QLabel("客户端版本")
        self.dst_client_widget = QtWidgets.QWidget()
        cw = QtWidgets.QHBoxLayout(self.dst_client_widget)
        cw.setContentsMargins(0, 0, 0, 0)
        cw.addWidget(self.dst_version_combo)
        cw.addWidget(QtWidgets.QLabel("(留空=mods 放根目录)"))
        cw.addStretch(1)
        self.dst_server_widget = QtWidgets.QWidget()
        sw = QtWidgets.QHBoxLayout(self.dst_server_widget)
        sw.setContentsMargins(0, 0, 0, 0)
        self.overwrite_radio = QtWidgets.QRadioButton("迁移到新的空服务端")
        self.overwrite_radio.setChecked(True)
        self.overwrite_inplace_radio = QtWidgets.QRadioButton("直接覆盖当前服务端 mods（只更新 mods，其他不迁移）")
        self.overwrite_inplace_radio.toggled.connect(self._on_dst_overwrite_changed)
        sw.addWidget(self.overwrite_radio)
        sw.addWidget(self.overwrite_inplace_radio)
        sw.addStretch(1)
        self.dst_client_widget.hide()

        self.loader_combo = QtWidgets.QComboBox()
        for l in LOADERS:
            self.loader_combo.addItem(LOADER_LABEL[l], l)
        self.mc_combo = QtWidgets.QComboBox()
        self.mc_combo.setEditable(True)
        self.mc_combo.setMinimumWidth(140)
        self.show_all_chk = QtWidgets.QCheckBox("显示所有版本（含快照）")
        self.mc_status_label = QtWidgets.QLabel("正在拉取版本列表...")

        dst_grid.addWidget(QtWidgets.QLabel("目录"), 0, 0)
        dst_grid.addWidget(self.dst_root_edit, 0, 1)
        dst_grid.addWidget(self.dst_browse_btn, 0, 2)
        dst_grid.addWidget(self.dst_version_label, 1, 0)
        dst_grid.addWidget(self.dst_client_widget, 1, 1)
        dst_grid.addWidget(self.dst_server_widget, 1, 1)
        dst_grid.addWidget(QtWidgets.QLabel("目标加载器"), 2, 0)
        dst_grid.addWidget(self.loader_combo, 2, 1)
        dst_grid.addWidget(QtWidgets.QLabel("目标 MC 版本"), 3, 0)
        dst_grid.addWidget(self.mc_combo, 3, 1)
        dst_grid.addWidget(self.show_all_chk, 3, 2)
        dst_grid.addWidget(self.mc_status_label, 4, 1)
        dst_grid.setColumnStretch(1, 1)
        root.addWidget(dst_box)

        opt_box = QtWidgets.QGroupBox("③ 选项")
        opt = QtWidgets.QGridLayout(opt_box)
        self.auto_yes_chk = QtWidgets.QCheckBox("自动确认模组匹配")
        self.deps_chk = QtWidgets.QCheckBox("自动下载依赖")
        self.deps_chk.setChecked(True)
        self.data_master_chk = QtWidgets.QCheckBox("迁移数据")
        self.data_master_chk.setChecked(True)
        self.data_master_chk.toggled.connect(self._toggle_data_group)
        opt.addWidget(self.data_master_chk, 0, 0)
        opt.addWidget(self.auto_yes_chk, 0, 1)
        opt.addWidget(self.deps_chk, 0, 2)
        self.data_checks = {}
        for i, k in enumerate(CHOICE_KEYS):
            ck = QtWidgets.QCheckBox(CHOICE_LABELS[k])
            ck.setChecked(k != "optional")
            self.data_checks[k] = ck
            opt.addWidget(ck, 1 + i // 3, i % 3)
        self.proxy_chk = QtWidgets.QCheckBox("使用系统代理")
        self.proxy_chk.setChecked(True)
        self.threads_spin = QtWidgets.QSpinBox()
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setValue(4)
        self.threads_spin.setToolTip("并发下载线程数")
        self.failures_chk = QtWidgets.QCheckBox("完成后列出匹配失败及开源链接")
        self.failures_chk.setChecked(True)
        opt.addWidget(self.proxy_chk, 0, 3)
        opt.addWidget(QtWidgets.QLabel("下载线程"), 1, 3)
        opt.addWidget(self.threads_spin, 2, 3)
        opt.addWidget(self.failures_chk, 3, 0, 1, 3)
        opt.setColumnStretch(3, 1)
        root.addWidget(opt_box)
        bar = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始迁移")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setFixedHeight(18)
        self.progress.setRange(0, 0)
        self.progress.hide()
        bar.addWidget(self.start_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.progress, 1)
        root.addLayout(bar)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QtGui.QFont("Consolas", 9))
        root.addWidget(self.log_view, 1)

        self.src_root_edit.textChanged.connect(lambda _: self._refresh_src_versions())
        self.dst_root_edit.textChanged.connect(lambda _: self._refresh_dst_versions())
        self.src_version_combo.currentIndexChanged.connect(self._on_src_version_changed)
        self.dst_version_combo.currentIndexChanged.connect(self._on_dst_version_changed)
        self.loader_combo.activated.connect(lambda _: setattr(self, "_loader_touched", True))
        self.show_all_chk.toggled.connect(lambda _: self._fill_mc_combo())

    def _apply_defaults(self):
        from .core import default_mc_root
        default = default_mc_root()
        self.src_root_edit.setText(default)
        self.dst_root_edit.setText(default)
        self._on_mode_changed()
        
    def _pick_src_root(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择源目录",
                                                       self.src_root_edit.text() or os.path.expanduser("~"))
        if d:
            self.src_root_edit.setText(os.path.normpath(d))

    def _pick_dst_root(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择目标目录",
                                                       self.dst_root_edit.text() or os.path.expanduser("~"))
        if d:
            self.dst_root_edit.setText(os.path.normpath(d))

    def _on_mode_changed(self):
        new_mode = "s2s" if self.s2s_radio.isChecked() else "c2c"
        if new_mode == self._mode:
            return
        self._mode = new_mode
        s2s = new_mode == "s2s"
        self.src_version_row.setVisible(not s2s)
        self.src_version_label.setVisible(not s2s)
        self.dst_client_widget.setVisible(not s2s)
        self.dst_version_label.setVisible(not s2s)
        self.dst_server_widget.setVisible(s2s)
        self.data_checks["server"].setVisible(s2s)
        self.data_checks["options"].setVisible(not s2s)
        self._refresh_src_versions()
        self._refresh_dst_versions()
        if s2s:
            self._on_dst_overwrite_changed()
        else:
            self._overwrite_mode = False
            self.dst_root_edit.setEnabled(True)
            self.dst_browse_btn.setEnabled(True)
            self.data_master_chk.setEnabled(True)
            self._toggle_data_group(self.data_master_chk.isChecked())
        self._auto_set_mc_version()

    def _on_dst_overwrite_changed(self):
        ov = self.overwrite_inplace_radio.isChecked()
        self._overwrite_mode = ov
        self.dst_root_edit.setEnabled(not ov)
        self.dst_browse_btn.setEnabled(not ov)
        for ck in [self.data_master_chk] + list(self.data_checks.values()):
            ck.setEnabled(not ov)
        if ov:
            self.dst_root_edit.setText(self.src_root_edit.text().strip())

    def _auto_loader_default(self, ldr):
        if ldr and not self._loader_touched:
            i = self.loader_combo.findData(ldr)
            if i >= 0:
                self.loader_combo.setCurrentIndex(i)

    def _refresh_src_versions(self):
        from .clients import (is_server_root, list_clients, resolve_version_dir,
                              sniff_server_loader)
        from .core import LOADER_LABEL
        root = self.src_root_edit.text().strip()
        if self._mode != "s2s" and os.path.isdir(root):
            vd = resolve_version_dir(root)
            if vd:
                self._src_vdir = vd
                mc_root, version = vd
                if self.src_root_edit.text().strip() != mc_root:
                    self.src_root_edit.setText(mc_root)
                self._select_version(self.src_version_combo, self._src_versions, version)
                return
        if os.path.isdir(root) and is_server_root(root) and not self.s2s_radio.isChecked():
            self.s2s_radio.setChecked(True)
            return
        self.src_version_combo.blockSignals(True)
        self.src_version_combo.clear()
        if self._mode == "s2s":
            self._src_versions = []
            self.src_version_combo.setEnabled(False)
            self.src_version_combo.addItem("(服务端根目录，mods 直接在根目录)")
            ldr = sniff_server_loader(root)
            self.src_ver_label.setText("加载器: %s" % (LOADER_LABEL[ldr] if ldr else "未识别（请手动选目标加载器）"))
            self._auto_loader_default(ldr)
        else:
            self.src_version_combo.setEnabled(True)
            self._src_versions = list_clients(root) if os.path.isdir(root) else []
            for name, loader in self._src_versions:
                tag = "  [%s]" % LOADER_LABEL[loader] if loader else ""
                self.src_version_combo.addItem(name + tag)
            self._on_src_version_changed()
        self.src_version_combo.blockSignals(False)

    def _refresh_dst_versions(self):
        from .clients import (is_server_root, list_clients, resolve_version_dir,
                              sniff_server_loader)
        from .core import LOADER_LABEL
        root = self.dst_root_edit.text().strip()
        if self._mode != "s2s" and os.path.isdir(root):
            vd = resolve_version_dir(root)
            if vd:
                self._dst_vdir = vd
                mc_root, version = vd
                if self.dst_root_edit.text().strip() != mc_root:
                    self.dst_root_edit.setText(mc_root)
                self._select_version(self.dst_version_combo, self._target_versions, version, offset=1)
                self._auto_set_mc_version()
                return
        if os.path.isdir(root) and is_server_root(root) and not self.s2s_radio.isChecked():
            self.s2s_radio.setChecked(True)
            return
        self.dst_version_combo.blockSignals(True)
        self.dst_version_combo.clear()
        if self._mode == "s2s":
            self._target_versions = []
            self.dst_version_combo.setEnabled(False)
            self.dst_version_combo.addItem("(服务端根目录，无版本)")
            self._auto_loader_default(sniff_server_loader(root))
        else:
            self.dst_version_combo.setEnabled(True)
            self._target_versions = list_clients(root) if os.path.isdir(root) else []
            self.dst_version_combo.addItem("(不使用版本目录，mods 放根目录)")
            for name, loader in self._target_versions:
                tag = "  [%s]" % LOADER_LABEL[loader] if loader else ""
                self.dst_version_combo.addItem(name + tag)
        self.dst_version_combo.blockSignals(False)
        self._auto_set_mc_version()

    @staticmethod
    def _select_version(combo, versions, version, offset=0):
        for i, (name, _loader) in enumerate(versions):
            if name == version:
                combo.setCurrentIndex(i + offset)
                return True
        return False

    def _on_src_version_changed(self):
        from .core import LOADER_LABEL
        idx = self.src_version_combo.currentIndex()
        if 0 <= idx < len(self._src_versions):
            loader = self._src_versions[idx][1]
            self.src_ver_label.setText("加载器: %s" % (LOADER_LABEL[loader] if loader else "未识别"))
            self._auto_loader_default(loader)
        else:
            self.src_ver_label.setText("加载器: -")

    def _on_dst_version_changed(self):
        self._auto_set_mc_version()

    def _start_version_fetch(self):
        self._vf_thread = QtCore.QThread(self)
        self._vf = VersionFetcher()
        self._vf.moveToThread(self._vf_thread)
        self._vf_thread.started.connect(self._vf.run)
        self._vf.done.connect(self._on_versions_fetched)
        self._vf_thread.start()

    def _on_versions_fetched(self, releases, all_ids):
        self._mc_releases, self._mc_all = releases, all_ids
        if self._vf_thread:
            self._vf_thread.quit()
            self._vf_thread.wait(2000)
            self._vf_thread = None
        self._fill_mc_combo()
        self.mc_status_label.setText(
            "已拉取 %d 个正式版（勾选『显示所有版本』可含快照）" % len(releases)
            if releases else "版本列表拉取失败，可手动输入版本号")
        self._auto_set_mc_version()

    def _fill_mc_combo(self):
        ids = self._mc_all if self.show_all_chk.isChecked() else self._mc_releases
        cur = self.mc_combo.currentText()
        self.mc_combo.blockSignals(True)
        self.mc_combo.clear()
        self.mc_combo.addItems(ids or [])
        if cur:
            i = self.mc_combo.findText(cur)
            if i >= 0:
                self.mc_combo.setCurrentIndex(i)
            else:
                self.mc_combo.setEditText(cur)
        self.mc_combo.blockSignals(False)

    def _auto_set_mc_version(self):
        if self._mode != "c2c":
            return
        t_idx = self.dst_version_combo.currentIndex()
        if not (0 <= t_idx < len(self._target_versions)):
            return
        from .versions import base_mc_version
        name = self._target_versions[t_idx][0]
        base = base_mc_version(name, self._mc_releases or self._mc_all or [])
        if not base:
            return
        self.mc_combo.blockSignals(True)
        i = self.mc_combo.findText(base)
        if i >= 0:
            self.mc_combo.setCurrentIndex(i)
        else:
            self.mc_combo.setEditText(base)
        self.mc_combo.blockSignals(False)

    def _toggle_data_group(self, on):
        for ck in self.data_checks.values():
            ck.setEnabled(on)

    def _start(self):
        mc_root = self.src_root_edit.text().strip().strip('"')
        if not os.path.isdir(mc_root):
            QtWidgets.QMessageBox.warning(self, "错误", "源目录不存在:\n" + mc_root)
            return
        if self._mode == "c2c":
            idx = self.src_version_combo.currentIndex()
            if not (0 <= idx < len(self._src_versions)):
                QtWidgets.QMessageBox.warning(self, "错误", "请先在源游戏目录下选择客户端版本")
                return
            src_version = self._src_versions[idx][0]
            t_root = self.dst_root_edit.text().strip().strip('"')
            if not os.path.isdir(t_root):
                QtWidgets.QMessageBox.warning(self, "错误", "目标目录不存在:\n" + t_root)
                return
            t_idx = self.dst_version_combo.currentIndex()
            t_version = self._target_versions[t_idx - 1][0] if t_idx > 0 else None
        else:
            src_version = None
            if self._overwrite_mode:
                t_root = mc_root
                t_version = None
                self._log("覆盖模式：直接在源服务端更新 mods（目标 = 源目录）")
            else:
                t_root = self.dst_root_edit.text().strip().strip('"')
                if not os.path.isdir(t_root):
                    QtWidgets.QMessageBox.warning(self, "错误", "目标目录不存在:\n" + t_root)
                    return
                t_version = None

        src_force = bool(self._src_vdir and src_version == self._src_vdir[1]
                         and mc_root == self._src_vdir[0].strip().strip('"'))
        t_force = bool(self._dst_vdir and t_version == self._dst_vdir[1]
                       and t_root == self._dst_vdir[0].strip().strip('"'))

        t_loader = self.loader_combo.currentData()
        t_mc = self.mc_combo.currentText().strip().split("-")[0].strip()
        if not re.match(r"^\d+\.\d+", t_mc):
            QtWidgets.QMessageBox.warning(
                self, "错误",
                "目标 MC 版本无效：%s\n请在下方列表选择（C2C 会自动读取目标版本）" % t_mc)
            return

        if self._mode == "s2s" and self._overwrite_mode:
            choices = {k: False for k in self.data_checks}
        else:
            choices = {}
            for k, ck in self.data_checks.items():
                choices[k] = self.data_master_chk.isChecked() and ck.isChecked()

        from .migrator import RunConfig

        cfg = RunConfig(
            auto_yes=self.auto_yes_chk.isChecked(),
            skip_deps=not self.deps_chk.isChecked(),
            choices=choices,
            use_system_proxy=self.proxy_chk.isChecked(),
            download_threads=self.threads_spin.value(),
            print_failures=self.failures_chk.isChecked(),
        )
        self.stop_event = threading.Event()
        params = {"src_root": mc_root, "src_version": src_version,
                  "target_root": t_root, "target_version": t_version,
                  "src_force_isolated": src_force,
                  "target_force_isolated": t_force,
                  "target_loader": t_loader, "target_mc": t_mc,
                  "stop_event": self.stop_event}

        self.worker = MigrateWorker(params, cfg)
        self._worker_thread = threading.Thread(target=self.worker.run,
                                               name="migrate-worker", daemon=True)

        self._log("===== 开始迁移（%s）=====" % ("S2S 服务端→服务端" if self._mode == "s2s" else "C2C 客户端→客户端"))
        self._set_running(True)
        self._last_log_time = time.time()
        self._dumped = False
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_messages)
        self._poll_timer.start()
        self._worker_thread.start()

    def _stop(self):
        if self.stop_event:
            self.stop_event.set()
        self._log("正在停止（处理完当前项后退出）...")

    def _set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.progress.setVisible(running)

    def _log(self, msg, level="info"):
        if threading.current_thread() is threading.main_thread():
            self._render_log(msg, level)
        else:
            self._log_signal.emit(msg, level)

    def _render_log(self, msg, level="info"):
        self._last_log_time = time.time()
        color = {"error": "#d32f2f", "warn": "#b45309"}.get(level)
        if color:
            self.log_view.appendHtml('<span style="color:%s">%s</span>' % (color, html.escape(msg)))
        else:
            self.log_view.appendPlainText(msg)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _poll_messages(self):
        if self.worker is None:
            return
        try:
            while True:
                kind, *payload = self.worker.msg_queue.get_nowait()
                if kind == "log":
                    level, msg = payload
                    self._render_log(msg, level)
                elif kind == "confirm":
                    (prompt,) = payload
                    self._show_confirm_dialog(prompt)
                elif kind == "done":
                    (result,) = payload
                    self._poll_timer.stop()
                    self._on_finished(result)
                    return
                elif kind == "fail":
                    (err,) = payload
                    self._poll_timer.stop()
                    self._on_failed(err)
                    return
        except queue.Empty:
            pass

    def _show_confirm_dialog(self, prompt):
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Question, "确认", prompt,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            self)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        box.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        box.raise_()
        box.activateWindow()
        timer = QtCore.QTimer(box)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: (self._render_log("确认超时，自动按「是」继续", "warn"),
                                       box.done(QtWidgets.QMessageBox.StandardButton.Yes)))
        timer.start(CONFIRM_TIMEOUT_MS)
        ret = box.exec()
        if self.worker:
            self.worker.set_answer(ret == QtWidgets.QMessageBox.StandardButton.Yes)

    def _confirm(self, prompt):
        if self.worker is None:
            return True
        self.worker._ans_event.clear()
        self.worker.confirm_signal.emit(prompt)
        if not self.worker._ans_event.wait(timeout=None):
            return False
        return self.worker._answer

    def _on_finished(self, result):
        from .migrator import write_report_file
        report, same = result["report"], result["same"]
        self._set_running(False)
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None
        self.worker = None
        self._log("\n===== 完成 =====")
        self._log("成功下载 %d 个模组" % len(report["ok"]))
        for name, fname in report["ok"]:
            self._log("  %s -> %s" % (name, fname))
        if report["skipped"]:
            self._log("跳过 %d 个（库/非模组）" % len(report["skipped"]))
        if report.get("duplicates"):
            self._log("重复项目合并 %d 个（同一项目只下载一次）" % len(report["duplicates"]))
        if report["manual"]:
            self._log("%d 个模组需要手动处理（详见报告文件）" % len(report["manual"]), "warn")
            for jname, meta, why in report["manual"]:
                self._log("  - %s（%s）" % (jname, why), "warn")
        try:
            f = write_report_file(report, self._src_desc(), self._dst_desc())
            self._log("\n报告已保存: %s" % f)
        except OSError as e:
            self._log("报告保存失败: %s" % e, "error")

    def _on_failed(self, err):
        self._set_running(False)
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None
        self.worker = None
        QtWidgets.QMessageBox.critical(self, "迁移失败", err)
        self._log("\n错误: " + err, "error")

    def _src_desc(self):
        base = self.src_root_edit.text().strip()
        if self._mode == "s2s":
            return base + " (服务端)"
        if self._src_versions:
            return base + "/" + self._src_versions[self.src_version_combo.currentIndex()][0]
        return base

    def _dst_desc(self):
        base = self.dst_root_edit.text().strip()
        if self._mode == "s2s":
            return base + " (服务端)"
        t_idx = self.dst_version_combo.currentIndex()
        t = self._target_versions[t_idx - 1][0] if t_idx > 0 else "(根目录)"
        return base + "/" + t

    def closeEvent(self, event):
        if self._worker_thread and self._worker_thread.is_alive():
            ret = QtWidgets.QMessageBox.question(
                self, "退出", "迁移仍在进行中，确定退出？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if ret != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.stop_event:
                self.stop_event.set()
            self._worker_thread.join(timeout=5)
        if self._vf_thread and self._vf_thread.isRunning():
            self._vf_thread.quit()
            self._vf_thread.wait(2000)
        event.accept()


def run_gui(args):
    app = QtWidgets.QApplication(sys.argv)
    try:
        app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    except Exception:
        pass
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
