import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mc_migrator as mm  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PY):
    VENV_PY = sys.executable


def make_client(mc_root, version, loader_marker, mods=None, extra_dirs=None, extra_files=None):
    vdir = os.path.join(mc_root, "versions", version)
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, version + ".json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": version,
                            "libraries": [{"name": loader_marker}]}))
    mods = mods or []
    if mods:
        os.makedirs(os.path.join(vdir, "mods"), exist_ok=True)
        for name, meta, *rest in mods:
            with zipfile.ZipFile(os.path.join(vdir, "mods", name), "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("fabric.mod.json", json.dumps(meta))
                z.writestr("assets/readme.txt", "x")
                if rest and rest[0]:
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as iz:
                        iz.writestr("fabric.mod.json", json.dumps(rest[0]))
                    z.writestr("META-INF/jars/real-mod.jar", buf.getvalue())
                    z.writestr("META-INF/jars/real-mod-mc1.21.jar", buf.getvalue())
    for d in extra_dirs or []:
        os.makedirs(os.path.join(vdir, d), exist_ok=True)
        with open(os.path.join(vdir, d, "data.txt"), "w") as f:
            f.write("fake data")
    for fname in extra_files or []:
        with open(os.path.join(vdir, fname), "w") as f:
            f.write("fake file")


def run_cli(args):
    return subprocess.run([VENV_PY, os.path.join(ROOT, "mod_migrator.py"), "--cli", "--yes"] + args,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=ROOT, timeout=600)


def main():
    tmp = tempfile.mkdtemp(prefix="mcmod_e2e_")
    src_mc = os.path.join(tmp, "src")
    dst_mc = os.path.join(tmp, "dst")
    os.makedirs(src_mc)
    os.makedirs(dst_mc)

    make_client(src_mc, "1.20.1-fabric", "net.fabricmc:fabric-loader:0.15.10",
                mods=[
                    ("sodium_extra_fake.jar", {"id": "sodium-extra", "name": "Sodium Extra", "version": "0.0.1"}),
                    ("jade_fake.jar", {"id": "jade", "name": "Jade", "version": "0.0.1"}),
                    ("gca_wrapper_fake.jar",
                     {"id": "gca_wrapper", "name": "gugle-carpet-addition-Wrapper", "version": "1.0.6"},
                     {"id": "gca", "name": "gugle-carpet-addition", "version": "2.12.6"}),
                    ("my_private_mod.jar", {"id": "my_private_mod", "name": "My Private Mod", "version": "0.0.1"}),
                    ("sodium_fake.jar", {"id": "sodium", "name": "Sodium", "version": "0.0.1"}),
                    ("appleskin_cn.jar", {"id": "appleskin_cn", "name": "苹果皮", "version": "0.0.1"}),
                ],
                extra_dirs=["config", "journeymap"],
                extra_files=["options.txt"])
    os.makedirs(os.path.join(src_mc, "versions", "1.20.1-fabric", "saves", "world1"), exist_ok=True)
    with open(os.path.join(src_mc, "versions", "1.20.1-fabric", "saves", "world1", "level.dat"), "w") as f:
        f.write("fake world")
    os.makedirs(os.path.join(src_mc, "versions", "1.20.1-fabric", "saves", "existing_world"), exist_ok=True)
    with open(os.path.join(src_mc, "versions", "1.20.1-fabric", "saves", "existing_world", "level.dat"), "w") as f:
        f.write("from-source")

    make_client(dst_mc, "1.20.1-fabric", "net.fabricmc:fabric-loader:0.15.10")

    os.makedirs(os.path.join(dst_mc, "versions", "1.20.1-fabric", "saves", "existing_world"), exist_ok=True)
    with open(os.path.join(dst_mc, "versions", "1.20.1-fabric", "saves", "existing_world", "level.dat"), "w") as f:
        f.write("existing")

    args = ["--src-root", src_mc, "--src-version", "1.20.1-fabric",
            "--target-root", dst_mc, "--target-version", "1.20.1-fabric",
            "--target-loader", "fabric",
            "--migrate", "config,options,saves,stray,optional"]
    try:
        res = run_cli(args)
    except subprocess.TimeoutExpired:
        print("✗ 迁移超时（10 分钟）")
        shutil.rmtree(tmp, ignore_errors=True)
        return 2
    except Exception as e:
        print("✗ 无法运行 CLI: %s" % e)
        shutil.rmtree(tmp, ignore_errors=True)
        return 2

    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-3000:])

    if "网络错误" in out:
        print("\n[S K I P] 无法访问 Modrinth（网络被墙？），跳过在线断言")
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    dst_v = os.path.join(dst_mc, "versions", "1.20.1-fabric")
    jars = sorted(os.listdir(os.path.join(dst_v, "mods")))
    print("\n目标 mods 目录: %s" % jars)

    check(res.returncode == 0, "CLI 正常退出 (rc=%d)" % res.returncode)
    check("已自动读取目标版本: 1.20.1" in out, "目标 MC 版本自动读取")
    check(any("sodium" in j for j in jars), "下载了 sodium")
    check(any("sodium-extra" in j for j in jars), "下载了 sodium-extra")
    check(any("jade" in j.lower() for j in jars), "下载了 jade")
    check(any("gugle-carpet-addition" in j.lower() for j in jars), "下载了 gca (GugleCarpetAddition)")

    real_gca = next((j for j in jars if "gugle-carpet-addition" in j.lower()), None)
    if real_gca:
        meta = mm.parse_mod_jar(os.path.join(dst_v, "mods", real_gca))
        check(meta and meta["name"] == "gugle-carpet-addition-Wrapper",
              "真实 GCA jar 解析出 name=%r id=%r (kind=%s)"
              % (meta["name"] if meta else None, meta["id"] if meta else None,
                 meta["kind"] if meta else None))

    try:
        import requests
        r = requests.get("https://api.modrinth.com/v2/project/gca/version",
                         params={"game_versions": '["1.20.1"]', "loaders": '["fabric"]'},
                         headers={"User-Agent": "mc-mod-migrator-test"}, timeout=30)
        newest = max(r.json(), key=lambda v: v.get("date_published") or "")
        check(any(newest["version_number"] in j for j in jars),
              "优先最新发布: %s (%s, %s)"
              % (newest["version_number"], newest.get("version_type"), (newest.get("date_published") or "")[:10]))
    except Exception as e:
        print("  · 跳过最新版本断言: %s" % e)

    check("my_private_mod" in out and "手动处理" in out, "私有模组进入手动清单")
    check("重复项目合并 1 个" in out and "sodium_fake.jar" in out,
          "重复项目独立统计，不进『跳过』清单")
    check("合并重复" in out, "重复项日志标记为『合并重复』而非跳过")
    check("依赖图统计" in out, "下载时即时构建依赖图并统计")
    check("通过 mcmod.cn" in out and any("appleskin" in j.lower() for j in jars),
          "Modrinth 搜不到时经 mcmod.cn 兜底下载 AppleSkin")
    check(os.path.exists(os.path.join(dst_v, "config", "data.txt")), "config 已迁移")
    check(os.path.exists(os.path.join(dst_v, "options.txt")), "options.txt 已迁移")
    check(os.path.exists(os.path.join(dst_v, "saves", "world1", "level.dat")), "saves 新世界已复制")
    check(open(os.path.join(dst_v, "saves", "existing_world", "level.dat")).read() == "existing",
          "saves 已有世界未被覆盖")
    check(open(os.path.join(dst_v, "saves", "existing_world_old", "level.dat")).read() == "from-source",
          "saves 重名世界自动加 _old 迁移")
    check(os.path.exists(os.path.join(dst_v, "journeymap", "data.txt")), "模组杂项目录 journeymap 已迁移")

    reports = [f for f in os.listdir(ROOT) if f.startswith("迁移报告_")]
    check(bool(reports), "生成了报告文件")
    for r in reports:
        os.remove(os.path.join(ROOT, r))

    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("\n客户端测试失败 %d 项: %s" % (len(fails), fails))
        return 1
    print("\n客户端端到端测试通过 ✓")
    return 0


