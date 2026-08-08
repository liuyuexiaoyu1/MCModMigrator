import os
import shutil
from datetime import datetime

from .core import (ASK_CONF, CHOICE_KEYS, HIGH_CONF, KNOWN_DIRS, KNOWN_FILES,
                   LOADER_LABEL, OPTIONAL_DIRS, OPTIONAL_FILES, SERVER_FILES,
                   Logger, PROXY_SETTINGS, human_size, plain_log_sink)
from .clients import client_paths, detect_loader, is_server_root, sniff_server_loader
from .mod_parser import parse_mod_jar
from .modrinth import (Downloader, collect_deps, configure_http,
                       match_to_project, pick_version, primary_filename)


def _make_dep_logger(base, report):
    def sink(msg, level):
        getattr(base, level)(msg)
        report["deps"].append(msg)
    return Logger(sink)


class RunConfig:
    def __init__(self, auto_yes=False, skip_deps=False, choices=None,
                 use_system_proxy=True, download_threads=4, print_failures=True,
                 log=None, confirm=None):
        self.auto_yes = auto_yes
        self.skip_deps = skip_deps
        self.use_system_proxy = use_system_proxy
        self.download_threads = max(1, min(int(download_threads or 4), 16))
        self.print_failures = print_failures
        self.choices = choices or {k: True for k in CHOICE_KEYS}
        self.log = log or Logger(plain_log_sink)
        self.confirm = confirm or (lambda p: True)

def migrate_mods(src_mods_dir, target_mods_dir, mc_version, loader, cfg, report, stop_event=None):
    os.makedirs(target_mods_dir, exist_ok=True)
    jars = sorted(f for f in os.listdir(src_mods_dir) if f.lower().endswith(".jar"))
    if not jars:
        cfg.log.warn("源 mods 目录中没有 jar 文件: %s" % src_mods_dir)
        return

    downloader = Downloader(max_workers=cfg.download_threads)
    downloaded_projects = set()
    n_total = len(jars)
    for i, jname in enumerate(jars, 1):
        if stop_event is not None and stop_event.is_set():
            cfg.log.info("已由用户停止。")
            break
        jpath = os.path.join(src_mods_dir, jname)
        cfg.log.info("[%d/%d] 处理: %s" % (i, n_total, jname))
        cfg.log.info("正在解包解析元数据: %s ..." % jname)
        meta = parse_mod_jar(jpath)
        if meta:
            cfg.log.info("正在查询 Modrinth 匹配 %s ..." % (meta.get("name") or meta.get("id")))
        if not meta:
            report["skipped"].append(jname)
            cfg.log.warn("无法解析该 jar（可能不是模组或文件损坏），跳过")
            continue
        if meta.get("library"):
            report["skipped"].append(jname)
            cfg.log.warn("%s 是库文件（library=true），跳过" % meta.get("id"))
            continue

        project_id, confidence, reason = match_to_project(meta, mc_version, loader, jpath)
        if project_id is None:
            report["manual"].append((jname, meta, reason or "未找到匹配"))
            cfg.log.warn("%s，已记入手动清单" % (reason or "未找到匹配"))
            continue
        if project_id in downloaded_projects:
            report["duplicates"].append(jname)
            cfg.log.info("该项目此前已下载，合并重复（%s）" % project_id)
            continue
        if confidence < ASK_CONF:
            report["manual"].append((jname, meta, "置信度不足 (%.2f)" % confidence))
            cfg.log.warn("置信度 %.2f 不足（%s），已记入手动清单" % (confidence, reason))
            continue

        if confidence < HIGH_CONF:
            hit_name = "（项目 %s）" % project_id
            if not cfg.auto_yes:
                if not cfg.confirm("置信度 %.2f 匹配到 %s%s，是否下载？" % (confidence, hit_name, reason)):
                    report["manual"].append((jname, meta, "用户拒绝"))
                    cfg.log.info("用户拒绝，已记入手动清单")
                    continue
            else:
                cfg.log.info("置信度 %.2f（%s），自动确认" % (confidence, reason))

        ver, warn = pick_version(project_id, mc_version, loader)
        if not ver:
            report["manual"].append((jname, meta, "无适配目标加载器/版本的版本"))
            cfg.log.warn("该项目没有适配 %s %s 的版本，已记入手动清单"
                         % (LOADER_LABEL.get(loader, loader), mc_version))
            continue
        downloaded_projects.add(project_id)
        fname = primary_filename(ver) or "mod.jar"
        cfg.log.info("提交下载: %s" % fname)
        downloader.submit(meta.get("name") or meta.get("id"), ver, target_mods_dir,
                          kind="mod", extra=(jname, meta))
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
                jname, meta = extra
                report["ok"].append((label, os.path.basename(dest)))
                cfg.log.info("已下载: %s (%s)" % (os.path.basename(dest), size))
            else:
                report["deps"].append("依赖 %s -> %s" % (label, os.path.basename(dest)))
                cfg.log.info("依赖 %s -> %s (%s)" % (label, os.path.basename(dest), size))
        else:
            if kind == "mod":
                jname, meta = extra
                report["manual"].append((jname, meta, "下载失败: %s" % err))
                cfg.log.error("下载失败: %s" % err)
            else:
                cfg.log.error("依赖 %s 下载失败: %s" % (label, err))

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
    migrate_mods(src_mods, dst_mods, t_mc, t_loader, cfg, report, stop_event)

    if not same_client:
        migrate_game_data(src_client, dst_client, cfg)

    if cfg.print_failures and report["manual"]:
        print_failure_links(report["manual"], cfg.log)
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
    if report.get("duplicates"):
        lines.append("")
        lines.append("重复项目合并 %d 个（同一项目只下载一次）:" % len(report["duplicates"]))
        lines += ["  · " + j for j in report["duplicates"]]
    if report["manual"]:
        lines.append("")
        lines.append("⚠ 需要手动处理 %d 个:" % len(report["manual"]))
        for jname, meta, why in report["manual"]:
            q = meta.get("name") or meta.get("id") or jname
            lines.append("  - %s（%s）" % (jname, why))
            lines.append("    搜索: https://modrinth.com/search?q=%s" % quote(q))
    fname = os.path.join(os.getcwd(), "迁移报告_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fname
