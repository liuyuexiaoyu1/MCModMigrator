import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .core import (ASK_CONF, CHOICE_KEYS, HIGH_CONF, KNOWN_DIRS, KNOWN_FILES,
                   LOADER_LABEL, OPTIONAL_DIRS, OPTIONAL_FILES, SERVER_FILES,
                   Logger, PROXY_SETTINGS, human_size, plain_log_sink)
from .clients import client_paths, detect_loader, is_server_root, sniff_server_loader
from .mod_parser import parse_mod_jar
from .modrinth import (Downloader, collect_deps, configure_http,
                       match_to_project, mr_download_file, pick_version,
                       pick_version_in_range, primary_filename)


def _make_dep_logger(base, report):
    def sink(msg, level):
        getattr(base, level)(msg)
        report["deps"].append(msg)
    return Logger(sink)


class RunConfig:
    def __init__(self, auto_yes=False, skip_deps=False, choices=None,
                 use_system_proxy=True, download_threads=4, analysis_threads=4,
                 print_failures=True, ignore_fork=False, log=None, confirm=None):
        self.auto_yes = auto_yes
        self.skip_deps = skip_deps
        self.use_system_proxy = use_system_proxy
        self.download_threads = max(1, min(int(download_threads or 4), 16))
        self.analysis_threads = max(1, min(int(analysis_threads or 8), 16))
        self.print_failures = print_failures
        self.ignore_fork = ignore_fork
        self.on_conflicts = None
        self.choices = choices or {k: True for k in CHOICE_KEYS}
        self.log = log or Logger(plain_log_sink)
        self.confirm = confirm or (lambda p: True)

def _match_one(i, jname, meta, jpath, mc_version, src_mc_version, loader, cfg, stop_event,
               target_mods_dir):
    if stop_event is not None and stop_event.is_set():
        return ("stopped",)
    if not meta:
        return ("skip", "无法解析该 jar（可能不是模组或文件损坏），跳过")
    if meta.get("library"):
        return ("skip", "%s 是库文件（library=true），跳过" % meta.get("id"))
    cfg.log.info("正在查询 Modrinth 匹配 %s ..." % (meta.get("name") or meta.get("id")))
    project_id, confidence, reason, matched_file = match_to_project(
        meta, mc_version, loader, jpath, ignore_fork=cfg.ignore_fork,
        src_mc_version=src_mc_version, compare_dir=target_mods_dir)
    if reason and "已按选项忽略" in reason:
        cfg.log.warn(reason)
    if reason and "mcmod.cn" in reason:
        cfg.log.info(reason)
    if project_id is None:
        return ("manual", jname, meta, reason or "未找到匹配")
    if confidence < ASK_CONF:
        return ("manual", jname, meta, "置信度不足 (%.2f)" % confidence)
    if matched_file:
        return ("matched", jname, meta, project_id, matched_file)
    if confidence < HIGH_CONF and not cfg.auto_yes:
        ver, warn = pick_version(project_id, mc_version, loader)
        if not ver:
            return ("manual", jname, meta, "无适配目标加载器/版本的版本")
        return ("confirm", jname, meta, project_id, confidence, reason, ver, warn)
    if confidence < HIGH_CONF:
        cfg.log.info("置信度 %.2f（%s），自动确认" % (confidence, reason))
    ver, warn = pick_version(project_id, mc_version, loader)
    if not ver:
        return ("manual", jname, meta, "无适配目标加载器/版本的版本")
    return ("ok", jname, meta, project_id, ver, warn)