def test_server(tmp):
    src = os.path.join(tmp, "srv_src")
    dst = os.path.join(tmp, "srv_dst")
    for d in (src, dst):
        os.makedirs(os.path.join(d, "mods"), exist_ok=True)
    # 源服务端
    with zipfile.ZipFile(os.path.join(src, "mods", "sodium_extra_server.jar"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "sodium-extra", "name": "Sodium Extra"}))
    os.makedirs(os.path.join(src, "config"), exist_ok=True)
    open(os.path.join(src, "config", "server.toml"), "w").write("x")
    os.makedirs(os.path.join(src, "world", "worldA"), exist_ok=True)
    open(os.path.join(src, "world", "worldA", "level.dat"), "w").write("worldA")
    for fn in ("server.properties", "eula.txt", "whitelist.json"):
        open(os.path.join(src, fn), "w").write(fn)
    os.makedirs(os.path.join(src, "journeymap"), exist_ok=True)
    open(os.path.join(src, "journeymap", "data.txt"), "w").write("x")
    os.makedirs(os.path.join(dst, "world", "existing_world"), exist_ok=True)
    open(os.path.join(dst, "world", "existing_world", "level.dat"), "w").write("existing")

    args = ["--src-root", src, "--target-root", dst,
            "--target-loader", "fabric", "--target-mc", "1.20.1",
            "--migrate", "config,saves,stray,server"]
    res = run_cli(args)
    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-2000:])

    if "网络错误" in out:
        return None

    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    jars = os.listdir(os.path.join(dst, "mods"))
    print("目标服务端 mods: %s" % sorted(jars))
    check(res.returncode == 0, "服务端 CLI 正常退出 (rc=%d)" % res.returncode)
    check("服务端" in out, "识别出服务端模式")
    check(any("sodium-extra" in j for j in jars), "服务端下载了 sodium-extra")
    check(any("sodium" in j and "sodium-extra" not in j for j in jars), "服务端下载了依赖 sodium")
    check(os.path.exists(os.path.join(dst, "server.properties")), "server.properties 已迁移")
    check(os.path.exists(os.path.join(dst, "eula.txt")), "eula.txt 已迁移")
    check(os.path.exists(os.path.join(dst, "whitelist.json")), "whitelist.json 已迁移")
    check(os.path.exists(os.path.join(dst, "world", "worldA", "level.dat")), "world/worldA 已迁移")
    check(open(os.path.join(dst, "world", "existing_world", "level.dat")).read() == "existing",
          "world 已有世界未被覆盖")
    check(os.path.exists(os.path.join(dst, "config", "server.toml")), "服务端 config 已迁移")
    check(os.path.exists(os.path.join(dst, "journeymap", "data.txt")), "服务端杂项目录已迁移")
    check(not os.path.exists(os.path.join(dst, "options.txt")), "未请求 options.txt（不迁移）")

    reports = [f for f in os.listdir(ROOT) if f.startswith("迁移报告_")]
    for r in reports:
        os.remove(os.path.join(ROOT, r))
    if fails:
        print("\n服务端测试失败 %d 项: %s" % (len(fails), fails))
        return 1
    print("\n服务端端到端测试通过 ✓")
    return 0


