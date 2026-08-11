import sys

if (getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None)
        and "--cli" in sys.argv):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.AllocConsole()
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:
            pass
    else:
        try:
            sys.stdout = open("/dev/tty", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
            sys.stdin = open("/dev/tty", "r", encoding="utf-8", errors="replace")
        except Exception:
            pass

from mc_migrator.__main__ import main

if __name__ == "__main__":
    main()
