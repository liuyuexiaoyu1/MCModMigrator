import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mc_migrator as mm

app = mm.QtWidgets.QApplication([])
win = mm.MainWindow()
win.resize(820, 640)
win.show()
app.processEvents()

print("下载线程框宽:", win.threads_spin.width(), "| 分析线程框宽:", win.analysis_spin.width())
print("下载线程框 x:", win.threads_spin.mapTo(win, win.threads_spin.rect().topLeft()).x(),
      "| 分析线程框 x:", win.analysis_spin.mapTo(win, win.analysis_spin.rect().topLeft()).x())
win.resize(1600, 900)
app.processEvents()
print("最大化后 下载线程框宽:", win.threads_spin.width(), "| 分析线程框宽:", win.analysis_spin.width())
print("最大化后 下载线程框 x:", win.threads_spin.mapTo(win, win.threads_spin.rect().topLeft()).x(),
      "| 分析线程框 x:", win.analysis_spin.mapTo(win, win.analysis_spin.rect().topLeft()).x())
print("三个勾选 x:", win.proxy_chk.mapTo(win, win.proxy_chk.rect().topLeft()).x(),
      win.ignore_fork_chk.mapTo(win, win.ignore_fork_chk.rect().topLeft()).x(),
      win.failures_chk.mapTo(win, win.failures_chk.rect().topLeft()).x())
print("saves x:", win.data_checks["saves"].mapTo(win, win.data_checks["saves"].rect().topLeft()).x())
