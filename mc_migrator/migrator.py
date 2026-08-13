import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

BIG_FOLDER_THRESHOLD = 5 * 1024 ** 3

from .core import (ASK_CONF, CHOICE_KEYS, HIGH_CONF, KNOWN_DIRS, KNOWN_FILES,
                   LOADER_LABEL, OPTIONAL_DIRS, OPTIONAL_FILES, SERVER_FILES,
                   Logger, PROXY_SETTINGS, human_size, plain_log_sink)
from .clients import client_paths, detect_loader, is_server_root, sniff_server_loader
from .mod_parser import parse_mod_jar, sha1_of
from .modrinth import (Downloader, collect_deps, configure_http,
                       match_to_project, mr_download_file, mr_lookup_sha1,
                       mr_versions, pick_version, pick_version_in_range,
                       primary_filename, ver_compatible)
from .graph import version_satisfies


def _make_dep_logger(base, report):
    def sink(msg, level):
        getattr(base, level)(msg)
        if msg.startswith("提交依赖下载"):
            return
        report["deps"].append(msg)
    return Logger(sink)


class RunConfig:
    def __init__(self, auto_yes=False, skip_deps=False, choices=None,
                 use_system_proxy=True, download_threads=4, analysis_threads=4,
                 print_failures=True, ignore_fork=False, migrate_mods=True,
                 log=None, confirm=None):
        self.auto_yes = auto_yes
        self.skip_deps = skip_deps
        self.use_system_proxy = use_system_proxy
        self.download_threads = max(1, min(int(download_threads or 4), 16))
        self.analysis_threads = max(1, min(int(analysis_threads or 8), 16))
        self.print_failures = print_failures
        self.ignore_fork = ignore_fork
        self.migrate_mods = migrate_mods
        self.on_conflicts = None
        self.pending_big = []
        self.migrated_dirs = []
        self.choices = choices or {k: True for k in CHOICE_KEYS}
        self.log = log or Logger(plain_log_sink)
        self.confirm = confirm or (lambda p: True)

def _match_one(i, jname, meta, jpath, mc_version, src_mc_version, loader, cfg, stop_event,
               target_mods_dir):
    if stop_event is not None and stop_event.is_set():
        return ("stopped",)
    if not meta:
        return ("skip", jname, "无法解析该 jar（可能不是模组或文件损坏），跳过")
    if meta.get("library"):
        return ("skip", jname, "%s 是库文件（library=true），跳过" % meta.get("id"))
    cfg.log.info("正在查询 Modrinth 匹配 %s ..." % (meta.get("name") or meta.get("id")))
    project_id, confidence, reason, matched_file = match_to_project(
        meta, mc_version, loader, jpath, ignore_fork=cfg.ignore_fork,
        src_mc_version=src_mc_version, compare_dir=target_mods_dir)
    if reason and "已按选项忽略" in reason:
        cfg.log.warn(reason)
    if reason and "mcmod.cn" in reason:
        cfg.log.info(reason)
    if reason and "CurseForge" in reason:
        cfg.log.info(reason)
    if isinstance(project_id, str) and project_id.startswith("github:"):
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

    downloader = Downloader(max_workers=cfg.download_threads, log=cfg.log)
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
            jname, reason = out[1], out[2]
            if "无法解析" in reason:
                report["skipped"].append((jname, reason))
                cfg.log.warn("%s（%s）" % (jname, reason))
            else:
                cfg.log.info("%s（%s）" % (jname, reason))
            continue
        if kind == "manual":
            jname, meta, reason = out[1], out[2], out[3]
            report["manual"].append((jname, meta, reason))
            cfg.log.warn("%s（%s），已记入手动清单"
                         % (meta.get("name") or meta.get("id") or jname, reason))
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
            if kind == "mod":
                jname, meta, pid = extra
                report["ok"].append((label, os.path.basename(dest)))
            else:
                report["deps"].append("依赖 %s -> %s" % (label, os.path.basename(dest)))
        else:
            if kind == "mod":
                jname, meta, pid = extra
                report["manual"].append((jname, meta, "下载失败: %s" % err))

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


def _lookup_project_id(jpath):
    try:
        ver = mr_lookup_sha1(sha1_of(jpath))
        if ver and ver.get("project_id"):
            return ver["project_id"]
    except requests.RequestException:
        pass
    return None