def migrate_mods(src_mods_dir, target_mods_dir, mc_version, loader, cfg, report,
                 stop_event=None, graph=None, src_mc_version=None):
    os.makedirs(target_mods_dir, exist_ok=True)
    jars = sorted(f for f in os.listdir(src_mods_dir) if f.lower().endswith(".jar"))
    if not jars:
        cfg.log.warn("源 mods 目录中没有 jar 文件: %s" % src_mods_dir)
        return

    downloader = Downloader(max_workers=cfg.download_threads)
    downloaded_projects = set()
    matched_items = []
    n_total = len(jars)

    if stop_event is not None and stop_event.is_set():
        cfg.log.info("已由用户停止。")
        return

    cfg.log.info("阶段一：全部解包解析元数据（%d 个 jar）..." % n_total)
    metas = [None] * n_total
    with ThreadPoolExecutor(max_workers=cfg.analysis_threads) as pool:
        for i, jname in enumerate(jars):
            metas[i] = pool.submit(parse_mod_jar, os.path.join(src_mods_dir, jname))
        for i in range(n_total):
            try:
                metas[i] = metas[i].result()
            except Exception:
                metas[i] = None

    cfg.log.info("阶段二：全部搜索 Modrinth 匹配 ...")
    outcomes = [None] * n_total
    with ThreadPoolExecutor(max_workers=cfg.analysis_threads) as pool:
        for i, jname in enumerate(jars):
            if stop_event is not None and stop_event.is_set():
                break
            cfg.log.info("[%d/%d] 处理: %s" % (i + 1, n_total, jname))
            outcomes[i] = pool.submit(
                _match_one, i, jname, metas[i], os.path.join(src_mods_dir, jname),
                mc_version, src_mc_version, loader, cfg, stop_event, target_mods_dir)
        for i in range(n_total):
            if outcomes[i] is None:
                continue
            try:
                outcomes[i] = outcomes[i].result()
            except Exception as e:
                outcomes[i] = ("manual", jars[i], None, "分析异常: %s" % e)

    for out in outcomes:
        if out is None or out[0] == "stopped":
            continue
        kind = out[0]
        if kind == "skip":
            report["skipped"].append(out[1])
            cfg.log.warn(out[1])
            continue
        if kind == "manual":
            jname, meta, reason = out[1], out[2], out[3]
            report["manual"].append((jname, meta, reason))
            cfg.log.warn("%s，已记入手动清单" % reason)
            continue
        if kind == "matched":
            jname, meta, project_id, matched_file = out[1:]
            if project_id in downloaded_projects:
                report["duplicates"].append(jname)
                try:
                    os.remove(matched_file)
                except OSError:
                    pass
                cfg.log.info("该项目此前已下载，合并重复（%s）" % project_id)
                continue
            downloaded_projects.add(project_id)
            size = human_size(os.path.getsize(matched_file)) if os.path.exists(matched_file) else "?"
            report["ok"].append((meta.get("name") or meta.get("id"), os.path.basename(matched_file)))
            cfg.log.info("比对命中: %s (%s)" % (os.path.basename(matched_file), size))
            matched_items.append((meta.get("name") or meta.get("id"), "mod",
                                  (jname, meta, project_id), matched_file))
            continue
        if kind == "confirm":
            jname, meta, project_id, confidence, reason, ver, warn = out[1:]
            if project_id in downloaded_projects:
                report["duplicates"].append(jname)
                cfg.log.info("该项目此前已下载，合并重复（%s）" % project_id)
                continue
            if not cfg.confirm("置信度 %.2f 匹配到（项目 %s）%s，是否下载？"
                               % (confidence, project_id, reason)):
                report["manual"].append((jname, meta, "用户拒绝"))
                cfg.log.info("用户拒绝，已记入手动清单")
                continue
        else:
            jname, meta, project_id, ver, warn = out[1:]
            if project_id in downloaded_projects:
                report["duplicates"].append(jname)
                cfg.log.info("该项目此前已下载，合并重复（%s）" % project_id)
                continue
        downloaded_projects.add(project_id)
        fname = primary_filename(ver) or "mod.jar"
        cfg.log.info("提交下载: %s" % fname)
        downloader.submit(meta.get("name") or meta.get("id"), ver, target_mods_dir,
                          kind="mod", extra=(jname, meta, project_id))
        if warn:
            cfg.log.warn(warn)
        if not cfg.skip_deps:
            collect_deps(ver, mc_version, loader, target_mods_dir, downloaded_projects,
                         _make_dep_logger(cfg.log, report), downloader)
    if downloader.pending:
        cfg.log.info("等待 %d 个下载完成 ..." % downloader.pending)
    results = downloader.gather(stop_event, cfg.log)
    downloader.shutdown(wait=True)
    for label, kind, extra, dest, err in results:
        if dest:
            size = human_size(os.path.getsize(dest)) if os.path.exists(dest) else "?"
            if kind == "mod":
                jname, meta, pid = extra
                report["ok"].append((label, os.path.basename(dest)))
                cfg.log.info("已下载: %s (%s)" % (os.path.basename(dest), size))
            else:
                report["deps"].append("依赖 %s -> %s" % (label, os.path.basename(dest)))
                cfg.log.info("依赖 %s -> %s (%s)" % (label, os.path.basename(dest), size))
        else:
            if kind == "mod":
                jname, meta, pid = extra
                report["manual"].append((jname, meta, "下载失败: %s" % err))
                cfg.log.error("下载失败: %s" % err)
            else:
                cfg.log.error("依赖 %s 下载失败: %s" % (label, err))

    if graph:
        cfg.log.info("全部下载完成，正在解包并构建依赖图 ...")
        graph_items = [(r[0], r[1], r[2], r[3]) for r in results if r[3]] + matched_items
        for label, kind, extra, dest in graph_items:
            pid = extra[2] if kind == "mod" else extra
            gmeta = parse_mod_jar(dest)
            if gmeta:
                graph.add_mod(gmeta.get("id") or label, gmeta.get("name") or label,
                              gmeta.get("version") or "", dest, pid,
                              gmeta.get("deps") or [], gmeta.get("conflicts") or [])
        _pin_dependency_versions(graph, mc_version, loader, target_mods_dir, cfg, report)


