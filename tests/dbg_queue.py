import json
import os
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mod_migrator as mm

LOG = "迁移日志.txt"
if os.path.exists(LOG):
    os.remove(LOG)


def pump(app, seconds=0.05):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


tmp = tempfile.mkdtemp(prefix="mcmod_dbgq_")
src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
for mc, ver in ((src, "1.20.1-fabric"), (dst, "1.20.1-fabric")):
    vdir = os.path.join(mc, "versions", ver)
    os.makedirs(os.path.join(vdir, "mods"), exist_ok=True)
    open(os.path.join(vdir, ver + ".json"), "w", encoding="utf-8").write(
        json.dumps({"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}))
with zipfile.ZipFile(os.path.join(src, "versions", "1.20.1-fabric", "mods",
                                  "sodium_extra.jar"), "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("fabric.mod.json", json.dumps({"id": "sodium-extra", "name": "Sodium Extra"}))

app = mm.QtWidgets.QApplication([])
win = mm.MainWindow()
win.src_root_edit.setText(src)
win.dst_root_edit.setText(dst)
pump(app, 0.3)
win.dst_version_combo.setCurrentIndex(1)
win.mc_combo.setEditText("1.20.1")
win.start_btn.click()

deadline = time.time() + 120
while time.time() < deadline and win.worker is not None:
    pump(app, 0.3)
print("worker is None:", win.worker is None, flush=True)
text = win.log_view.toPlainText()
print("日志视图包含 已下载:", "已下载" in text, flush=True)
print("日志视图包含 开始迁移:", "开始迁移" in text, flush=True)
print("日志视图包含 完成:", "完成" in text, flush=True)
print("--- 视图日志前 800 字 ---", flush=True)
print(text[:800], flush=True)
if os.path.exists(LOG):
    content = open(LOG, encoding="utf-8").read()
    print("--- 落盘日志 迁移日志.txt ---", flush=True)
    print(content[:800], flush=True)
else:
    print("迁移日志.txt 不存在！", flush=True)