def repair_mods_same_dir(mods_dir, mc_version, loader, cfg, report, graph):
    jars = sorted(f for f in os.listdir(mods_dir) if f.lower().endswith(".jar"))
    if not jars:
        cfg.log.warn("mods 目录中没有 jar 文件: %s" % mods_dir)
        return
    n = len(jars)
    cfg.log.info("同目录同版本更新模式：只解包检查依赖与冲突，不重新下载（%d 个 jar）" % n)
    cfg.log.info("阶段一：全部解包解析元数据 ...")
    metas = [None] * n
    with ThreadPoolExecutor(max_workers=cfg.analysis_threads) as pool:
        for i, jname in enumerate(jars):
            metas[i] = pool.submit(parse_mod_jar, os.path.join(mods_dir, jname))
        for i in range(n):
            try:
                metas[i] = metas[i].result()
            except Exception:
                metas[i] = None
    cfg.log.info("阶段二：sha1 识别项目并构建依赖图 ...")
    ids = [None] * n
    with ThreadPoolExecutor(max_workers=cfg.analysis_threads) as pool:
        for i, jname in enumerate(jars):
            if metas[i] is None or metas[i].get("library"):
                continue
            ids[i] = pool.submit(_lookup_project_id, os.path.join(mods_dir, jname))
        for i in range(n):
            if ids[i] is None:
                continue
            try:
                ids[i] = ids[i].result()
            except Exception:
                ids[i] = None
    for jname, meta, pid in zip(jars, metas, ids):
        if meta is None:
            report["skipped"].append((jname, "无法解析该 jar（可能不是模组或文件损坏），跳过"))
            cfg.log.warn("%s（无法解析该 jar（可能不是模组或文件损坏），跳过）" % jname)
            continue
        if meta.get("library"):
            cfg.log.info("%s 是库文件（library=true），跳过" % jname)
            continue
        graph.add_mod(meta.get("id") or jname, meta.get("name") or jname,
                      meta.get("version") or "", os.path.join(mods_dir, jname),
                      pid, meta.get("deps") or [], meta.get("conflicts") or [])
        report["checked"].append((meta.get("name") or meta.get("id"), jname))
    if not graph.mods:
        cfg.log.warn("没有可检查的模组")
        return
    _pin_dependency_versions(graph, mc_version, loader, mods_dir, cfg, report)


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
        cfg.log.info("%s 依赖 %s@%s，已将 %s（当前版本 %s）更换为版本 %s"
                     % (a, b, ",".join(ranges), b, binfo.get("version") or "?",
                        ver.get("version_number") or "?"))
        binfo["file"] = dest
        binfo["version"] = ver.get("version_number") or ""
        report["ok"] = [(n, f) for n, f in report["ok"] if f != os.path.basename(old)] + \
                       [(binfo.get("name") or b, os.path.basename(dest))]


def _parse_probe(v, target_mods_dir):
    dest, err = mr_download_file(v, target_mods_dir)
    if not dest:
        return None, err
    cm = parse_mod_jar(dest)
    try:
        os.remove(dest)
    except OSError:
        pass
    if not cm:
        return None, "解析失败"
    return cm, None


def _replace_version(binfo, candidates, ranges, target_mods_dir, cfg, note, report):
    for v in candidates:
        cm, err = _parse_probe(v, target_mods_dir)
        if not cm:
            continue
        jver = cm.get("version") or ""
        if jver == binfo.get("version"):
            continue
        if any(version_satisfies(jver, r) for r in ranges):
            continue
        dest, err2 = mr_download_file(v, target_mods_dir)
        if not dest:
            continue
        old = binfo.get("file")
        if old and os.path.exists(old) and os.path.abspath(old) != os.path.abspath(dest):
            try:
                os.remove(old)
            except OSError:
                pass
        cfg.log.info("%s 与 %s 冲突，已将 %s（当前版本 %s）更换为不冲突版本 %s"
                     % (binfo.get("name") or "mod", note, binfo.get("name") or "mod",
                        binfo.get("version") or "?", v.get("version_number") or jver))
        binfo["file"] = dest
        binfo["version"] = jver
        report["ok"] = [(n, f) for n, f in report["ok"] if f != os.path.basename(old)] + \
                       [(binfo.get("name") or "mod", os.path.basename(dest))]
        return True
    return False