def _pin_dependency_versions(graph, mc_version, loader, target_mods_dir, cfg, report):
    for a, b, ranges, bver in graph.mismatches():
        binfo = graph.mods.get(b)
        if not binfo or not binfo.get("project_id"):
            continue
        ver = pick_version_in_range(binfo["project_id"], mc_version, loader,
                                    graph.requirements_for(b))
        if not ver or ver.get("version_number") == bver:
            continue
        old = binfo.get("file")
        dest, err = mr_download_file(ver, target_mods_dir)
        if not dest:
            cfg.log.warn("无法为 %s 更换到满足 %s 依赖的版本: %s" % (b, a, err))
            continue
        if old and os.path.exists(old) and os.path.abspath(old) != os.path.abspath(dest):
            try:
                os.remove(old)
            except OSError:
                pass
        binfo["file"] = dest
        binfo["version"] = ver.get("version_number") or ""
        cfg.log.info("%s 依赖 %s@%s，已将 %s 更换为版本 %s"
                     % (a, b, ",".join(ranges), b, ver.get("version_number")))
        report["ok"] = [(n, f) for n, f in report["ok"] if f != os.path.basename(old)] + \
                       [(binfo.get("name") or b, os.path.basename(dest))]


def resolve_conflicts(graph, cfg):
    conflicts = graph.conflict_report()
    if not conflicts:
        return []
    if cfg.on_conflicts:
        action = cfg.on_conflicts(conflicts)
    else:
        action = "skip"
    if action == "delete_c":
        targets = [item["mod"] for item in conflicts]
        for item in conflicts:
            targets += item["dependents"]
    elif action == "delete_conflicts":
        targets = []
        for item in conflicts:
            targets += item["conflicting"]
    else:
        for item in conflicts:
            cfg.log.warn("模组 %s 被 %d 个模组依赖、与 %d 个模组冲突（忽略，未处理）"
                         % (item["name"], len(item["dependents"]), len(item["conflicting"])))
        return []
    removed = []
    for mid in sorted(set(targets)):
        info = graph.remove(mid)
        if info and info.get("file") and os.path.exists(info["file"]):
            os.remove(info["file"])
            removed.append(os.path.basename(info["file"]))
            cfg.log.warn("已删除 %s（%s）" % (mid, os.path.basename(info["file"])))
    return removed

def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def copy_tree_overwrite(src, dst, log):
    shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
    n = sum(len(fs) for _r, _d, fs in os.walk(dst))
    log.info("已迁移 %s -> %s（%d 个文件）" % (src, dst, n))