def test_server_overwrite(tmp):
    srv = os.path.join(tmp, "ov_srv")
    os.makedirs(os.path.join(srv, "mods"), exist_ok=True)
    with zipfile.ZipFile(os.path.join(srv, "mods", "sodium_extra_old.jar"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "sodium-extra", "name": "Sodium Extra"}))
    os.makedirs(os.path.join(srv, "config"), exist_ok=True)
    open(os.path.join(srv, "config", "keep.toml"), "w").write("original")
    os.makedirs(os.path.join(srv, "world", "worldA"), exist_ok=True)
    open(os.path.join(srv, "world", "worldA", "level.dat"), "w").write("worldA")
    open(os.path.join(srv, "server.properties"), "w").write("x")
    open(os.path.join(srv, "options.txt"), "w").write("should-not-exist")

    args = ["--src-root", srv, "--overwrite",
            "--target-loader", "fabric", "--target-mc", "1.20.1"]
    res = run_cli(args)
    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-1500:])

    if "网络错误" in out:
        return None

    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    jars = os.listdir(os.path.join(srv, "mods"))
    print("覆盖后 mods:", sorted(jars))
    check(res.returncode == 0, "覆盖模式 CLI 正常退出 (rc=%d)" % res.returncode)
    check(any("sodium-extra" in j for j in jars), "在原服务端上更新了 mods")
    check("=== 迁移游戏数据" not in out, "覆盖模式不执行任何数据迁移")
    check("仅更新 mods" in out, "明确提示只更新 mods")
    check(open(os.path.join(srv, "config", "keep.toml")).read() == "original",
          "config 原样保留")
    check(open(os.path.join(srv, "world", "worldA", "level.dat")).read() == "worldA",
          "world 原样保留")
    check(open(os.path.join(srv, "server.properties")).read() == "x", "server.properties 原样保留")
    check(open(os.path.join(srv, "options.txt")).read() == "should-not-exist",
          "options.txt 未被迁移/改动（覆盖模式不碰任何数据）")

    reports = [f for f in os.listdir(ROOT) if f.startswith("迁移报告_")]
    for r in reports:
        os.remove(os.path.join(ROOT, r))
    if fails:
        print("\n覆盖模式测试失败 %d 项: %s" % (len(fails), fails))
        return 1
    print("\n覆盖模式端到端测试通过 ✓")
    return 0


