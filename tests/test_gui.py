import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mc_migrator as mm  # noqa: E402

PASS = 0


def ok(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        print("  ✗ FAIL: %s" % msg)
        sys.exit(1)


def pump(app, seconds=0.05):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def gui_migration_test():
    tmp = tempfile.mkdtemp(prefix="mcmod_gui_mig_")
    src = os.path.join(tmp, "src")
    dst = os.path.join(tmp, "dst")
    for mc, ver in ((src, "1.20.1-fabric"), (dst, "1.20.1-fabric")):
        vdir = os.path.join(mc, "versions", ver)
        os.makedirs(os.path.join(vdir, "mods"), exist_ok=True)
        open(os.path.join(vdir, ver + ".json"), "w", encoding="utf-8").write(
            json.dumps({"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}))
    with zipfile.ZipFile(os.path.join(src, "versions", "1.20.1-fabric", "mods",
                                      "sodium_extra.jar"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "sodium-extra", "name": "Sodium Extra"}))
    with zipfile.ZipFile(os.path.join(src, "versions", "1.20.1-fabric", "mods",
                                      "gca_wrapper.jar"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps(
            {"id": "gca_wrapper", "name": "gugle-carpet-addition-Wrapper", "version": "1.0.6"}))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as iz:
            iz.writestr("fabric.mod.json", json.dumps(
                {"id": "gca", "name": "gugle-carpet-addition", "version": "2.12.6"}))
        z.writestr("META-INF/jars/real-mod.jar", buf.getvalue())
        z.writestr("META-INF/jars/real-mod-mc1.21.jar", buf.getvalue())

    import mc_migrator as _mm
    app = _mm.QtWidgets.QApplication.instance() or _mm.QtWidgets.QApplication([])
    win = _mm.MainWindow()
    win.src_root_edit.setText(src)
    win.dst_root_edit.setText(dst)
    pump(app, 0.3)
    win.dst_version_combo.setCurrentIndex(1)
    win.mc_combo.setEditText("1.20.1")
    win.start_btn.click()
    pump(app, 0.2)

    deadline = time.time() + 180
    while time.time() < deadline and win.worker is not None:
        pump(app, 0.3)
    finished = win.worker is None
    text = win.log_view.toPlainText()
    jars = os.listdir(os.path.join(dst, "versions", "1.20.1-fabric", "mods"))
    ok(finished, "GUI 迁移正常结束（未死锁未卡死）")
    ok(any("sodium-extra" in j for j in jars), "GUI 迁移下载成功: %s" % jars)
    ok("已下载" in text, "GUI 日志经信号正常渲染")
    ok("===== 完成" in text, "完成日志齐全")
    shutil.rmtree(tmp, ignore_errors=True)


def gui_confirm_test():
    import mc_migrator as _mm
    app = _mm.QtWidgets.QApplication.instance() or _mm.QtWidgets.QApplication([])
    win = _mm.MainWindow()
    class FakeWorker:
        def __init__(self):
            self._ans_event = threading.Event()
            self._answer = False

        def set_answer(self, val):
            self._answer = val
            self._ans_event.set()

    win.worker = FakeWorker()

    def _answer_yes():
        box = app.activeModalWidget()
        if box is not None:
            box.done(_mm.QtWidgets.QMessageBox.StandardButton.Yes)

    from PySide6.QtCore import QTimer
    QTimer.singleShot(300, _answer_yes)
    win._show_confirm_dialog("测试：是否下载？")
    ok(win.worker._answer is True, "确认弹窗正常应答路径")

    _mm.gui.CONFIRM_TIMEOUT_MS = 100
    win.worker = FakeWorker()
    win._show_confirm_dialog("测试：超时自动继续？")
    ok(win.worker._answer is True and "确认超时" in win.log_view.toPlainText(),
       "确认框超时自动按「是」并提示（不永久卡死）")
    _mm.gui.CONFIRM_TIMEOUT_MS = 60000


tmp = tempfile.mkdtemp(prefix="mcmod_gui_")
app = mm.QtWidgets.QApplication([])
win = mm.MainWindow()

try:
    ok(win.c2c_radio.isChecked() and win._mode == "c2c", "默认 C2C 模式")
    ok(hasattr(win, "s2s_radio") and not hasattr(win, "src_server_radio")
       and not hasattr(win, "dst_server_radio"),
       "全应用仅顶部一个模式选择（无分散的服务端单选）")
    ok(win.src_version_row.isHidden() is False and win.dst_client_widget.isHidden() is False,
       "C2C 下源/目标客户端版本行可见")
    ok(not win.src_version_label.isHidden() and not win.dst_version_label.isHidden(),
       "C2C 下『客户端版本』标签可见")
    ok(win.dst_server_widget.isHidden(), "C2C 下服务端方式行隐藏")
    ok(win.data_checks["server"].isHidden(), "C2C 下隐藏『服务端文件』数据类别")
    ok(not win.data_checks["options"].isHidden(), "C2C 下显示 options.txt 类别")

    ok("正在拉取" in win.mc_status_label.text(), "版本列表未就绪时显示『正在拉取』")
    ok(win.mods_chk.isChecked() and not hasattr(win, "data_master_chk"),
       "总开关改为『迁移mod』且默认勾选")
    ok(win.proxy_chk.isChecked(), "默认勾选使用系统代理")
    ok(win.threads_spin.value() == 4, "默认下载线程数 4")
    ok(win.analysis_spin.value() == 8, "默认分析线程数 8")
    ok(win.failures_chk.isChecked(), "默认勾选完成后打印匹配失败及开源链接")
    ok(not win.ignore_fork_chk.isChecked(), "默认不勾选忽略 fork 防护")
    for _ in range(100):
        pump(app, 0.1)
        if win._mc_releases:
            break
    if win._mc_releases:
        ok("已拉取" in win.mc_status_label.text(), "拉取完成后状态更新: %s" % win.mc_status_label.text())
        ok(win.mc_combo.count() == len(win._mc_releases) and win.mc_combo.itemText(0) == win._mc_releases[0],
           "清单已加载 %d 个正式版（默认选中最新 %s）" % (len(win._mc_releases), win._mc_releases[0]))
        rel_count = win.mc_combo.count()
        win.show_all_chk.setChecked(True)
        pump(app)
        ok(win.mc_combo.count() >= rel_count, "勾选『显示所有版本（含快照）』后数量增加 (%d → %d)"
           % (rel_count, win.mc_combo.count()))
        win.show_all_chk.setChecked(False)
    else:
        print("  · Mojang 清单不可达，跳过在线断言（界面仍可手动输入）")

    fake = os.path.join(tmp, "mc")
    vdir = os.path.join(fake, "versions", "1.20.1-fabric", "mods")
    os.makedirs(vdir, exist_ok=True)
    open(os.path.join(fake, "versions", "1.20.1-fabric", "1.20.1-fabric.json"), "w",
         encoding="utf-8").write('{"id": "1.20.1-fabric", "libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}')
    win.dst_root_edit.setText(fake)
    pump(app)
    win.dst_version_combo.setCurrentIndex(1)
    pump(app)
    ok(win.mc_combo.currentText() == "1.20.1", "C2C 自动读取目标 MC 版本 → %s" % win.mc_combo.currentText())

    win.src_root_edit.setText(os.path.join(fake, "versions", "1.20.1-fabric"))
    pump(app)
    ok(win.src_root_edit.text() == fake, "源直选版本隔离目录后回填 .minecraft 根目录")
    ok(win._src_versions and win.src_version_combo.currentIndex() == 1
       and win._src_versions[win.src_version_combo.currentIndex() - 1][0] == "1.20.1-fabric",
       "源自动选中版本 1.20.1-fabric")
    win.src_version_combo.setCurrentIndex(0)
    pump(app)
    ok(win.src_version_combo.currentIndex() == 0 and win.src_version_combo.itemText(0) == "非版本隔离",
       "源下拉第一项为『非版本隔离』")
    win.src_version_combo.setCurrentIndex(1)
    pump(app)
    win.dst_root_edit.setText(os.path.join(fake, "versions", "1.20.1-fabric"))
    pump(app)
    ok(win.dst_root_edit.text() == fake, "目标直选版本隔离目录后回填根目录")
    ok(win.dst_version_combo.currentIndex() == 1, "目标自动选中版本")
    ok(win.mc_combo.currentText() == "1.20.1", "目标 MC 版本自动读取")

    win.s2s_radio.setChecked(True)
    pump(app)
    ok(win._mode == "s2s", "切到 S2S 模式")
    ok(win.src_version_label.isHidden() and win.dst_version_label.isHidden(),
       "S2S 下源/目标『客户端版本』标签隐藏")
    ok(win.src_version_row.isHidden() and win.dst_client_widget.isHidden(),
       "S2S 下源/目标版本行隐藏")
    win.c2c_radio.setChecked(True)
    win.s2s_radio.setChecked(True)
    pump(app)
    ok(win._mode == "s2s" and win.src_version_label.isHidden(), "重复切换去重后仍为 S2S")
    ok(not win.dst_server_widget.isHidden(), "S2S 下显示『迁移/覆盖』方式行")
    ok(not win.data_checks["server"].isHidden(), "S2S 下显示『服务端文件』数据类别")
    ok(win.data_checks["options"].isHidden(), "S2S 下隐藏 options.txt 类别")
    before = win.mc_combo.currentText()
    win.dst_version_combo.setCurrentIndex(0)
    pump(app)
    ok(win.mc_combo.currentText() == before, "S2S 不自动改 MC 版本（需手动选择）")
    ok(not win.overwrite_inplace_radio.isChecked(), "S2S 默认『迁移到新的空服务端』")
    ok(win.dst_root_edit.isEnabled(), "迁移模式下目标目录可选")

    win.overwrite_inplace_radio.setChecked(True)
    pump(app)
    ok(win._overwrite_mode, "覆盖模式已开启")
    ok(not win.dst_root_edit.isEnabled(), "覆盖模式下目标目录禁用")
    ok(all(not ck.isEnabled() for ck in win.data_checks.values()),
       "覆盖模式下全部数据类别勾选禁用")
    ok(win.mods_chk.isEnabled(), "覆盖模式下『迁移mod』仍可用")
    ok(win.overwrite_inplace_radio.text().find("只更新模组") >= 0,
       "覆盖模式单选文案说明『只更新模组到指定游戏版本』")
    win.overwrite_radio.setChecked(True)
    pump(app)
    ok(win.dst_root_edit.isEnabled()
       and all(ck.isEnabled() for ck in win.data_checks.values()), "切回迁移模式恢复正常")

    win.overwrite_inplace_radio.setChecked(True)
    pump(app)
    win.c2c_radio.setChecked(True)
    pump(app)
    ok(win._mode == "c2c" and not win._overwrite_mode, "覆盖模式切回 C2C 后覆盖标志清除")
    ok(win.dst_root_edit.isEnabled() and win.dst_browse_btn.isEnabled(),
       "切回 C2C 后目标路径可用")
    ok(all(ck.isEnabled() for ck in win.data_checks.values()),
       "切回 C2C 后数据迁移勾选恢复可用")

    win.c2c_radio.setChecked(True)
    pump(app)
    srv = os.path.join(tmp, "srv")
    os.makedirs(os.path.join(srv, "mods"), exist_ok=True)
    open(os.path.join(srv, "server.properties"), "w").close()
    win.src_root_edit.setText(srv)
    pump(app)
    ok(win.s2s_radio.isChecked() and win._mode == "s2s", "源目录是服务端时自动切到 S2S")

    print("\nGUI 冒烟测试全部通过: %d 项断言" % PASS)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

gui_migration_test()
gui_confirm_test()