def copy_tree_missing(src, dst):
    copied = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            s, d = os.path.join(root, f), os.path.join(target_dir, f)
            if not os.path.exists(d):
                shutil.copy2(s, d)
                copied += 1
    return copied


def copy_saves_merge(src_dir, dst_dir, log=None):
    os.makedirs(dst_dir, exist_ok=True)
    copied = renamed = 0
    for entry in sorted(os.listdir(src_dir)):
        s = os.path.join(src_dir, entry)
        d = os.path.join(dst_dir, entry)
        if os.path.exists(d):
            name = entry + "_old"
            while os.path.exists(os.path.join(dst_dir, name)):
                name += "_old"
            d = os.path.join(dst_dir, name)
            renamed += 1
            if log:
                log.info("重名 %s -> %s（已加 _old）" % (entry, os.path.basename(d)))
        if os.path.isdir(s):
            shutil.copytree(s, d, copy_function=shutil.copy2)
        else:
            shutil.copy2(s, d)
        copied += 1
    return copied, renamed


def find_stray(root):
    version_names = set()
    vdir = os.path.join(root, "versions")
    if os.path.isdir(vdir):
        try:
            version_names = {n for n in os.listdir(vdir)
                             if os.path.isdir(os.path.join(vdir, n))}
        except OSError:
            pass
    base = os.path.basename(os.path.normpath(root))
    launcher_files = {base + ".jar", base + ".json"}
    dirs, files = [], []
    try:
        entries = os.listdir(root)
    except OSError:
        return dirs, files
    for e in entries:
        if e.startswith("."):
            continue
        low = e.lower()
        if low in KNOWN_DIRS or low in KNOWN_FILES:
            continue
        if low in OPTIONAL_DIRS or low in OPTIONAL_FILES:
            continue
        if e in launcher_files:
            continue
        if e.endswith(".json") and e[:-5] in version_names:
            continue
        p = os.path.join(root, e)
        if os.path.isdir(p):
            dirs.append(e)
        elif os.path.isfile(p):
            files.append(e)
    return sorted(dirs), sorted(files)


def migrate_game_data(src_root, dst_root, cfg):
    server_mode = is_server_root(src_root)
    choices = cfg.choices
    cfg.log.info("\n=== 迁移游戏数据（%s）===" % ("服务端" if server_mode else "客户端"))
    if not any(choices.get(k) for k in CHOICE_KEYS):
        cfg.log.info("未勾选任何数据类别，跳过")
        return
    if choices.get("config"):
        sc = os.path.join(src_root, "config")
        if os.path.isdir(sc):
            cfg.log.info("正在迁移 config 目录 (%s) ..." % human_size(dir_size(sc)))
            copy_tree_overwrite(sc, os.path.join(dst_root, "config"), cfg.log)
        else:
            cfg.log.info("源目录没有 config，跳过")
    if choices.get("options"):
        so = os.path.join(src_root, "options.txt")
        if os.path.exists(so):
            shutil.copy2(so, os.path.join(dst_root, "options.txt"))
            cfg.log.info("options.txt 已复制")
        else:
            cfg.log.info("源目录没有 options.txt，跳过")
    if choices.get("saves"):
        sdir = "world" if server_mode else "saves"
        ss = os.path.join(src_root, sdir)
        if os.path.isdir(ss):
            cfg.log.info("正在迁移 %s (%s)，重名自动加 _old ..." % (sdir, human_size(dir_size(ss))))
            n, r = copy_saves_merge(ss, os.path.join(dst_root, sdir), cfg.log)
            cfg.log.info("%s 迁移完成：复制 %d 项，重名改名 %d 项（已加 _old）" % (sdir, n, r))
        else:
            cfg.log.info("源目录没有 %s，跳过" % sdir)
    if choices.get("stray"):
        stray_dirs, stray_files = find_stray(src_root)
        if stray_dirs or stray_files:
            cfg.log.info("发现模组生成的目录/文件:")
            for d in stray_dirs:
                cfg.log.info("  %s/  (%s)" % (d, human_size(dir_size(os.path.join(src_root, d)))))
            for f in stray_files:
                cfg.log.info("  %s" % f)
            for d in stray_dirs:
                s, dst = os.path.join(src_root, d), os.path.join(dst_root, d)
                n = copy_tree_missing(s, dst)
                cfg.log.info("%s/ 已迁移（新复制 %d 个文件）" % (d, n))
            for f in stray_files:
                shutil.copy2(os.path.join(src_root, f), os.path.join(dst_root, f))
                cfg.log.info("%s 已复制" % f)
        else:
            cfg.log.info("没有发现模组生成的杂项目录")
    if choices.get("optional"):
        opt_dirs = [d for d in OPTIONAL_DIRS if os.path.isdir(os.path.join(src_root, d))]
        opt_files = [f for f in OPTIONAL_FILES if os.path.exists(os.path.join(src_root, f))]
        if opt_dirs or opt_files:
            for d in opt_dirs:
                copy_tree_missing(os.path.join(src_root, d), os.path.join(dst_root, d))
                cfg.log.info("%s/ 已迁移" % d)
            for f in opt_files:
                shutil.copy2(os.path.join(src_root, f), os.path.join(dst_root, f))
                cfg.log.info("%s 已复制" % f)
        else:
            cfg.log.info("源目录没有资源包/光影/servers.dat，跳过")
    if server_mode and choices.get("server"):
        copied = False
        for fn in SERVER_FILES:
            s, d = os.path.join(src_root, fn), os.path.join(dst_root, fn)
            if os.path.exists(s):
                shutil.copy2(s, d)
                cfg.log.info("%s 已复制" % fn)
                copied = True
        if not copied:
            cfg.log.info("源服务端没有服务端专属文件，跳过")