def _compatible_versions(pid, mc_version, loader):
    vs = mr_versions(pid, mc_version, loader)
    if not vs:
        vs = mr_versions(pid)
    return [v for v in vs
            if (mc_version in v.get("game_versions", [])
                or any(ver_compatible(g, mc_version) for g in v.get("game_versions", [])))
            and (not loader or loader in v.get("loaders", []))]


def auto_resolve_conflicts(graph, mc_version, loader, target_mods_dir, cfg, report):
    for item in graph.conflict_report():
        if item["dependents"]:
            continue
        b = item["mod"]
        ranges = set()
        for reqs in graph.conf_reqs.values():
            if b in reqs:
                ranges |= reqs[b]
        if not ranges:
            continue
        binfo = graph.mods.get(b)
        if not binfo or not binfo.get("project_id"):
            continue
        if _replace_version(binfo, _compatible_versions(binfo["project_id"], mc_version, loader),
                            ranges, target_mods_dir, cfg, "、".join(item["conflicting"]), report):
            continue
        if not _replace_breaker(item, graph, mc_version, loader, target_mods_dir, cfg, report):
            cfg.log.warn("%s 与 %s 冲突，且找不到不冲突的版本"
                         % (b, "、".join(item["conflicting"])))


def _replace_breaker(item, graph, mc_version, loader, target_mods_dir, cfg, report):
    b = item["mod"]
    bv = graph.mods[b].get("version") or ""
    for a in item["conflicting"]:
        ainfo = graph.mods.get(a)
        if not ainfo or not ainfo.get("project_id"):
            continue
        reqs = graph.requirements_for(a)
        probed = 0
        for v in _compatible_versions(ainfo["project_id"], mc_version, loader):
            if probed >= 10:
                break
            probed += 1
            cm, err = _parse_probe(v, target_mods_dir)
            if not cm:
                continue
            jver = cm.get("version") or ""
            if jver == ainfo.get("version"):
                continue
            if reqs and not all(version_satisfies(jver, r) for r in reqs):
                continue
            cbreaks = dict((t, r) for t, r in cm.get("conflicts", []))
            if b in cbreaks and version_satisfies(bv, cbreaks[b]):
                continue
            existing_targets = set(graph.conf_reqs.get(a, {}).keys())
            new_break = False
            for t, r in cbreaks.items():
                tv = graph.mods.get(t, {}).get("version")
                if tv and version_satisfies(tv, r) and t not in existing_targets:
                    new_break = True
                    break
            if new_break:
                continue
            final, err2 = mr_download_file(v, target_mods_dir)
            if not final:
                continue
            old = ainfo.get("file")
            if old and os.path.exists(old) and os.path.abspath(old) != os.path.abspath(final):
                try:
                    os.remove(old)
                except OSError:
                    pass
            cfg.log.info("%s 与 %s 冲突，已将 %s（当前版本 %s）更换为不冲突版本 %s"
                         % (a, b, a, ainfo.get("version") or "?", v.get("version_number") or "?"))
            ainfo["file"] = final
            ainfo["version"] = v.get("version_number") or ""
            report["ok"] = [(n, f) for n, f in report["ok"] if f != os.path.basename(old)] + \
                           [(ainfo.get("name") or a, os.path.basename(final))]
            graph.conf_reqs[a] = {}
            for t, r in cm.get("conflicts", []):
                graph.conf_reqs[a].setdefault(t, set()).add(r)
            graph.conflicting = {}
            for src, reqs in graph.conf_reqs.items():
                for t in reqs:
                    graph.conflicting.setdefault(t, set()).add(src)
            return True
    return False


