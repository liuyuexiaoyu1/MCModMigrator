import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mod_migrator as mm  # noqa: E402

app = mm.QtWidgets.QApplication([])
srv = tempfile.mkdtemp(prefix="mcmod_bench_")
os.makedirs(os.path.join(srv, "mods"), exist_ok=True)
open(os.path.join(srv, "server.properties"), "w").close()
for i in range(100):
    with zipfile.ZipFile(os.path.join(srv, "mods", "mod_%03d.jar" % i), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "mod_%d" % i, "name": "Mod %d" % i}))

win = mm.MainWindow()
win.src_root_edit.setText(srv)
app.processEvents()

mm.clients._SNIFF_CACHE.clear()
t0 = time.perf_counter()
win.src_root_edit.setText("")
app.processEvents()
win.src_root_edit.setText(srv)
app.processEvents()
cold_ms = (time.perf_counter() - t0) * 1000

t0 = time.perf_counter()
win.c2c_radio.setChecked(True)
win.s2s_radio.setChecked(True)
app.processEvents()
hot_ms = (time.perf_counter() - t0) * 1000

print("冷切换（首次 sniff 100 个 jar）: %.0f ms" % cold_ms)
print("热切换（缓存+去重）: %.0f ms" % hot_ms)
shutil.rmtree(srv, ignore_errors=True)
print("PASS" if hot_ms < 50 else "TOO SLOW")