def run_migration(params, cfg):
    stop_event = params.get("stop_event")
    mc_root, src_version = params["src_root"], params["src_version"]
    t_root, t_version = params["target_root"], params["target_version"]
    t_loader, t_mc = params["target_loader"], params["target_mc"]

    PROXY_SETTINGS["use_system"] = cfg.use_system_proxy
    configure_http(cfg.log)

    if src_version:
        src_client, src_isolated = client_paths(
            mc_root, src_version, force_isolated=params.get("src_force_isolated", False))
        src_loader = detect_loader(mc_root, src_version)
        src_desc = "%s%s" % (src_version, "（版本隔离）" if src_isolated else "")
    else:
        src_client, src_isolated = mc_root, False
        src_loader = sniff_server_loader(mc_root)
        src_desc = "服务端根目录"
    cfg.log.info("源: %s%s" % (src_desc, ("  [%s]" % LOADER_LABEL[src_loader]) if src_loader else ""))
    cfg.log.info("根目录: %s" % src_client)
    if not src_loader:
        cfg.log.warn("未能识别源加载器")

    t_detected = None
    if t_version:
        dst_client, dst_isolated = client_paths(
            t_root, t_version, force_isolated=params.get("target_force_isolated", False))
        t_detected = detect_loader(t_root, t_version)
        dst_desc = "%s%s" % (t_version, "（版本隔离）" if dst_isolated else "")
    else:
        dst_client, dst_isolated = t_root, False
        if is_server_root(t_root):
            dst_desc = "服务端根目录"
            t_detected = sniff_server_loader(t_root)
        else:
            dst_desc = "根目录（非隔离，mods 在根目录）"
    if t_detected and t_detected != t_loader:
        cfg.log.warn("目标检测到的加载器是 %s，与所选 %s 不一致！"
                     % (LOADER_LABEL.get(t_detected, t_detected), LOADER_LABEL.get(t_loader, t_loader)))
    cfg.log.info("目标: %s%s" % (dst_desc, ("  [%s]" % LOADER_LABEL[t_detected]) if t_detected else ""))
    cfg.log.info("目标加载器: %s   目标 MC 版本: %s" % (LOADER_LABEL.get(t_loader, t_loader), t_mc))

    same_client = os.path.normcase(os.path.realpath(src_client)) == \
                  os.path.normcase(os.path.realpath(dst_client))
    if same_client:
        cfg.log.info("注意: 源与目标是同一目录，进入「更新模组」模式（只更新 mods，不迁移数据）")

    src_mods = os.path.join(src_client, "mods")
    dst_mods = os.path.join(dst_client, "mods")
    if not os.path.isdir(src_mods):
        raise RuntimeError("源目录 mods 不存在: %s" % src_mods)
    os.makedirs(dst_mods, exist_ok=True)

    cfg.log.info("\n=== 模组迁移（源 mods 目录: %s）===" % src_mods)

    report = {"ok": [], "manual": [], "deps": [], "skipped": [], "duplicates": []}
    from .graph import ModGraph
    from .versions import base_mc_version
    graph = ModGraph()
    src_mc_ver = base_mc_version(src_version) if src_version else ""
    migrate_mods(src_mods, dst_mods, t_mc, t_loader, cfg, report, stop_event, graph,
                 src_mc_version=src_mc_ver)

    if not same_client:
        migrate_game_data(src_client, dst_client, cfg)

    if cfg.print_failures and report["manual"]:
        print_failure_links(report["manual"], cfg.log)

    if graph.mods:
        n_mods, n_deps, n_confs = graph.summary()
        cfg.log.info("依赖图统计: %d 个模组, %d 条依赖, %d 条冲突" % (n_mods, n_deps, n_confs))
        removed = resolve_conflicts(graph, cfg)
        if removed:
            report["ok"] = [(n, f) for n, f in report["ok"] if f not in removed]
    return report, same_client