def resolve_conflicts(graph, cfg):
    conflicts = [item for item in graph.conflict_report() if item["dependents"]]
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
    def _defer_or_copy(label, src, dst, kind):
        size = dir_size(src)
        if size > BIG_FOLDER_THRESHOLD:
            cfg.pending_big.append((label, src, dst, kind))
            cfg.log.info("%s (%s) 超过 5GB，留到最后询问后迁移" % (label, human_size(size)))
            return True
        return False

    if choices.get("config"):
        sc = os.path.join(src_root, "config")
        if os.path.isdir(sc):
            if not _defer_or_copy("config", sc, os.path.join(dst_root, "config"), "overwrite"):
                cfg.log.info("正在迁移 config 目录 (%s) ..." % human_size(dir_size(sc)))
                copy_tree_overwrite(sc, os.path.join(dst_root, "config"), cfg.log)
                cfg.migrated_dirs.append(("config", sc, os.path.join(dst_root, "config")))
        else:
            cfg.log.info("源目录没有 config，跳过")
    if choices.get("options"):
        so = os.path.join(src_root, "options.txt")
        if os.path.exists(so):
            shutil.copy2(so, os.path.join(dst_root, "options.txt"))
            cfg.log.info("options.txt 已复制")
            cfg.migrated_dirs.append(("options.txt", so, os.path.join(dst_root, "options.txt")))
        else:
            cfg.log.info("源目录没有 options.txt，跳过")
    if choices.get("saves"):
        sdir = "world" if server_mode else "saves"
        ss = os.path.join(src_root, sdir)
        if os.path.isdir(ss):
            if not _defer_or_copy(sdir, ss, os.path.join(dst_root, sdir), "saves"):
                cfg.log.info("正在迁移 %s (%s)，重名自动加 _old ..." % (sdir, human_size(dir_size(ss))))
                n, r = copy_saves_merge(ss, os.path.join(dst_root, sdir), cfg.log)
                cfg.log.info("%s 迁移完成：复制 %d 项，重名改名 %d 项（已加 _old）" % (sdir, n, r))
                cfg.migrated_dirs.append((sdir, ss, os.path.join(dst_root, sdir)))
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
                if _defer_or_copy(d, s, dst, "missing"):
                    continue
                n = copy_tree_missing(s, dst)
                cfg.log.info("%s/ 已迁移（新复制 %d 个文件）" % (d, n))
                cfg.migrated_dirs.append((d, s, dst))
            for f in stray_files:
                shutil.copy2(os.path.join(src_root, f), os.path.join(dst_root, f))
                cfg.log.info("%s 已复制" % f)
                cfg.migrated_dirs.append((f, os.path.join(src_root, f), os.path.join(dst_root, f)))
        else:
            cfg.log.info("没有发现模组生成的杂项目录")
    if choices.get("optional"):
        opt_dirs = [d for d in OPTIONAL_DIRS if os.path.isdir(os.path.join(src_root, d))]
        opt_files = [f for f in OPTIONAL_FILES if os.path.exists(os.path.join(src_root, f))]
        if opt_dirs or opt_files:
            for d in opt_dirs:
                copy_tree_missing(os.path.join(src_root, d), os.path.join(dst_root, d))
                cfg.log.info("%s/ 已迁移" % d)
                cfg.migrated_dirs.append((d, os.path.join(src_root, d), os.path.join(dst_root, d)))
            for f in opt_files:
                shutil.copy2(os.path.join(src_root, f), os.path.join(dst_root, f))
                cfg.log.info("%s 已复制" % f)
                cfg.migrated_dirs.append((f, os.path.join(src_root, f), os.path.join(dst_root, f)))
        else:
            cfg.log.info("源目录没有资源包/光影/servers.dat，跳过")
    if server_mode and choices.get("server"):
        copied = False
        for fn in SERVER_FILES:
            s, d = os.path.join(src_root, fn), os.path.join(dst_root, fn)
            if os.path.exists(s):
                shutil.copy2(s, d)
                cfg.log.info("%s 已复制" % fn)
                cfg.migrated_dirs.append(("服务端文件: " + fn, s, d))
                copied = True
        if not copied:
            cfg.log.info("源服务端没有服务端专属文件，跳过")


def migrate_big_folders(cfg):
    if not cfg.pending_big:
        return
    lines = ["以下目录超过 5GB，是否现在迁移？"]
    for label, src, _dst, _kind in cfg.pending_big:
        lines.append("  %s（%s）" % (label, human_size(dir_size(src))))
    if not cfg.confirm("\n".join(lines)):
        cfg.log.info("已跳过 %d 个大目录的迁移" % len(cfg.pending_big))
        return
    for label, src, dst, kind in cfg.pending_big:
        if kind == "saves":
            n, r = copy_saves_merge(src, dst, cfg.log)
            cfg.log.info("%s 迁移完成：复制 %d 项，重名改名 %d 项（已加 _old）" % (label, n, r))
        elif kind == "overwrite":
            cfg.log.info("正在迁移 %s (%s) ..." % (label, human_size(dir_size(src))))
            copy_tree_overwrite(src, dst, cfg.log)
        else:
            n = copy_tree_missing(src, dst)
            cfg.log.info("%s 迁移完成：新复制 %d 个文件" % (label, n))
        cfg.migrated_dirs.append((label, src, dst))
    cfg.pending_big = []

