import ctypes
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "MCModMigrator.exe")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

kernel32 = ctypes.windll.kernel32


def all_pids():
    out = subprocess.run(
        ["powershell", "-Command",
         "Get-Process MCModMigrator -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }"],
        capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.strip().isdigit()}


def has_console(pid):
    kernel32.FreeConsole()
    if kernel32.AttachConsole(pid):
        kernel32.FreeConsole()
        return True
    return False


def run_case(args, expect_console, settle):
    before = all_pids()
    p = subprocess.Popen(args)
    time.sleep(settle)
    new_pids = all_pids() - before
    if not new_pids and p.poll() is not None:
        new_pids = {p.pid}
    results = {pid: has_console(pid) for pid in new_pids}
    try:
        p.wait(3)
    except Exception:
        pass
    for pid in new_pids:
        subprocess.run(["powershell", "-Command",
                        "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid],
                       capture_output=True)
    return results, p.returncode


def main():
    if not os.path.exists(EXE):
        print("[S K I P] 未找到 exe")
        return 0

    gui_consoles, _ = run_case([EXE], expect_console=False, settle=8)
    ok_gui = gui_consoles and not any(gui_consoles.values())
    print("GUI 模式进程控制台情况: %s → %s"
          % (gui_consoles, "✓ 无 cmd 窗口" if ok_gui else "✗ 仍有控制台"))
    
    cli_consoles, _ = run_case([EXE, "--cli"], expect_console=True, settle=5)
    ok_cli = cli_consoles and any(cli_consoles.values())
    print("CLI 模式进程控制台情况: %s → %s"
          % (cli_consoles, "✓ 控制台可用" if ok_cli else "✗ CLI 无控制台"))

    print("\n全部通过" if ok_gui and ok_cli else "\n存在失败项")
    return 0 if (ok_gui and ok_cli) else 1


if __name__ == "__main__":
    sys.exit(main())