def print_failure_links(manual_list, log):
    from .modrinth import lookup_project_links
    log.warn("匹配失败 %d 个模组：" % len(manual_list))
    for jname, meta, reason in manual_list:
        log.warn("  %s（%s）" % (jname, reason))
        try:
            links = lookup_project_links(meta)
        except Exception:
            links = None
        if links:
            log.warn("    Modrinth: %s" % links["modrinth"])
            if links.get("source"):
                log.warn("    开源仓库: %s" % links["source"])
        else:
            log.warn("    未找到 Modrinth 项目")


def print_summary(report, same_client):
    from urllib.parse import quote
    print("\n" + "=" * 50)
    print("迁移完成，摘要：")
    print("成功下载 %d 个模组：" % len(report["ok"]))
    for name, fname in report["ok"]:
        print("  %s -> %s" % (name, fname))
    if report["skipped"]:
        print("跳过 %d 个（库文件/非模组）：" % len(report["skipped"]))
        for j in report["skipped"]:
            print("  %s" % j)
    if report.get("duplicates"):
        print("重复项目合并 %d 个（同一项目只下载一次）：" % len(report["duplicates"]))
    if report["deps"]:
        print("依赖：")
        for d in report["deps"]:
            print("  %s" % d)
    if report["manual"]:
        print("%d 个模组需要手动处理：" % len(report["manual"]))
        for jname, meta, why in report["manual"]:
            q = meta.get("name") or meta.get("id") or jname
            print("  - %s（%s）" % (jname, why))
            print("    搜索: https://modrinth.com/search?q=%s" % quote(q))
    if same_client:
        print("（同客户端更新模式，未迁移数据目录）")


def write_report_file(report, src_desc, dst_desc):
    from urllib.parse import quote
    lines = ["MC 模组迁移报告", "时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "源: %s" % src_desc, "目标: %s" % dst_desc, ""]
    lines.append("成功下载 %d 个模组:" % len(report["ok"]))
    for name, fname in report["ok"]:
        lines.append("  ✓ %s -> %s" % (name, fname))
    if report["skipped"]:
        lines.append("")
        lines.append("跳过 %d 个（库文件/非模组）:" % len(report["skipped"]))
        lines += ["  · " + j for j in report["skipped"]]
    if report["manual"]:
        lines.append("")
        lines.append("需要手动处理 %d 个:" % len(report["manual"]))
        for jname, meta, why in report["manual"]:
            q = meta.get("name") or meta.get("id") or jname
            lines.append("  - %s（%s）" % (jname, why))
            lines.append("    搜索: https://modrinth.com/search?q=%s" % quote(q))
    fname = os.path.join(os.getcwd(), "迁移报告_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fname
