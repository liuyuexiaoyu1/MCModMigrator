import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

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
        raise SystemExit(1)


def make_jar(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)


FORGE_TOML = """\
modLoader="javafml"
loaderVersion="[38,)"
license="MIT"

[[mods]]
modId="jei"
version="15.0.0"
displayName="Just Enough Items"
authors="mezz"

[[dependencies.jei]]
modId="forge"
mandatory=true
versionRange="[38,)"
ordering="NONE"
side="BOTH"
"""

NEOFORGE_TOML = """\
modLoader="javafml"
loaderVersion="[2,)"
license="MIT"

[[mods]]
modId="sodium"
version="0.5.11"
displayName="Sodium"
authors="jellysquid3"

[[mods]]
modId="sodium-extra"
version="0.5.4"
displayName="Sodium Extra"
library="true"
"""

print("== 1. jar 元数据解析 ==")
tmp = tempfile.mkdtemp(prefix="mcmod_test_")

try:
    # fabric
    f = os.path.join(tmp, "fabric_mod.jar")
    make_jar(f, {"fabric.mod.json": json.dumps({"id": "sodium", "name": "Sodium",
                                                "version": "0.5.8"})})
    m = mm.parse_mod_jar(f)
    ok(m and m["id"] == "sodium" and m["name"] == "Sodium" and m["kind"] == "fabric",
       "fabric.mod.json: %s" % m)

    # quilt
    q = os.path.join(tmp, "quilt_mod.jar")
    make_jar(q, {"quilt.mod.json": json.dumps({"id": "quilted_fabric_api", "name": "QFAPI"})})
    m = mm.parse_mod_jar(q)
    ok(m and m["id"] == "quilted_fabric_api" and m["kind"] == "quilt", "quilt.mod.json")

    # forge toml
    fj = os.path.join(tmp, "forge_mod.jar")
    make_jar(fj, {"META-INF/mods.toml": FORGE_TOML})
    m = mm.parse_mod_jar(fj)
    ok(m and m["id"] == "jei" and m["name"] == "Just Enough Items" and m["kind"] == "forge",
       "mods.toml: %s" % m)

    # neoforge toml
    nj = os.path.join(tmp, "neoforge_mod.jar")
    make_jar(nj, {"META-INF/neoforge.mods.toml": NEOFORGE_TOML})
    m = mm.parse_mod_jar(nj)
    ok(m and m["id"] == "sodium" and m["kind"] == "neoforge", "neoforge.mods.toml: %s" % m)
    only_lib = os.path.join(tmp, "lib.jar")
    make_jar(only_lib, {"META-INF/mods.toml": NEOFORGE_TOML})
    m = mm.parse_mod_jar(only_lib)
    ok(m and m["id"] == "sodium", "toml 中第二个 mod 带 library 标记不影响取第一个")

    # mcmod.info
    lj = os.path.join(tmp, "legacy_mod.jar")
    make_jar(lj, {"mcmod.info": json.dumps([{"modid": "waila", "name": "Waila",
                                             "version": "1.8.26"}])})
    m = mm.parse_mod_jar(lj)
    ok(m and m["id"] == "waila" and m["kind"] == "forge-legacy", "mcmod.info")

    # 非模组 jar / 损坏 jar
    bad = os.path.join(tmp, "not_a_mod.jar")
    make_jar(bad, {"readme.txt": "hi"})
    ok(mm.parse_mod_jar(bad) is None, "无元数据的 jar 返回 None")
    bad2 = os.path.join(tmp, "broken.jar")
    with open(bad2, "wb") as fh:
        fh.write(b"this is not a zip file")
    ok(mm.parse_mod_jar(bad2) is None, "损坏 jar 返回 None 不抛异常")

    # 容错 JSON：描述里含未转义换行的 fabric.mod.json（EuphoriaPatcher 同款）也能解析
    ej = os.path.join(tmp, "escaped_mod.jar")
    make_jar(ej, {"fabric.mod.json": '{\n\t"id": "euphoria_patcher",\n\t"name": "Euphoria Patcher",\n'
                                     '\t"description": "line1\nline2",\n\t"version": "1.6.5",\n'
                                     '\t"entrypoints": {"main": ["mc.ep.Main"]}\n}'})
    m = mm.parse_mod_jar(ej)
    ok(m and m["id"] == "euphoria_patcher" and m["name"] == "Euphoria Patcher",
       "容错解析未转义换行: %s" % (m and m["id"]))

    print("== 2. 置信度评分 ==")
    meta = {"id": "sodium-extra", "name": "Sodium Extra", "version": "0.5.4"}
    s, why = mm.score_hit(meta, {"slug": "sodium-extra", "title": "Sodium Extra", "id": "AANobbMI"})
    ok(s == 1.0, "modid==slug 精确匹配 → %.2f (%s)" % (s, why))
    s, why = mm.score_hit(meta, {"slug": "sodium", "title": "Sodium"})
    ok(s >= mm.ASK_CONF and s < 1.0, "部分匹配 sodium → %.2f (%s)" % (s, why))
    s, why = mm.score_hit(meta, {"slug": "create", "title": "Create"})
    ok(s < mm.ASK_CONF, "无关项目低分 → %.2f" % s)
    s, why = mm.score_hit({"id": "jade", "name": "Jade"}, {"slug": "jade", "title": "Jade 🔍"})
    ok(s == 1.0, "标题含 emoji 仍精确匹配 → %.2f" % s)

    print("== 3. 杂项目录识别 ==")
    game_root = os.path.join(tmp, "game_root")
    os.makedirs(os.path.join(game_root, "journeymap"))
    os.makedirs(os.path.join(game_root, "waystones"))
    os.makedirs(os.path.join(game_root, "saves"))
    os.makedirs(os.path.join(game_root, "logs"))
    os.makedirs(os.path.join(game_root, "resourcepacks"))
    os.makedirs(os.path.join(game_root, ".fabric"))
    open(os.path.join(game_root, "options.txt"), "w").close()
    open(os.path.join(game_root, "servers.dat"), "w").close()
    open(os.path.join(game_root, "random_file.bin"), "w").close()
    dirs, files = mm.find_stray(game_root)
    ok(dirs == ["journeymap", "waystones"],
       "仅识别模组目录: %s" % dirs)
    ok(files == ["random_file.bin"], "仅识别模组文件: %s" % files)
    # 版本隔离
    iso_root = os.path.join(tmp, "iso_root")
    os.makedirs(os.path.join(iso_root, "versions", "1.20.1-fabric"), exist_ok=True)
    open(os.path.join(iso_root, "1.20.1-fabric.json"), "w").close()
    open(os.path.join(iso_root, "journeymap.txt"), "w").close()
    d2, f2 = mm.find_stray(iso_root)
    ok(f2 == ["journeymap.txt"], "版本 json 不参与杂项迁移: %s" % f2)
    # 版本隔离根目录下的核心 jar 与启动器 json（<目录名>.jar/.json）不迁移
    iso2 = os.path.join(tmp, "1.20.1-fabric")
    os.makedirs(iso2)
    open(os.path.join(iso2, "1.20.1-fabric.jar"), "w").close()
    open(os.path.join(iso2, "1.20.1-fabric.json"), "w").close()
    open(os.path.join(iso2, "waystones.json"), "w").close()
    d3, f3 = mm.find_stray(iso2)
    ok(f3 == ["waystones.json"], "核心 jar 与启动器 json 不迁移: %s" % f3)

    print("== 4. 客户端目录识别（版本隔离）==")
    mc = os.path.join(tmp, "minecraft")
    iso = os.path.join(mc, "versions", "1.20.1-fabric")
    noniso = os.path.join(mc, "versions", "1.20.1-vanilla")
    os.makedirs(os.path.join(iso, "mods"))
    os.makedirs(noniso)
    root, isolated = mm.client_paths(mc, "1.20.1-fabric")
    ok(isolated and root == iso, "版本隔离识别正确")
    root, isolated = mm.client_paths(mc, "1.20.1-vanilla")
    ok(not isolated and root == mc, "非隔离客户端回退到根目录")
    root, isolated = mm.client_paths(mc, "1.20.1-vanilla", force_isolated=True)
    ok(isolated and root == noniso, "显式直选版本目录时强制按隔离布局处理")
    ok(mm.resolve_version_dir(iso) == (mc, "1.20.1-fabric"), "识别 versions/<版本> 隔离目录")
    ok(mm.resolve_version_dir(mc) is None, ".minecraft 根目录不是版本目录")
    ok(mm.resolve_version_dir(os.path.join(mc, "versions")) is None, "versions 目录本身不是版本目录")
    # 加载器识别
    vj = os.path.join(iso, "1.20.1-fabric.json")
    with open(vj, "w", encoding="utf-8") as fh:
        fh.write('{"id": "1.20.1-fabric", "libraries": [{"name": "net.fabricmc:fabric-loader:0.15.10"}]}')
    ok(mm.detect_loader(mc, "1.20.1-fabric") == "fabric", "从版本 json 识别 fabric")
    nj = os.path.join(noniso, "1.20.1-vanilla.json")
    with open(nj, "w", encoding="utf-8") as fh:
        fh.write('{"id": "1.20.1-vanilla", "libraries": [{"name": "net.neoforged:neoforge:20.4.223"}]}')
    ok(mm.detect_loader(mc, "1.20.1-vanilla") == "neoforge", "neoforge 优先于 forge 识别")
    fg = os.path.join(tmp, "minecraft2", "versions", "forge180")
    os.makedirs(fg)
    with open(os.path.join(fg, "forge180.json"), "w", encoding="utf-8") as fh:
        fh.write('{"libraries": [{"name": "net.minecraftforge:forge:1.18-38.0.17"}]}')
    ok(mm.detect_loader(os.path.join(tmp, "minecraft2"), "forge180") == "forge", "识别 forge")

    print("== 5. 版本兼容判断 ==")
    ok(mm.ver_compatible("1.20.1", "1.20.1"), "精确一致")
    ok(mm.ver_compatible("1.20.1", "1.20"), "前缀兼容 (1.20.1 ⊃ 1.20)")
    ok(not mm.ver_compatible("1.21.1", "1.20.1"), "不兼容")

    print("== 6. wrapper 模组嵌套解析 ==")
    import io
    def make_jar_bytes(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zz:
            for name, content in entries.items():
                zz.writestr(name, content)
        return buf.getvalue()

    w = os.path.join(tmp, "wrapper_gca.jar")
    with zipfile.ZipFile(w, "w", zipfile.ZIP_DEFLATED) as zz:
        zz.writestr("fabric.mod.json", json.dumps(
            {"id": "gca_wrapper", "name": "gugle-carpet-addition-Wrapper", "version": "1.0.6"}))
        zz.writestr("META-INF/jars/gca-real.jar", make_jar_bytes(
            {"fabric.mod.json": json.dumps({"id": "gca", "name": "gugle-carpet-addition", "version": "2.12.6"})}))
        zz.writestr("META-INF/jars/gca-real-dup.jar", make_jar_bytes(
            {"fabric.mod.json": json.dumps({"id": "gca", "name": "gugle-carpet-addition", "version": "2.12.6"})}))
        zz.writestr("META-INF/jars/jep-2.24.jar", make_jar_bytes(
            {"fabric.mod.json": json.dumps({"id": "jep_jep", "name": "jep", "version": "2.24"})}))
    m = mm.parse_mod_jar(w)
    ok(m and m["id"] == "gca_wrapper" and m["name"] == "gugle-carpet-addition-Wrapper",
       "外层 wrapper 元数据: %s" % (m["id"] if m else None))
    ok(len(m.get("nested", [])) == 1 and m["nested"][0]["id"] == "gca",
       "wrapper 只保留一个代表内嵌 jar: %s" % [x["id"] for x in m.get("nested", [])])
    ok(m["nested"][0]["name"] == "gugle-carpet-addition", "内嵌 name 为真实模组名")
    ok(m["nested"][0].get("sha1"), "代表内嵌 jar 带字节 sha1")
    ok(mm.is_wrapper_meta(m), "结构性识别为 wrapper 模组")
    ok(mm.wrapper_base_id(m) == "gca", "wrapper 基底 id: gca")
    ok(not mm.is_wrapper_meta({"id": "sodium", "name": "Sodium"}), "普通模组不是 wrapper")
    # 纯 wrapper
    w2 = os.path.join(tmp, "pure_wrapper.jar")
    with zipfile.ZipFile(w2, "w", zipfile.ZIP_DEFLATED) as zz:
        zz.writestr("META-INF/jars/inner.jar", make_jar_bytes(
            {"fabric.mod.json": json.dumps({"id": "gca", "name": "gugle-carpet-addition"})}))
    m2 = mm.parse_mod_jar(w2)
    ok(m2 and m2["id"] == "gca", "纯 wrapper 直接用内嵌真实模组元数据")
    # jarjar
    j = os.path.join(tmp, "forge_with_jarjar.jar")
    with zipfile.ZipFile(j, "w", zipfile.ZIP_DEFLATED) as zz:
        zz.writestr("META-INF/mods.toml",
                    'modLoader="javafml"\n\n[[mods]]\nmodId="jei"\ndisplayName="Just Enough Items"\n')
        zz.writestr("META-INF/jarjar/mixin-extras.jar", make_jar_bytes(
            {"META-INF/mods.toml": 'modLoader="javafml"\n\n[[mods]]\nmodId="mixinextras"\ndisplayName="MixinExtras"\n'}))
    m3 = mm.parse_mod_jar(j)
    ok(m3["id"] == "jei" and len(m3.get("nested", [])) == 1
       and not mm.is_wrapper_meta(m3), "jarjar 内嵌库不误判为 wrapper")

    print("== 7. MC 版本号提取（Mojang 清单名）==")
    known = ["1.21.4", "1.21.1", "1.21", "1.20.6", "1.20.1", "1.20", "1.16.5"]
    ok(mm.base_mc_version("1.20.1-fabric", known) == "1.20.1", "1.20.1-fabric → 1.20.1")
    ok(mm.base_mc_version("1.21-fabric", known) == "1.21", "1.21-fabric → 1.21")
    ok(mm.base_mc_version("1.20.10-fabric", ["1.21", "1.20.10", "1.20.1"]) == "1.20.10",
       "1.20.10-fabric 不会被误判为 1.20.1")
    ok(mm.base_mc_version("22w43a") == "", "快照名无法提取 → 空串")
    ok(mm.base_mc_version("1.16.5-optifine") == "1.16.5", "正则兜底 1.16.5-optifine")
    ok(mm.base_mc_version("") == "", "空名返回空")
    # 从版本 json 自动读取目标版本
    ok(mm.detect_target_mc(mc, "1.20.1-fabric") == "1.20.1", "detect_target_mc 从版本 json 读出 1.20.1")
    ok(mm.detect_target_mc(mc, "no-such-version") == "", "不存在的版本返回空")

    print("== 8. sniff 服务端加载器（缓存）==")
    srv = os.path.join(tmp, "srv_cache")
    os.makedirs(os.path.join(srv, "mods"), exist_ok=True)
    make_jar(os.path.join(srv, "mods", "sodium_fake.jar"),
             {"fabric.mod.json": json.dumps({"id": "sodium", "name": "Sodium"})})
    ok(mm.sniff_server_loader(srv) == "fabric", "服务端加载器识别")
    ok(mm.sniff_server_loader(srv) == "fabric", "重复调用结果一致（缓存命中）")
    ok(mm.sniff_server_loader(os.path.join(tmp, "no_such_dir")) is None, "目录不存在返回 None")

    print("== 9. 作者提取与 fork 防护 ==")
    fa = os.path.join(tmp, "authors_fabric.jar")
    make_jar(fa, {"fabric.mod.json": json.dumps({"id": "m1", "name": "M1",
        "authors": [{"name": "Alice"}, "bob", {"name": "Charlie", "contact": {}}]})})
    ok(mm.parse_mod_jar(fa)["authors"] == ["Alice", "bob", "Charlie"],
       "fabric 作者提取(字典/字符串混合): %s" % mm.parse_mod_jar(fa)["authors"])
    fa2 = os.path.join(tmp, "authors_fabric2.jar")
    make_jar(fa2, {"fabric.mod.json": json.dumps({"id": "m2", "name": "M2", "authors": "SingleAuthor"})})
    ok(mm.parse_mod_jar(fa2)["authors"] == ["SingleAuthor"], "fabric 字符串作者")
    ta = os.path.join(tmp, "authors_toml.jar")
    make_jar(ta, {"META-INF/mods.toml":
                  'modLoader="javafml"\n[[mods]]\nmodId="m3"\ndisplayName="M3"\nauthors="Alice, Bob"\n'})
    ok(mm.parse_mod_jar(ta)["authors"] == ["Alice", "Bob"], "toml 作者逗号拆分")
    ok(mm.authors_equal(["Alice", "bob"], ["bob", "Alice"]), "顺序无关的严格一致")
    ok(not mm.authors_equal(["Alice"], ["EvilForker"]), "fork 作者不一致")
    ok(not mm.authors_equal(["Alice"], ["Alice", "Bob"]), "成员多一人也不一致")
    ok(not mm.authors_equal([], ["Alice"]), "空作者视为无法校验")

    print("== 10. 限速解析与并发下载器 ==")
    ok(mm.parse_retry_after("5") == 5, "Retry-After 秒数")
    ok(mm.parse_retry_after("9999") == 60, "Retry-After 上限 60 秒")
    ok(mm.parse_retry_after(None) == 0, "无 Retry-After 返回 0")
    ok(1 <= mm.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") <= 60, "HTTP 日期兜底")

    def fake_worker(file_info, dest_dir):
        time.sleep(0.15)
        p = os.path.join(dest_dir, file_info["name"])
        open(p, "w").write("x")
        return p, None

    dl_dir = os.path.join(tmp, "dl")
    os.makedirs(dl_dir)
    d = mm.Downloader(max_workers=4, worker=fake_worker)
    t0 = time.time()
    for i in range(3):
        d.submit("f%d" % i, {"name": "f%d.bin" % i}, dl_dir)
    res = d.gather()
    d.shutdown()
    elapsed = time.time() - t0
    ok(len(res) == 3 and all(r[3] for r in res), "3 个下载全部完成")
    ok(elapsed < 0.45, "并发生效（3×0.15s 任务实际耗时 %.2fs）" % elapsed)
    ok([r[0] for r in res] == ["f0", "f1", "f2"], "gather 按提交顺序返回")

    print("== 10.5 存档重名处理（加 _old）==")
    s_saves = os.path.join(tmp, "src_saves")
    d_saves = os.path.join(tmp, "dst_saves")
    for w in ("world1", "world2", "world1_old"):
        os.makedirs(os.path.join(s_saves, w), exist_ok=True)
        open(os.path.join(s_saves, w, "level.dat"), "w").write(w)
    os.makedirs(os.path.join(d_saves, "world1"), exist_ok=True)
    open(os.path.join(d_saves, "world1", "level.dat"), "w").write("old-target")
    os.makedirs(os.path.join(d_saves, "world1_old"), exist_ok=True)
    open(os.path.join(d_saves, "world1_old", "level.dat"), "w").write("old-target-2")
    n, r = mm.copy_saves_merge(s_saves, d_saves)
    ok(n == 3 and r == 2, "复制 3 项、重名改名 2 项: %s" % ((n, r),))
    ok(open(os.path.join(d_saves, "world1", "level.dat")).read() == "old-target",
       "目标旧世界 world1 未被覆盖")
    ok(open(os.path.join(d_saves, "world1_old", "level.dat")).read() == "old-target-2",
       "目标旧世界 world1_old 未被覆盖")
    ok(open(os.path.join(d_saves, "world1_old_old", "level.dat")).read() == "world1",
       "源 world1 复制为 world1_old_old（两级重名）")
    ok(open(os.path.join(d_saves, "world1_old_old_old", "level.dat")).read() == "world1_old",
       "源 world1_old 复制为 world1_old_old_old")
    ok(open(os.path.join(d_saves, "world2", "level.dat")).read() == "world2",
       "无重名 world2 正常复制")

    print("== 11. 下载源游戏版本最新版解包比对作者 ==")
    import mc_migrator.modrinth as _mr
    _saved = (_mr.mr_lookup_sha1, _mr.mr_search, _mr.mr_versions, _mr.mr_download_file,
              _mr.resolve_via_github)

    cdir = os.path.join(tmp, "cmp_dir")
    os.makedirs(cdir, exist_ok=True)
    vmatch_jar = os.path.join(tmp, "vmatch.jar")
    make_jar(vmatch_jar, {"fabric.mod.json": json.dumps(
        {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["Alice"]})})
    fork_jar = os.path.join(tmp, "vfork.jar")
    make_jar(fork_jar, {"fabric.mod.json": json.dumps(
        {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["EvilForker"]})})
    newest_authors = ["Alice"]

    def fake_sha1(_s):
        return None

    def fake_search(q, mc=None, loader=None, limit=10):
        return [{"project_id": "P1", "slug": "somemod", "title": "SomeMod", "id": "P1"}]

    def fake_versions(pid, mc=None, loader=None):
        if mc and loader:
            return []
        return [
            {"id": "v9", "version_number": "1.5.0", "date_published": "2026-02-01",
             "game_versions": ["1.20.1"], "loaders": ["fabric"],
             "files": [{"primary": True, "filename": "m-1.5.0.jar", "hashes": {"sha1": "X" * 40}}]},
            {"id": "v8", "version_number": "v1.4.0+build.7", "date_published": "2026-01-01",
             "game_versions": ["1.20.1"], "loaders": ["fabric"],
             "files": [{"primary": True, "filename": "m-1.4.0.jar", "hashes": {"sha1": "X" * 40}}]},
        ]

    def fake_download(file_info, dest_dir):
        vn = file_info["version_number"]
        p = os.path.join(cdir, "official-" + vn + ".jar")
        if "1.5.0" in vn:
            ver, authors = "1.5.0", list(newest_authors)
        else:
            ver, authors = "1.4.0", ["Alice"]
        make_jar(p, {"fabric.mod.json": json.dumps(
            {"id": "somemod", "name": "SomeMod", "version": ver, "authors": authors})})
        return p, None

    _mr.mr_lookup_sha1 = fake_sha1
    _mr.mr_search = fake_search
    _mr.mr_versions = fake_versions
    _mr.mr_download_file = fake_download
    _mr.resolve_via_github = lambda *a, **k: None
    _mr._versions_cache.clear()
    try:
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["Alice"]},
            "1.20.1", "fabric", vmatch_jar, src_mc_version="1.20.1", compare_dir=cdir)
        ok(pid == "P1" and "作者校验通过" in why,
           "解包最新版作者一致 → 通过: %s" % why)
        newest_authors = ["Alice", "Bob"]
        _mr._versions_cache.clear()
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["Alice"]},
            "1.20.1", "fabric", vmatch_jar, src_mc_version="1.20.1", compare_dir=cdir)
        ok(pid == "P1" and "原版文件" in why,
           "最新版作者变了，但同版本号文件 sha1 一致 → 通过: %s" % why)
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["EvilForker"]},
            "1.20.1", "fabric", fork_jar, src_mc_version="1.20.1", compare_dir=cdir)
        ok(pid is None and "疑似 fork" in why,
           "解包作者不一致且同版本号 sha1 不同 → 拒绝: %s" % why)
        nover_jar = os.path.join(tmp, "vnover.jar")
        make_jar(nover_jar, {"fabric.mod.json": json.dumps(
            {"id": "somemod", "name": "SomeMod", "authors": ["EvilForker"]})})
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "authors": ["EvilForker"]},
            "1.20.1", "fabric", nover_jar, src_mc_version="1.20.1", compare_dir=cdir)
        ok(pid is None and "疑似 fork" in why, "作者不一致且无版本号 → 拒绝（不漏过）")
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "9.9.9", "authors": []},
            "1.20.1", "fabric", vmatch_jar, src_mc_version="1.20.1", compare_dir=cdir)
        ok(pid == "P1" and "slug" in why, "无作者信息 → 按置信度通过: %s" % why)
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["EvilForker"]},
            "1.20.1", "fabric", fork_jar, src_mc_version="1.20.1", compare_dir=cdir,
            ignore_fork=True)
        ok(pid == "P1" and conf == 1.0 and "已按选项忽略" in why,
           "ignore_fork=True → fork 不拒绝，直接下载并提示: %s" % why)
        pid, conf, why, _mf = _mr.match_to_project(
            {"id": "somemod", "name": "SomeMod", "version": "1.4.0", "authors": ["Alice"]},
            "1.20.1", "fabric", vmatch_jar)
        ok(pid == "P1", "无 compare_dir → 跳过作者下载校验: %s" % why)
    finally:
        (_mr.mr_lookup_sha1, _mr.mr_search, _mr.mr_versions, _mr.mr_download_file,
         _mr.resolve_via_github) = _saved

    print("== 11.9 fork 版 GitHub release 兜底 ==")
    _gh_saved = (_mr.mr_get, _mr._gh_releases, _mr.mc_release_date, _mr.mr_download_file,
                 _mr.mr_lookup_sha1, _mr.mr_search, _mr.mr_versions)
    _mr.mr_lookup_sha1 = fake_sha1
    _mr.mr_search = fake_search
    _mr.mr_versions = fake_versions
    _mr.mc_release_date = lambda v: "2026-01-15T00:00:00Z"

    def gh_download(file_info, dest_dir):
        if "version_number" in file_info:
            vn = file_info["version_number"]
            p = os.path.join(dest_dir, "official-" + vn + ".jar")
            make_jar(p, {"fabric.mod.json": json.dumps(
                {"id": "somemod", "name": "SomeMod", "version": "1.5.0",
                 "authors": ["Alice", "Bob"]})})
            return p, None
        fn = file_info["files"][0]["filename"]
        p = os.path.join(dest_dir, fn)
        if "src" in fn:
            make_jar(p, {"fabric.mod.json": json.dumps(
                {"id": "gugle", "name": "Gugle", "version": "2.0.0",
                 "authors": ["EvilForker"], "depends": {"minecraft": ">=1.20 <=1.21"}}),
                "com/example/Mod.java": "class Mod {}"})
            return p, None
        if "26.2" in fn:
            mc_dep = "26.2"
        elif "1.21" in fn:
            mc_dep = ">=1.21"
        else:
            mc_dep = ">=1.20 <=1.21"
        make_jar(p, {"fabric.mod.json": json.dumps(
            {"id": "gugle", "name": "Gugle", "version": "2.0.0",
             "authors": ["EvilForker"], "depends": {"minecraft": mc_dep}})})
        return p, None

    _mr.mr_download_file = gh_download

    def gh_release(tag, assets, pub="2026-07-01T00:00:00Z", title="", body=""):
        return {"tag_name": tag, "name": title, "body": body, "published_at": pub,
                "assets": [{"name": n,
                            "browser_download_url": "https://github.com/x/y/releases/download/%s/%s" % (tag, n)}
                           for n in assets]}

    gh_jar = os.path.join(tmp, "ghfork.jar")
    make_jar(gh_jar, {"fabric.mod.json": json.dumps({
        "id": "somemod", "name": "SomeMod", "version": "1.4.0",
        "authors": ["EvilForker"],
        "contact": {"sources": "https://github.com/evil/somemod-fork"}})})
    gh_meta = {"id": "somemod", "name": "SomeMod", "version": "1.4.0",
               "authors": ["EvilForker"],
               "contact": {"sources": "https://github.com/evil/somemod-fork"}}

    _mr._gh_releases = lambda repo: [gh_release("v2", ["gugle-1.21.jar", "gugle-1.20.1.jar"])]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "1.20.1", "fabric", gh_jar,
                                              src_mc_version="1.20.1", compare_dir=cdir)
    ok(pid == "github:evil/somemod-fork" and conf == 1.0
       and mf and os.path.basename(mf) == "gugle-1.20.1.jar" and "GitHub" in why,
       "fork 版经 GitHub release 兜底下载并验证适配: %s" % why)

    _mr._gh_releases = lambda repo: [gh_release("v1", ["gugle-1.20.1.jar"],
                                                pub="2025-06-01T00:00:00Z")]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "1.20.1", "fabric", gh_jar,
                                              src_mc_version="1.20.1", compare_dir=cdir)
    ok(pid is None and "疑似 fork" in why and mf is None,
       "release 发布于 MC 版本发布之前 → 不采用仍拒绝: %s" % why)

    _mr._gh_releases = lambda repo: [gh_release("v4", ["gugle-latest.jar"])]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "1.20.1", "fabric", gh_jar,
                                              src_mc_version="1.20.1", compare_dir=cdir)
    ok(pid == "github:evil/somemod-fork" and "仅含一个 jar 文件" in why
       and mf and os.path.basename(mf) == "gugle-latest.jar",
       "release 仅一个 jar → 直接下载并解包验证: %s" % why)

    _mr._gh_releases = lambda repo: [gh_release("v5", ["gugle-1.21.jar"])]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "1.20.1", "fabric", gh_jar,
                                              src_mc_version="1.20.1", compare_dir=cdir)
    ok(pid is None and "疑似 fork" in why and mf is None,
       "下载解包后不适配目标版本 → 拒绝: %s" % why)

    _mr.mr_versions = lambda *a, **k: []
    _mr._gh_releases = lambda repo: [gh_release("v12", ["subtick-mc26.2-v2.4.jar"])]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "26.2", "fabric", gh_jar,
                                              src_mc_version="1.21", compare_dir=cdir)
    ok(pid == "github:evil/somemod-fork" and conf == 1.0 and mf is not None,
       "项目无源版本可校验作者（cand=None）仍走 GitHub 兜底: %s" % why)
    _mr.mr_versions = fake_versions

    _mr._gh_releases = lambda repo: [gh_release("v11", ["gugle-src.jar"])]
    _mr._versions_cache.clear()
    pid, conf, why, mf = _mr.match_to_project(gh_meta, "1.20.1", "fabric", gh_jar,
                                              src_mc_version="1.20.1", compare_dir=cdir)
    ok(pid is None and "疑似 fork" in why and mf is None,
       "仅含源码的 jar（无 class 文件）→ 拒绝: %s" % why)

    rel_multi = gh_release("v6", ["gugle-mc1.20.1.jar", "gugle-mc1.21.jar"])
    a, why6 = _mr._pick_gh_asset(rel_multi, "1.20.1")
    ok(a and a["name"] == "gugle-mc1.20.1.jar" and "文件包含目标版本" in why6,
       "多 jar 按文件名包含目标版本选择: %s" % why6)
    rel_src = gh_release("v10", ["gugle-1.20.1-sources.jar", "gugle-1.20.1.jar"])
    a, why10 = _mr._pick_gh_asset(rel_src, "1.20.1")
    ok(a and a["name"] == "gugle-1.20.1.jar" and not a["name"].endswith("sources.jar"),
       "多 jar 时排除 -sources 源码包（%s）: %s" % (a and a["name"], why10))
    rel_body = gh_release("v7", ["gugle-a.jar", "gugle-b.jar"], title="", body="支持 1.20.1")
    a, why7 = _mr._pick_gh_asset(rel_body, "1.20.1")
    ok(a and a["name"] == "gugle-a.jar" and "标题/说明" in why7,
       "多 jar 时按 release 标题/说明包含目标版本选择: %s" % why7)
    rel_none = gh_release("v8", ["gugle-a.jar", "gugle-b.jar"])
    ok(_mr._pick_gh_asset(rel_none, "1.20.1")[0] is None,
       "多 jar 且无目标版本信息 → 不选")
    rel_single = gh_release("v9", ["gugle-one.jar"])
    a, why9 = _mr._pick_gh_asset(rel_single, "1.20.1")
    ok(a and a["name"] == "gugle-one.jar" and "仅含一个 jar" in why9,
       "单 jar 规则: %s" % why9)

    ok(_mr._github_repo({"contact": {"issues": "https://github.com/a/b/issues"}}, None) == "a/b",
       "contact.issues 识别仓库 a/b")
    ok(_mr._github_repo({"contact": {"sources": "https://github.com/a/repo.git"}}, None) == "a/repo",
       "sources 识别仓库（去除 .git）")
    _mr.mr_get = lambda p: {"id": p, "source_url": "https://github.com/forkowner/forkrepo"}
    ok(_mr._github_repo({"id": "x"}, "P9") == "forkowner/forkrepo",
       "无 contact 时回退匹配项目的 source_url")
    _mr.mr_get = lambda p: {"id": p}
    ok(_mr._github_repo({"id": "x"}, "P9") is None, "项目无 source_url → 无仓库")
    ok(_mr._github_repo({"id": "x"}, None) is None, "无任何线索 → 无仓库")

    class _FakeResp:
        def __init__(self, text, status=200):
            self.text = text
            self.status_code = status

        def raise_for_status(self):
            if self.status_code != 200:
                raise ValueError("bad status")

    _get_saved = _mr._SESSION.get
    _mr._SESSION.get = lambda url, **kw: (
        _FakeResp("<feed><entry><id>tag:github.com,2008:Repository/1/v2.0</id>"
                  "<title>v2.0: fix</title><updated>2026-07-01T00:00:00Z</updated></entry>"
                  "<entry><id>tag:github.com,2008:Repository/1/v1.0</id>"
                  "<title>v1.0</title><updated>2026-06-01T00:00:00Z</updated></entry></feed>")
        if "releases.atom" in url
        else (_FakeResp('<a href="/o/r/releases/download/v2.0/mod-1.20.1.jar">m</a>'
                        '<a href="/o/r/releases/download/v2.0/mod-1.21.jar">m</a>')
              if url.endswith("v2.0")
              else _FakeResp('<a href="/o/r/releases/download/v1.0/mod-1.20.1.jar">m</a>'
                             '<a href="/o/r/releases/download/v1.0/readme.txt">m</a>')))
    rels = _mr._gh_releases_html("o/r")
    ok(len(rels) == 2 and rels[0]["tag_name"] == "v2.0"
       and rels[0]["published_at"] == "2026-07-01T00:00:00Z"
       and [a["name"] for a in rels[0]["assets"]] == ["mod-1.20.1.jar", "mod-1.21.jar"]
       and rels[1]["assets"][0]["browser_download_url"]
       == "https://github.com/o/r/releases/download/v1.0/mod-1.20.1.jar",
       "HTML 兜底解析 release 列表（tag 取自 id、日期、仅 jar 资源）")
    _mr._SESSION.get = _get_saved
    (_mr.mr_get, _mr._gh_releases, _mr.mc_release_date, _mr.mr_download_file,
     _mr.mr_lookup_sha1, _mr.mr_search, _mr.mr_versions) = _gh_saved

    print("== 11.8 依赖/冲突解析 ==")
    rel = os.path.join(tmp, "relations.jar")
    make_jar(rel, {"fabric.mod.json": json.dumps({
        "id": "amod", "name": "AMod", "version": "1.0",
        "depends": {"bmod": ">=1.0", "minecraft": "1.20.1", "fabricloader": "*"},
        "conflicts": {"cmod": "<2.0"},
        "breaks": {"dmod": "<=1.5", "emod": "*"}})})
    m = mm.parse_mod_jar(rel)
    ok(("bmod", ">=1.0") in m["deps"] and ("minecraft", "1.20.1") in m["deps"],
       "fabric depends 解析: %s" % m["deps"])
    ok(("cmod", "<2.0") in m["conflicts"], "fabric conflicts 解析: %s" % m["conflicts"])
    ok(("dmod", "<=1.5") in m["conflicts"] and ("emod", "*") in m["conflicts"],
       "fabric breaks 字段并入冲突: %s" % m["conflicts"])
    rel2 = os.path.join(tmp, "relations_list.jar")
    make_jar(rel2, {"fabric.mod.json": json.dumps({
        "id": "iris", "name": "Iris", "version": "1.11.2+mc26.2",
        "depends": {"sodium": ["0.9.x"], "minecraft": "~26.2"}})})
    m2 = mm.parse_mod_jar(rel2)
    ok(("sodium", "0.9.x") in m2["deps"], "depends 数组值解析: %s" % m2["deps"])
    g9 = mm.ModGraph()
    g9.add_mod("iris", "Iris", "1.11.2+mc26.2", "iris.jar", "PI",
               m2["deps"], m2["conflicts"])
    g9.add_mod("sodium", "Sodium", "0.9.2-alpha.4+mc26.2", "sodium.jar", "PS",
               [], [("iris", "<=1.11.2")])
    ok(g9.dependents.get("sodium") == {"iris"}, "iris 依赖 sodium（0.9.x）")
    rep9 = g9.conflict_report()
    ok(len(rep9) == 1 and rep9[0]["mod"] == "iris" and "sodium" in rep9[0]["conflicting"],
       "sodium breaks iris<=1.11.2 → 冲突识别: %s" % rep9)
    toml_rel = os.path.join(tmp, "toml_rel.jar")
    make_jar(toml_rel, {"META-INF/mods.toml":
                        'modLoader="javafml"\n[[mods]]\nmodId="xmod"\ndisplayName="XMod"\n'
                        '[[dependencies.xmod]]\nmodId="ymod"\nmandatory=true\nversionRange="[1.0,2.0)"\n'
                        '[[dependencies.xmod]]\nmodId="zmod"\nmandatory=false\n'})
    m = mm.parse_mod_jar(toml_rel)
    ok(("ymod", "[1.0,2.0)") in m["deps"] and ("zmod", "*") not in m["deps"],
       "toml 依赖解析（mandatory 过滤）: %s" % m["deps"])

    print("== 11.9 版本约束匹配 ==")
    ok(mm.version_satisfies("1.2.3", ">=1.0"), ">= 满足")
    ok(mm.version_satisfies("1.0.0", "1.0.x"), "x 通配符")
    ok(not mm.version_satisfies("1.1.0", "1.0.x"), "x 通配符不满足")
    ok(mm.version_satisfies("2.5", "<3.0"), "< 满足")
    ok(mm.version_satisfies("1.0", "*"), "* 全通过")
    ok(not mm.version_satisfies("0.9", ">=1.0"), ">= 不满足")
    ok(mm.version_satisfies("1.20.1", "1.20.1"), "精确相等")
    ok(mm.version_satisfies("1.5", "[1.0,2.0)"), "Forge 区间 [1.0,2.0) 满足")
    ok(not mm.version_satisfies("2.0", "[1.0,2.0)"), "Forge 区间右开不满足")
    ok(mm.version_satisfies("0.9.2-alpha.4+mc26.2", ">=0.9.1"),
       "预发布+构建元数据：0.9.2-alpha.4 满足 >=0.9.1")
    ok(not mm.version_satisfies("0.9.2-alpha.4+mc26.2", ">=0.9.2"),
       "预发布 < 同核心正式版：不满足 >=0.9.2")
    ok(mm.version_satisfies("0.9.2", ">=0.9.2-alpha.4"), "正式版满足 >= 同版本预发布")
    ok(mm.version_satisfies("7.4.5+7590-b8dc4c1", ">7.4.4 || <7.4.4"),
       "|| 或语义：7.4.5 命中 >7.4.4")
    ok(not mm.version_satisfies("7.4.4", ">7.4.4 || <7.4.4"), "|| 或语义：恰好 7.4.4 不命中")
    ok(mm.version_satisfies("1.0.0+mc26.2", "1.0.0"), "构建元数据不参与比较")
    ok(mm.version_satisfies("1.0.0-rc1", ">=1.0.0-alpha.1"), "rc > alpha")
    ok(mm.version_satisfies("0.9.2-alpha.4+mc26.2", ">=0.9.1 <0.10-"),
       "fabric 区间 [0.9.1, 0.10-) 对 0.9.2-alpha.4 判定")
    ok(not mm.version_satisfies("0.10-beta.2", "<0.10-"), "fabric: <0.10- 排除 0.10 预发布")
    ok(mm.version_satisfies("1.4-beta.2", "<1.4"), "fabric: <1.4 包含 1.4 预发布")
    ok(not mm.version_satisfies("1.4-beta.2", "<1.4-"), "fabric: <1.4- 排除 1.4 预发布")
    ok(mm.version_satisfies("1.4-beta.2", ">=1.4-"), "fabric: >=1.4- 包含 1.4 预发布")
    ok(mm.version_satisfies("1.4", ">=1.4-"), "fabric: >=1.4- 包含 1.4")
    ok(not mm.version_satisfies("1.3", ">=1.4-"), "fabric: >=1.4- 排除 1.3")
    ok(mm.version_satisfies("1.3.0-alpha.1", "1.3.x"), "fabric x 区间含预发布")
    ok(not mm.version_satisfies("1.4.0", "1.3.x"), "fabric x 区间排除下一版本")
    ok(mm.version_satisfies("2.9.0-beta.2", "2.*"), "2.* 通配")
    ok(mm.version_satisfies("1.2.1-alpha.3", "~1.2"), "fabric ~1.2 含 1.2.x 预发布")
    ok(not mm.version_satisfies("1.2.0-rc.2", "~1.2"), "fabric ~1.2 排除下界预发布")
    ok(mm.version_satisfies("1.2.0-rc.2", "~1.2-"), "fabric ~1.2- 包含下界预发布")
    ok(not mm.version_satisfies("1.3.0", "~1.2"), "fabric ~1.2 排除 1.3")
    ok(mm.version_satisfies("1.3.0", "^1.2.3"), "fabric ^1.2.3 含同大版本")
    ok(not mm.version_satisfies("1.2.3-beta.2", "^1.2.3"), "fabric ^1.2.3 排除下界预发布")
    ok(mm.version_satisfies("0.3.0", "^0.2.3"), "fabric ^0.2.3 含 0.3.0")
    ok(not mm.version_satisfies("1.21-4.6", "<=1.21-4.5"), "预发布逐段比较：4.6 > 4.5 不命中")
    ok(mm.version_satisfies("1.21-4.5", "<=1.21-4.5"), "预发布逐段比较：4.5 命中")
    ok(mm.version_satisfies("0.3.1-beta.9", ">=0.3.1-beta.8.d.10"),
       "预发布逐段比较：beta.9 > beta.8.d.10")
    ok(not mm.version_satisfies("0.3.1-beta.8.8", ">=0.3.1-beta.8.d.10"),
       "预发布逐段比较：数字 8 < 字母 d")
    ok(not mm.version_satisfies("0.3.1-beta.8.d", ">=0.3.1-beta.8.d.10"),
       "预发布逐段比较：短前缀不满足")

    print("== 11.10 依赖/冲突图 ==")
    g = mm.ModGraph()
    g.add_mod("amod", "AMod", "1.0", "a.jar", "PA", [("bmod", ">=1.0"), ("minecraft", "*")], [])
    g.add_mod("bmod", "BMod", "1.5", "b.jar", "PB", [], [])
    g.add_mod("cmod", "CMod", "2.0", "c.jar", "PC", [], [("bmod", "*")])
    ok(g.dependents.get("bmod") == {"amod"}, "bmod 被 amod 依赖")
    ok(g.conflicting.get("bmod") == {"cmod"}, "bmod 被 cmod 冲突")
    rep = g.conflict_report()
    ok(len(rep) == 1 and rep[0]["mod"] == "bmod" and rep[0]["dependents"] == ["amod"]
       and rep[0]["conflicting"] == ["cmod"], "冲突报告: %s" % rep)
    g2 = mm.ModGraph()
    g2.add_mod("amod", "AMod", "1.0", "a.jar", "PA", [("bmod", ">=2.0")], [])
    g2.add_mod("bmod", "BMod", "1.5", "b.jar", "PB", [], [])
    mis = g2.mismatches()
    ok(len(mis) == 1 and mis[0][:2] == ("amod", "bmod"), "版本不匹配记录: %s" % mis)
    g3 = mm.ModGraph()
    g3.add_mod("amod", "AMod", "1.0", "a.jar", "PA", [("minecraft", "*"), ("fabricloader", "*")], [])
    ok(not g3.dep_reqs.get("amod"), "默认依赖被排除")

    print("== 11.10.5 冲突处理动作 ==")
    import mc_migrator.migrator as _mig
    ddir = os.path.join(tmp, "conf_del")
    os.makedirs(ddir)
    for fn in ("a.jar", "b.jar", "c.jar"):
        open(os.path.join(ddir, fn), "w").write("x")

    class _Cfg:
        def __init__(self, action):
            self.action = action
            self.log = mm.Logger(lambda m, l="info": None)

        def on_conflicts(self, conflicts):
            return self.action

    def build_graph():
        for fn in ("a.jar", "b.jar", "c.jar"):
            open(os.path.join(ddir, fn), "w").write("x")
        g = mm.ModGraph()
        g.add_mod("amod", "AMod", "1.0", os.path.join(ddir, "a.jar"), "PA", [("bmod", "*")], [])
        g.add_mod("bmod", "BMod", "1.5", os.path.join(ddir, "b.jar"), "PB", [], [])
        g.add_mod("cmod", "CMod", "2.0", os.path.join(ddir, "c.jar"), "PC", [], [("bmod", "*")])
        return g

    g4 = build_graph()
    removed = _mig.resolve_conflicts(g4, _Cfg("delete_c"))
    ok(sorted(removed) == ["a.jar", "b.jar"]
       and not os.path.exists(os.path.join(ddir, "b.jar"))
       and os.path.exists(os.path.join(ddir, "c.jar")),
       "delete_c 删除模组及依赖它的模组: %s" % removed)
    g5 = build_graph()
    removed = _mig.resolve_conflicts(g5, _Cfg("delete_conflicts"))
    ok(sorted(removed) == ["c.jar"] and os.path.exists(os.path.join(ddir, "b.jar")),
       "delete_conflicts 只删冲突模组: %s" % removed)
    g6 = build_graph()
    removed = _mig.resolve_conflicts(g6, _Cfg("skip"))
    ok(removed == [] and os.path.exists(os.path.join(ddir, "a.jar")), "skip 不删除")

    print("== 11.10.6 冲突区间评估（已安装版本）==")
    g7 = mm.ModGraph()
    g7.add_mod("techutils", "Technical Utilities", "0.7.1", "t.jar", "PT",
               [], [("worldedit", "<7.4.4 || >7.4.4")])
    g7.add_mod("worldedit", "WorldEdit", "7.4.5+7590-b8dc4c1", "w.jar", "PW", [], [])
    rep7 = g7.conflict_report()
    ok(len(rep7) == 1 and rep7[0]["mod"] == "worldedit"
       and rep7[0]["conflicting"] == ["techutils"],
       "已安装版本落在冲突区间 → 报告: %s" % rep7)
    g8 = mm.ModGraph()
    g8.add_mod("techutils", "Technical Utilities", "0.7.1", "t.jar", "PT",
               [], [("worldedit", "<7.4.4 || >7.4.4")])
    g8.add_mod("worldedit", "WorldEdit", "7.4.4", "w.jar", "PW", [], [])
    ok(not g8.conflict_report(), "版本恰好 7.4.4 不在冲突区间 → 不报告")
    g9 = mm.ModGraph()
    g9.add_mod("a", "A", "1.0", "a.jar", "PA", [], [("x", "*")])
    g9.add_mod("b", "B", "1.0", "b.jar", "PB", [], [("x", "*")])
    g9.add_mod("c", "C", "1.0", "c.jar", "PC", [], [("x", ">9")])
    g9.add_mod("x", "X", "2.0", "x.jar", "PX", [], [])
    rep9 = g9.conflict_report()
    ok(len(rep9) == 1 and rep9[0]["mod"] == "x"
       and rep9[0]["conflicting"] == ["a", "b"],
       "同一目标多模组冲突合并为一条: %s" % rep9)

    print("== 11.10.7 单方面冲突自动换版本 ==")
    import mc_migrator.migrator as _mig3
    _saved3 = (_mig3.mr_versions, _mig3.mr_download_file)
    tdir3 = os.path.join(tmp, "auto_dir")
    os.makedirs(tdir3, exist_ok=True)
    old_file = os.path.join(tdir3, "worldedit-7.4.5.jar")
    open(old_file, "w").write("old")
    g10 = mm.ModGraph()
    g10.add_mod("techutils", "Technical Utilities", "0.7.1", "t.jar", "PT",
                [], [("worldedit", ">7.4.4 || <7.4.4")])
    g10.add_mod("worldedit", "WorldEdit", "7.4.5", old_file, "PW", [], [])

    def fake_versions(pid, mc=None, loader=None):
        return [
            {"id": "v3", "version_number": "7.4.5", "date_published": "2026-03-01",
             "game_versions": ["1.20.1"], "loaders": ["fabric"]},
            {"id": "v2", "version_number": "7.4.4", "date_published": "2026-02-01",
             "game_versions": ["1.20.1"], "loaders": ["fabric"]},
        ]

    def fake_download(file_info, dest_dir):
        vn = file_info["version_number"]
        p = os.path.join(tdir3, "worldedit-" + vn + ".jar")
        make_jar(p, {"fabric.mod.json": json.dumps(
            {"id": "worldedit", "name": "WorldEdit", "version": vn})})
        return p, None

    _mig3.mr_versions = fake_versions
    _mig3.mr_download_file = fake_download

    class _ACfg:
        log = mm.Logger(lambda m, l="info": None)
        confirm = lambda p: True

    try:
        rep10 = {"ok": [("WorldEdit", "worldedit-7.4.5.jar")]}
        _mig3.auto_resolve_conflicts(g10, "1.20.1", "fabric", tdir3, _ACfg(), rep10)
        ok(g10.mods["worldedit"]["version"] == "7.4.4"
           and not os.path.exists(old_file)
           and os.path.exists(os.path.join(tdir3, "worldedit-7.4.4.jar"))
           and any(f == "worldedit-7.4.4.jar" for _n, f in rep10["ok"]),
           "单方面冲突自动换成不冲突版本: %s" % g10.mods["worldedit"]["version"])
        ok(_mig3.resolve_conflicts(g10, _ACfg()) == [],
           "无人依赖目标 → 不弹窗: %s" % g10.conflict_report())
    finally:
        _mig3.mr_versions, _mig3.mr_download_file = _saved3

    print("== 11.10.8 目标无解时换成兼容的 breaker（sodium/iris 场景）==")
    import mc_migrator.migrator as _mig4
    _saved4 = (_mig4.mr_versions, _mig4.mr_download_file)
    tdir4 = os.path.join(tmp, "breaker_dir")
    os.makedirs(tdir4, exist_ok=True)
    sodium_file = os.path.join(tdir4, "sodium-a4.jar")
    open(sodium_file, "w").write("old")
    iris_file = os.path.join(tdir4, "iris-1.11.2.jar")
    open(iris_file, "w").write("iris")
    fapi_file = os.path.join(tdir4, "fabric-api.jar")
    open(fapi_file, "w").write("fapi")
    g11 = mm.ModGraph()
    g11.add_mod("iris", "Iris", "1.11.2+mc26.2", iris_file, "PIRIS", [("sodium", "0.9.x")], [])
    g11.add_mod("fabric-api", "Fabric API", "0.116.0", fapi_file, "PFAPI", [], [])
    g11.add_mod("sodium", "Sodium", "0.9.2-alpha.4+mc26.2", sodium_file, "PSODIUM",
                [], [("iris", "<=1.11.2"), ("fabric-api", "<0.145.1")])

    def fv(pid, mc=None, loader=None):
        if pid == "PIRIS":
            return [{"id": "v1", "version_number": "1.11.2+mc26.2", "date_published": "2026-07-08",
                     "game_versions": ["26.2"], "loaders": ["fabric"]}]
        if pid == "PFAPI":
            return []
        return [
            {"id": "v4", "version_number": "0.9.2-alpha.4+mc26.2", "date_published": "2026-08-07",
             "game_versions": ["26.2"], "loaders": ["fabric"]},
            {"id": "v3", "version_number": "0.9.1+mc26.2", "date_published": "2026-07-08",
             "game_versions": ["26.2"], "loaders": ["fabric"]},
        ]

    def fd(file_info, dest_dir):
        vn = file_info["version_number"]
        meta = {"id": "sodium" if "sodium" in str(file_info.get("project_id")) else "iris",
                "name": "Sodium", "version": vn, "authors": ["JellySquid"]}
        if "0.9.1" in vn:
            meta["breaks"] = {"iris": "<=1.11.1", "fabric-api": "<0.145.1"}
        elif "0.9.2" in vn:
            meta["breaks"] = {"iris": "<=1.11.2", "fabric-api": "<0.145.1"}
        p = os.path.join(tdir4, "dl-" + vn + ".jar")
        make_jar(p, {"fabric.mod.json": json.dumps(meta)})
        return p, None

    _mig4.mr_versions = fv
    _mig4.mr_download_file = fd
    try:
        rep11 = {"ok": [("Sodium", "sodium-a4.jar"), ("Iris", "iris-1.11.2.jar")]}
        _mig4.auto_resolve_conflicts(g11, "26.2", "fabric", tdir4, _ACfg(), rep11)
        ok(g11.mods["sodium"]["version"] == "0.9.1+mc26.2"
           and g11.mods["iris"]["version"] == "1.11.2+mc26.2"
           and not os.path.exists(sodium_file)
           and os.path.exists(iris_file),
           "目标无解时 breaker 换成兼容版本（既有 fabric-api break 不拦路）: %s"
           % g11.mods["sodium"]["version"])
        ok("iris" not in [r["mod"] for r in g11.conflict_report()],
           "iris/sodium 冲突消解: %s" % g11.conflict_report())
        ok(any(f == "dl-0.9.1+mc26.2.jar" for _n, f in rep11["ok"]),
           "报告文件已更新: %s" % rep11["ok"])
    finally:
        _mig4.mr_versions, _mig4.mr_download_file = _saved4

    print("== 11.11 带版本约束的版本挑选 ==")
    import mc_migrator.modrinth as _mr2
    _orig_v2 = _mr2.mr_versions
    _mr2.mr_versions = lambda pid, mc=None, loader=None: [
        {"id": "v3", "version_number": "2.0.0", "date_published": "2026-03-01",
         "game_versions": ["1.20.1"], "loaders": ["fabric"]},
        {"id": "v2", "version_number": "1.5.0", "date_published": "2026-02-01",
         "game_versions": ["1.20.1"], "loaders": ["fabric"]},
        {"id": "v1", "version_number": "1.0.0", "date_published": "2026-01-01",
         "game_versions": ["1.20.1"], "loaders": ["fabric"]},
    ]
    try:
        v = _mr2.pick_version_in_range("P", "1.20.1", "fabric", [">=2.0"])
        ok(v and v["version_number"] == "2.0.0", "范围 >=2.0 选 2.0.0")
        v = _mr2.pick_version_in_range("P", "1.20.1", "fabric", [">=1.0", "<2.0"])
        ok(v and v["version_number"] == "1.5.0", "范围 [>=1.0,<2.0) 选 1.5.0")
        v = _mr2.pick_version_in_range("P", "1.20.1", "fabric", [">=3.0"])
        ok(v is None, "无满足范围的版本 → None")
    finally:
        _mr2.mr_versions = _orig_v2

    print("== 11.12 mcmod 辅助解析 ==")
    import base64
    import mc_migrator.modrinth as _mr3
    ok(_mr3._modrinth_id_from_url("https://modrinth.com/mod/sodium") == "sodium",
       "modrinth /mod/ 链接提取 slug")
    ok(_mr3._modrinth_id_from_url("https://modrinth.com/project/AANobbMI") == "AANobbMI",
       "modrinth /project/ 链接提取 id")
    ok(_mr3._modrinth_id_from_url("https://modrinth.com/mod/sodium?x=1") == "sodium",
       "带查询参数仍可提取")
    ok(_mr3._modrinth_id_from_url("") is None, "空链接返回 None")
    ok(base64.b64decode("aHR0cHM6Ly9tb2RyaW50aC5jb20vbW9kL3NvZGl1bQ==").decode()
       == "https://modrinth.com/mod/sodium", "mcmod 跳转 base64 解码")

    print("== 11.13 大目录延迟迁移（超过阈值留到最后询问）==")
    import mc_migrator.migrator as _mig2
    _orig_thr = _mig2.BIG_FOLDER_THRESHOLD
    _mig2.BIG_FOLDER_THRESHOLD = 10
    sbig = os.path.join(tmp, "big_src")
    dbig = os.path.join(tmp, "big_dst")
    dbig2 = os.path.join(tmp, "big_dst2")
    os.makedirs(os.path.join(sbig, "saves", "world_big"))
    open(os.path.join(sbig, "saves", "world_big", "level.dat"), "w").write("x" * 100)

    class _BigCfg:
        def __init__(self, confirm=True):
            self.log = mm.Logger(lambda m, l="info": None)
            self.confirm = lambda p: confirm
            self.choices = {"config": True, "options": True, "saves": True,
                            "stray": True, "optional": True, "server": False}
            self.pending_big = []
            self.migrated_dirs = []

    try:
        bcfg = _BigCfg()
        _mig2.migrate_game_data(sbig, dbig, bcfg)
        ok(len(bcfg.pending_big) == 1 and bcfg.pending_big[0][0] == "saves",
           "超过阈值的大目录进入延迟队列: %s" % [p[0] for p in bcfg.pending_big])
        ok(not os.path.exists(os.path.join(dbig, "saves")), "大目录未立即复制")
        _mig2.migrate_big_folders(bcfg)
        ok(os.path.exists(os.path.join(dbig, "saves", "world_big", "level.dat")),
           "末尾确认后迁移完成")
        bcfg2 = _BigCfg(confirm=False)
        _mig2.migrate_game_data(sbig, dbig2, bcfg2)
        _mig2.migrate_big_folders(bcfg2)
        ok(not os.path.exists(os.path.join(dbig2, "saves")), "拒绝后不迁移")
    finally:
        _mig2.BIG_FOLDER_THRESHOLD = _orig_thr

    print("== 12. 版本挑选：无适配目标版本绝不下载 ==")
    import mc_migrator.modrinth as _mr
    _orig_versions = _mr.mr_versions

    def fake_versions(pid, mc_version=None, loader=None):
        if mc_version and loader:
            return []
        return [
            {"id": "v3", "version_number": "1.2.0", "date_published": "2026-03-01",
             "game_versions": ["1.20"], "loaders": ["fabric"], "version_type": "release"},
            {"id": "v2", "version_number": "1.1.0", "date_published": "2026-02-01",
             "game_versions": ["26.2"], "loaders": ["fabric"], "version_type": "release"},
            {"id": "v1", "version_number": "1.0.0", "date_published": "2026-01-01",
             "game_versions": ["1.21"], "loaders": ["fabric"], "version_type": "release"},
        ]

    _mr.mr_versions = fake_versions
    try:
        ver, warn = _mr.pick_version("p", "26.2", "fabric")
        ok(ver and ver["version_number"] == "1.1.0", "目标 26.2 精确命中最新版本")
        ver, warn = _mr.pick_version("p", "1.20.1", "fabric")
        ok(ver and ver["version_number"] == "1.2.0" and warn, "1.20 补丁兼容 1.20.1（带警告）")
        ver, warn = _mr.pick_version("p", "27.0", "fabric")
        ok(ver is None and warn is None, "目标 27.0 无适配版本 → 不下载（进手动清单）")
        ver, warn = _mr.pick_version("p", "26.2", "forge")
        ok(ver is None, "加载器不一致（只有 fabric 版）→ 不下载")
    finally:
        _mr.mr_versions = _orig_versions

    print("\n全部通过: %d 项断言" % PASS)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
