import faulthandler
import json
import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mod_migrator as mm

tmp = tempfile.mkdtemp(prefix="mcmod_freeze_")
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
win.show()
win.src_root_edit.setText(src)
win.dst_root_edit.setText(dst)
win.dst_version_combo.setCurrentIndex(1)
win.mc_combo.setEditText("1.20.1")
app.processEvents()

faulthandler.dump_traceback_later(45, exit=True)
win.start_btn.click()
print("已点击开始，等待迁移完成...", flush=True)

deadline = time.time() + 60
while time.time() < deadline and win.worker is not None:
    app.processEvents()
    time.sleep(0.05)
print("迁移结束，worker is None:", win.worker is None, flush=True)
print("--- 日志 ---", flush=True)
print(win.log_view.toPlainText()[:3000], flush=True)