def _fmt_dur(s):
    if s < 60:
        return "%.1f 秒" % s
    return "%d 分 %.0f 秒" % (int(s // 60), s % 60)


def run_migration(params, cfg):
    _t0 = time.monotonic()
    stop_event = params.get("stop_event")
    mc_root, src_version = params["src_root"], params["src_version"]
    t_root, t_version = params["target_root"], params["target_version"]
    t_loader, t_mc = params["target_loader"], params["target_mc"]

    log_lines = []
    orig_log = cfg.log
    cfg.log = Logger(lambda m, l="info": (getattr(orig_log, l)(m), log_lines.append(m)))

    PROXY_SETTINGS["use_system"] = cfg.use_system_proxy
    configure_http(cfg.log)
    from .curseforge import configure_cf
    configure_cf(cfg.log)

    if src_version:
        src_client, src_isolated = client_paths(
            mc_root, src_version, force_isolated=params.get("src_force_isolated", False))
        src_loader = detect_loader(mc_root, src_version)
        src_desc = "%s%s" % (src_version, "（版本隔离）" if src_isolated else "")
    elif params.get("src_is_server", False):
        src_client, src_isolated = mc_root, False
        src_loader = sniff_server_loader(mc_root)
        src_desc = "服务端根目录"
    else:
        src_client, src_isolated = mc_root, False
        src_loader = None
        src_desc = "根目录（非隔离客户端，mods 在根目录）"
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
    repair_mode = bool(not params.get("src_is_server", False)
                       and same_client
                       and (not src_version or not t_version
                            or src_version == t_version))
    if same_client:
        cfg.log.info("注意: 源与目标是同一目录，进入「更新模组」模式（只更新 mods，不迁移数据）")

    src_mods = os.path.join(src_client, "mods")
    dst_mods = os.path.join(dst_client, "mods")
    if not os.path.isdir(src_mods):
        raise RuntimeError("源目录 mods 不存在: %s" % src_mods)
    os.makedirs(dst_mods, exist_ok=True)

    cfg.log.info("\n=== 模组迁移（源 mods 目录: %s）===" % src_mods)

    report = {"ok": [], "manual": [], "deps": [], "skipped": [], "duplicates": [],
              "checked": []}
    from .graph import ModGraph
    from .versions import base_mc_version
    graph = ModGraph()
    if cfg.migrate_mods:
        src_mc_ver = base_mc_version(src_version) if src_version else ""
        if repair_mode:
            repair_mods_same_dir(dst_mods, t_mc, t_loader, cfg, report, graph)
        else:
            migrate_mods(src_mods, dst_mods, t_mc, t_loader, cfg, report, stop_event, graph,
                         src_mc_version=src_mc_ver)
    else:
        cfg.log.info("未勾选「迁移 mod」，跳过模组迁移")

    if not same_client:
        migrate_game_data(src_client, dst_client, cfg)

    if cfg.print_failures and report["manual"]:
        print_failure_links(report["manual"], cfg.log)

    if graph.mods:
        n_mods, n_deps, n_confs = graph.summary()
        cfg.log.info("依赖图统计: %d 个模组, %d 条依赖, %d 条冲突" % (n_mods, n_deps, n_confs))
        auto_resolve_conflicts(graph, t_mc, t_loader, dst_mods, cfg, report)
        removed = resolve_conflicts(graph, cfg)
        if removed:
            report["ok"] = [(n, f) for n, f in report["ok"] if f not in removed]

    migrate_big_folders(cfg)
    elapsed = time.monotonic() - _t0
    report["elapsed"] = elapsed
    report["migrated_dirs"] = (([("mods", src_mods, dst_mods)]
                                if cfg.migrate_mods and not repair_mode else [])
                               + list(cfg.migrated_dirs))
    report["log"] = log_lines
    cfg.log.info("总耗时: %s" % _fmt_dur(elapsed))
    src_kind = "服务端" if params.get("src_is_server", False) else (
        "版本隔离" if src_isolated else "非隔离")
    dst_kind = ("服务端" if (t_version is None and is_server_root(dst_client))
                else ("版本隔离" if dst_isolated else "非隔离"))
    src_desc = "%s（%s）" % (src_client, src_kind)
    dst_desc = "%s（%s）" % (dst_client, dst_kind)
    return report, same_client, src_desc, dst_desc


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
    if report.get("checked"):
        print("已检查现有模组 %d 个：" % len(report["checked"]))
        for name, jname in sorted(report["checked"], key=lambda x: str(x[0]).lower()):
            print("  %s（%s）" % (name, jname))
    print("成功下载 %d 个模组：" % len(report["ok"]))
    for name, fname in sorted(report["ok"], key=lambda x: str(x[0]).lower()):
        print("  %s -> %s" % (name, fname))
    if report["skipped"]:
        print("跳过 %d 个（无法解析）：" % len(report["skipped"]))
        for j, r in sorted(report["skipped"]):
            print("  - %s（%s）" % (j, r))
    if report.get("duplicates"):
        print("重复项目合并 %d 个（同一项目只下载一次）：" % len(report["duplicates"]))
    if report["deps"]:
        print("依赖：")
        for d in sorted(report["deps"]):
            print("  %s" % d)
    if report["manual"]:
        print("%d 个模组需要手动处理：" % len(report["manual"]))
        for jname, meta, why in sorted(report["manual"],
                                       key=lambda x: str((x[1] or {}).get("name")
                                                         or (x[1] or {}).get("id")
                                                         or x[0]).lower()):
            q = meta.get("name") or meta.get("id") or jname
            print("  - %s（%s）" % (jname, why))
            print("    搜索: https://modrinth.com/discover/mods?q=%s" % quote(q))
    if same_client:
        print("（同客户端更新模式，未迁移数据目录）")


def write_report_file(report, src_desc, dst_desc):
    import html as _html
    from urllib.parse import quote
    now = datetime.now()

    def esc(s):
        return _html.escape(str(s))

    def _mkey(x):
        jname, meta, _why = x
        return str((meta or {}).get("name") or (meta or {}).get("id") or jname).lower()

    ok_html = "".join(
        '<div class="ok-item"><span class="ok-name">%s</span><span class="ok-file">%s</span></div>'
        % (esc(n), esc(f))
        for n, f in sorted(report["ok"], key=lambda x: str(x[0]).lower()))
    checked_html = "".join(
        '<div class="ok-item"><span class="ok-name">%s</span><span class="ok-file">%s</span></div>'
        % (esc(n), esc(j))
        for n, j in sorted(report.get("checked", []), key=lambda x: str(x[0]).lower()))
    skip_html = "".join(
        '<div class="manual-item"><span class="manual-name">%s</span>'
        '<span class="manual-reason">%s</span></div>'
        % (esc(j), esc(r)) for j, r in sorted(report["skipped"]))
    dep_html = "".join('<div class="dep-item">%s</div>' % esc(d)
                       for d in sorted(report["deps"]))
    manual_html = "".join(
        '<div class="manual-item"><span class="manual-name">%s</span>'
        '<span class="manual-file">%s</span>'
        '<span class="manual-reason">%s</span>'
        '<a class="manual-link" href="https://modrinth.com/discover/mods?q=%s" target="_blank">Modrinth 搜索</a></div>'
        % (esc((meta or {}).get("name") or (meta or {}).get("id") or jname),
           esc(jname), esc(why),
           quote((meta or {}).get("name") or (meta or {}).get("id") or jname))
        for jname, meta, why in sorted(report["manual"], key=_mkey))

    def card(title, color, body):
        if not body:
            return ""
        return ('<div class="card"><h2 style="border-left:4px solid %s">%s</h2>%s</div>'
                % (color, esc(title), body))

    dirs_html = "".join(
        '<div class="dir-item"><span class="dir-name">%s</span>'
        '<span class="dir-path">%s</span><span class="dir-arrow">→</span>'
        '<span class="dir-path">%s</span></div>'
        % (esc(label), esc(src), esc(dst))
        for label, src, dst in report.get("migrated_dirs", []))
    log_lines = report.get("log", [])
    log_details = ('<details class="card"><summary class="log-summary">完整日志（%d 行，点击展开）</summary>'
                   '<pre class="log">%s</pre></details>'
                   % (len(log_lines), esc("\n".join(log_lines))))

    if report.get("checked"):
        stat_first = '<div class="stat"><b>%d</b><span>已检查模组</span></div>' % len(report["checked"])
    else:
        stat_first = '<div class="stat"><b>%d</b><span>成功下载</span></div>' % len(report["ok"])
    stats = ('<div class="card stats">%s'
             '<div class="stat warn"><b>%d</b><span>需手动处理</span></div>'
             '<div class="stat"><b>%d</b><span>依赖</span></div>'
             '<div class="stat"><b>%d</b><span>跳过</span></div></div>'
             % (stat_first, len(report["manual"]), len(report["deps"]),
                len(report["skipped"])))

    page = ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<title>MC 模组迁移报告</title>\n<style>\n'
            'body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;background:#f4f5f7;color:#222;margin:0;padding:28px}\n'
            '.wrap{max-width:920px;margin:0 auto}\n'
            'h1{font-size:22px;margin:0 0 6px}\n'
            '.meta{color:#666;font-size:13px;margin-bottom:8px;line-height:1.7}\n'
            '.card{background:#fff;border-radius:10px;padding:18px 22px;margin:14px 0;box-shadow:0 1px 4px rgba(0,0,0,.07)}\n'
            'h2{font-size:15px;margin:0 0 12px;padding-left:10px}\n'
            '.stats{display:flex;gap:14px;flex-wrap:wrap}\n'
            '.stat{flex:1;min-width:120px;background:#fafbfc;border-radius:8px;padding:12px;text-align:center}\n'
            '.stat b{display:block;font-size:24px;color:#222}\n'
            '.stat span{color:#777;font-size:12px}\n'
            '.stat.warn b{color:#c99700}\n'
            '.dir-item{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;padding:6px 4px;border-bottom:1px dashed #eee;font-size:13px}\n'
            '.dir-name{font-weight:700;color:#2f6f4f}\n'
            '.dir-path{color:#888;font-size:12px;word-break:break-all}\n'
            'details.card{padding:14px 22px}\n'
            'summary.log-summary{cursor:pointer;font-weight:700;color:#333;outline:none}\n'
            'pre.log{background:#f8f9fa;border:1px solid #eee;border-radius:6px;padding:12px;'
            'font-size:12px;line-height:1.6;max-height:420px;overflow:auto;white-space:pre-wrap;'
            'word-break:break-all;margin-top:12px}\n'
            '.dir-arrow{color:#bbb}\n'
            '.ok-item,.skip-item,.dup-item,.dep-item{padding:6px 4px;border-bottom:1px dashed #eee;font-size:13px}\n'
            '.ok-name{font-weight:600;color:#1a7f37}\n'
            '.ok-file{color:#888;margin-left:8px}\n'
            '.skip-item{color:#888}\n'
            '.dup-item{color:#5b6b8c}\n'
            '.manual-item{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;padding:9px 12px;margin:7px 0;'
            'border-left:4px solid #eab308;background:#fffdf2;border-radius:6px;font-size:13px}\n'
            '.manual-name{font-weight:700;color:#a16207;background:#fde68a;padding:1px 8px;border-radius:4px}\n'
            '.manual-file{color:#888;font-size:12px}\n'
            '.manual-reason{color:#000}\n'
            '.manual-link{color:#0b5cad;text-decoration:none;font-size:12px;margin-left:auto}\n'
            '.manual-link:hover{text-decoration:underline}\n'
            '</style>\n</head>\n<body><div class="wrap">\n'
            '<h1>MC 模组迁移报告</h1>\n'
            '<div class="meta">时间：%s　耗时：%s<br>源：%s<br>目标：%s</div>\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n'
            '</div></body></html>'
            % (now.strftime("%Y-%m-%d %H:%M:%S"), _fmt_dur(report.get("elapsed") or 0),
               esc(src_desc), esc(dst_desc), stats,
               card("迁移目录", "#2f6f4f", dirs_html),
               card("已检查模组", "#2f6f4f", checked_html),
               card("成功下载", "#2ea043", ok_html),
               card("需手动处理", "#eab308", manual_html),
               card("依赖", "#5b6b8c", dep_html),
               card("跳过", "#888", skip_html),
               log_details))

    fname = os.path.join(os.getcwd(), "迁移报告_%s.html" % now.strftime("%Y%m%d_%H%M%S"))
    with open(fname, "w", encoding="utf-8") as f:
        f.write(page)
    return fname