def test_fork_detection(tmp):
    src_mc = os.path.join(tmp, "fork_src")
    vdir = os.path.join(src_mc, "versions", "1.20.1-fabric", "mods")
    os.makedirs(vdir, exist_ok=True)
    open(os.path.join(src_mc, "versions", "1.20.1-fabric", "1.20.1-fabric.json"), "w",
         encoding="utf-8").write(json.dumps({"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}))
    dst_mc = os.path.join(tmp, "fork_dst")
    dst_mc2 = os.path.join(tmp, "fork_dst2")
    for dmc in (dst_mc, dst_mc2):
        os.makedirs(os.path.join(dmc, "versions", "1.20.1-fabric", "mods"), exist_ok=True)
        open(os.path.join(dmc, "versions", "1.20.1-fabric", "1.20.1-fabric.json"), "w",
             encoding="utf-8").write(json.dumps({"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}))

    try:
        import requests
        import tempfile as _tf
        vs = requests.get("https://api.modrinth.com/v2/project/jade/version",
                          params={"game_versions": '["1.20.1"]', "loaders": '["fabric"]'},
                          headers={"User-Agent": "mc-mod-migrator-test"}, timeout=30).json()
        newest = max(vs, key=lambda v: v.get("date_published") or "")
        tj = os.path.join(_tf.gettempdir(), newest["files"][0]["filename"])
        with open(tj, "wb") as f:
            f.write(requests.get(newest["files"][0]["url"],
                                 headers={"User-Agent": "mc-mod-migrator-test"}, timeout=60).content)
        real_authors = mm.parse_mod_jar(tj)["authors"]
    except Exception as e:
        print("  · 跳过 fork 测试（无法获取官方 jar）: %s" % e)
        return None

    for name, mid, authors in (("jade_real.jar", "jade", real_authors),
                               ("sodium_fork.jar", "sodium", ["EvilForker"])):
        with zipfile.ZipFile(os.path.join(vdir, name), "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("fabric.mod.json", json.dumps({"id": mid, "name": mid.title(), "authors": authors}))

    res = run_cli(["--src-root", src_mc, "--src-version", "1.20.1-fabric",
                   "--target-root", dst_mc, "--target-version", "1.20.1-fabric",
                   "--target-loader", "fabric", "--target-mc", "1.20.1",
                   "--yes", "--skip-data"])
    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-1200:])

    fails = []

    def check(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    jars = os.listdir(os.path.join(dst_mc, "versions", "1.20.1-fabric", "mods"))
    check(res.returncode == 0, "fork 测试 CLI 正常退出 (rc=%d)" % res.returncode)
    check(any("jade" in j.lower() for j in jars), "作者一致的 jade 正常下载")
    check(not any("sodium" in j for j in jars), "fork 版 sodium 未被下载")
    check("疑似 fork 版模组" in out, "fork 版进手动清单并提示原因")
    check("开源仓库" in out and "github.com" in out,
          "完成后打印匹配失败清单并附开源链接")
    check("手动处理" in out, "fork 版进入手动清单")
    # 开启 --ignore-fork 后：fork 版也直接下载，仅日志提示
    res2 = run_cli(["--src-root", src_mc, "--src-version", "1.20.1-fabric",
                    "--target-root", dst_mc2, "--target-version", "1.20.1-fabric",
                    "--target-loader", "fabric", "--target-mc", "1.20.1",
                    "--yes", "--skip-data", "--ignore-fork"])
    out2 = (res2.stdout + "\n" + res2.stderr).strip()
    jars2 = os.listdir(os.path.join(dst_mc2, "versions", "1.20.1-fabric", "mods"))
    check(res2.returncode == 0, "ignore-fork CLI 正常退出")
    check(any("sodium" in j for j in jars2), "--ignore-fork 后 fork 版直接下载")
    check("已按选项忽略" in out2, "忽略 fork 时日志提示")
    check("手动处理" not in out2, "开启忽略后不因 fork 进手动清单")

    reports = [f for f in os.listdir(ROOT) if f.startswith("迁移报告_")]
    for r in reports:
        os.remove(os.path.join(ROOT, r))
    if fails:
        print("fork 测试失败 %d 项: %s" % (len(fails), fails))
        return 1
    print("fork 防护测试通过 ✓")
    return 0


def test_version_dir(tmp):
    src_mc = os.path.join(tmp, "vd_src")
    dst_mc = os.path.join(tmp, "vd_dst")
    make_client(src_mc, "1.20.1-fabric", "net.fabricmc:fabric-loader:0.15.10",
                mods=[("sodium_extra_fake.jar", {"id": "sodium-extra", "name": "Sodium Extra"})])
    make_client(dst_mc, "1.20.1-fabric", "net.fabricmc:fabric-loader:0.15.10")
    src_vdir = os.path.join(src_mc, "versions", "1.20.1-fabric")
    dst_vdir = os.path.join(dst_mc, "versions", "1.20.1-fabric")

    res = run_cli(["--src-root", src_vdir, "--target-root", dst_vdir,
                   "--target-loader", "fabric", "--target-mc", "1.20.1",
                   "--migrate", "config", "--yes"])
    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-1000:])
    if "网络错误" in out:
        return None

    jars = os.listdir(os.path.join(dst_vdir, "mods"))
    ok = (res.returncode == 0
          and any("sodium-extra" in j for j in jars)
          and "自动使用客户端" in out)
    print(("  ✓ " if ok else "  ✗ ")
          + "版本隔离目录直选迁移（源+目标自动识别，rc=%d）" % res.returncode)
    reports = [f for f in os.listdir(ROOT) if f.startswith("迁移报告_")]
    for r in reports:
        os.remove(os.path.join(ROOT, r))
    return 0 if ok else 1


def test_cross_type(tmp):
    src_mc = os.path.join(tmp, "x_src")
    os.makedirs(os.path.join(src_mc, "versions", "1.20.1-fabric", "mods"), exist_ok=True)
    open(os.path.join(src_mc, "versions", "1.20.1-fabric", "1.20.1-fabric.json"), "w",
         encoding="utf-8").write('{"libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}')
    dst_srv = os.path.join(tmp, "x_dst")
    os.makedirs(os.path.join(dst_srv, "mods"), exist_ok=True)
    open(os.path.join(dst_srv, "server.properties"), "w").close()

    res = run_cli(["--src-root", src_mc, "--src-version", "1.20.1-fabric",
                   "--target-root", dst_srv, "--target-loader", "fabric"])
    out = (res.stdout + "\n" + res.stderr).strip()
    print(out[-500:])
    ok = res.returncode != 0 and "仅支持同类型迁移" in out
    print(("  ✓ " if ok else "  ✗ ") + "交叉迁移被拒绝 (rc=%d)" % res.returncode)
    return 0 if ok else 1


if __name__ == "__main__":
    tmp = tempfile.mkdtemp(prefix="mcmod_e2e_")
    try:
        rc1 = main()
        rc2 = test_server(tmp)
        rc3 = test_server_overwrite(tmp)
        rc4 = test_fork_detection(tmp)
        rc5 = test_version_dir(tmp)
        rc6 = test_cross_type(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if rc2 is None or rc3 is None or rc4 is None or rc5 is None:
        print("\n[S K I P] 网络不可用，在线测试跳过")
        sys.exit(0)
    sys.exit(1 if (rc1 or rc2 or rc3 or rc4 or rc5 or rc6) else 0)


if __name__ == "__main__":
    sys.exit(main())
