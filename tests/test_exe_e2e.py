import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "MCModMigrator.exe")


def make_jar(path, meta):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps(meta))
        z.writestr("assets/readme.txt", "x")


def main():
    if not os.path.exists(EXE):
        print("[S K I P] 未找到 %s，先运行打包再测" % EXE)
        return 0
    tmp = tempfile.mkdtemp(prefix="mcmod_exe_")
    src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
    for mc, ver in ((src, "1.20.1-fabric"), (dst, "1.20.1-fabric")):
        vdir = os.path.join(mc, "versions", ver)
        os.makedirs(os.path.join(vdir, "mods"), exist_ok=True)
        open(os.path.join(vdir, ver + ".json"), "w", encoding="utf-8").write(
            json.dumps({"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}))
    make_jar(os.path.join(src, "versions", "1.20.1-fabric", "mods", "sodium_extra.jar"),
             {"id": "sodium-extra", "name": "Sodium Extra"})
    make_jar(os.path.join(src, "versions", "1.20.1-fabric", "mods", "jade.jar"),
             {"id": "jade", "name": "Jade"})
    os.makedirs(os.path.join(src, "versions", "1.20.1-fabric", "config"), exist_ok=True)
    open(os.path.join(src, "versions", "1.20.1-fabric", "config", "test.toml"), "w").write("x")

    args = [EXE, "--cli", "--yes",
            "--src-root", src, "--src-version", "1.20.1-fabric",
            "--target-root", dst, "--target-version", "1.20.1-fabric",
            "--target-loader", "fabric", "--target-mc", "1.20.1",
            "--migrate", "config,options,saves,stray"]
    try:
        res = subprocess.run(args, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("✗ exe 迁移超时")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1
    print("exe 退出码:", res.returncode)

    dst_v = os.path.join(dst, "versions", "1.20.1-fabric")
    jars = os.listdir(os.path.join(dst_v, "mods"))
    print("目标 mods:", sorted(jars))
    ok = (res.returncode == 0
          and any("sodium-extra" in j for j in jars)
          and any("sodium-fabric" in j for j in jars)
          and any("jade" in j.lower() for j in jars)
          and os.path.exists(os.path.join(dst_v, "config", "test.toml")))
    print("✓ exe 完整迁移（下载 mods + 依赖 + config 迁移）" if ok else "✗ 产物不完整")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
