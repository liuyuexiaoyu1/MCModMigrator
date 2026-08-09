import os
import re
import sys

from .core import (CHOICE_KEYS, CHOICE_LABELS, LOADERS, LOADER_LABEL,
                   default_mc_root)
from .clients import (detect_loader, is_server_root, list_clients,
                      resolve_version_dir, sniff_server_loader)
from .migrator import RunConfig, print_summary, run_migration, write_report_file
from .versions import detect_target_mc, fetch_mc_versions


def die(msg):
    print("错误: " + msg)
    sys.exit(1)


def ask(prompt, default=None):
    d = " [%s]" % default if default is not None else ""
    while True:
        v = input("%s%s: " % (prompt, d)).strip().strip('"')
        if v:
            return v
        if default is not None:
            return default


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input("%s [%s] " % (prompt, hint)).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "是", "好的", "确定"):
            return True
        if raw in ("n", "no", "否", "取消"):
            return False


def pick_from_list(title, items, default_index=None):
    print(title)
    for i, it in enumerate(items, 1):
        print("  [%d] %s" % (i, it))
    suffix = " (回车默认 %d)" % (default_index + 1) if default_index is not None else ""
    while True:
        raw = input("  请输入编号 1-%d%s: " % (len(items), suffix)).strip()
        if not raw and default_index is not None:
            return default_index
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
        print("  输入无效，请重试")


def choose_loader(prompt, default=None):
    labels = [LOADER_LABEL[l] for l in LOADERS]
    d = LOADERS.index(default) if default in LOADERS else None
    return LOADERS[pick_from_list(prompt, labels, d)]


def ask_path(prompt, default):
    print("%s [%s]:" % (prompt, default), end=" ")
    raw = input().strip().strip('"')
    return raw or default


def make_cli_log():
    ansi = False
    try:
        if sys.stdout.isatty():
            if os.name == "nt":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                h = kernel32.GetStdHandle(-11)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(h, mode.value | 0x0004)
            ansi = True
    except Exception:
        ansi = False

    def sink(msg, level="info"):
        if ansi:
            code = {"error": "31", "warn": "33"}.get(level)
            print(("\033[%sm%s\033[0m" % (code, msg)) if code else msg)
        else:
            print(msg)

    return sink


def cli_pick_mc_version():
    try:
        releases, all_ids = fetch_mc_versions()
    except Exception:
        print("无法联网加载 Mojang 版本列表，请手动输入")
        return ask("目标 MC 版本（如 1.20.1）")
    ids = releases or all_ids
    print("已从 Mojang 加载 %d 个正式版（输入 all 可查看含快照的全部版本）" % len(ids))
    for i, v in enumerate(ids[:80], 1):
        print("  [%d] %s" % (i, v))
    while True:
        raw = input("选择目标 MC 版本（编号 / 输入版本号 / all）: ").strip()
        if raw.lower() == "all":
            print("全部版本（含快照）：")
            for i, v in enumerate(all_ids, 1):
                print("  [%d] %s" % (i, v))
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(ids):
            return ids[int(raw) - 1]
        if re.match(r"^\d+\.\d+", raw):
            return raw.split("-")[0]
        print("输入无效，请重试")


def run_cli(args):
    from .core import Logger
    log = Logger(make_cli_log())
    confirm = lambda p: ask_yes_no(p, default=True)

    mc_root = args.src_root or ask_path("源目录（.minecraft 根目录 / versions 下版本隔离目录 / 服务端根目录）",
                                        default_mc_root())
    if not os.path.isdir(mc_root):
        die("源目录不存在: %s" % mc_root)
    vd = resolve_version_dir(mc_root) if not args.src_version else None
    src_force_isolated = False
    if vd:
        mc_root, src_version = vd
        src_force_isolated = True
        src_is_server = False
        src_loader = detect_loader(mc_root, src_version)
        print("检测到版本隔离目录，自动使用客户端: %s" % src_version)
    elif is_server_root(mc_root):
        src_is_server = True
        src_version = None
        src_loader = sniff_server_loader(mc_root)
        print("检测到服务端根目录")
    else:
        src_is_server = False
        clients = list_clients(mc_root)
        if not clients:
            die("未在 %s 下找到任何版本客户端，也未检测到服务端结构" % mc_root)
        if args.src_version:
            src_version = args.src_version
        else:
            items = ["%s  [%s]" % (n, LOADER_LABEL[l]) if l else n for n, l in clients]
            src_version = clients[pick_from_list("选择源客户端（已装加载器）:", items)][0]
        src_loader = detect_loader(mc_root, src_version)
        
    if args.target_loader:
        t_loader = args.target_loader
    else:
        t_loader = choose_loader("目标加载器（回车默认源加载器）:", src_loader or "fabric")

    t_force_isolated = False
    if args.overwrite:
        if not src_is_server:
            die("--overwrite 仅用于服务端（源目录需含 server.properties / eula.txt）")
        t_root, t_version, t_is_server = mc_root, None, True
        print("覆盖模式：直接在源服务端目录更新 mods")
    else:
        t_root = args.target_root or ask_path("目标目录（.minecraft 根目录 / versions 版本隔离目录 / 服务端根目录）", mc_root)
        t_vd = resolve_version_dir(t_root) if not args.target_version else None
        if t_vd:
            t_root, t_version = t_vd
            t_force_isolated = True
            t_is_server = False
            print("检测到目标版本隔离目录，自动使用客户端: %s" % t_version)
        elif is_server_root(t_root):
            t_is_server = True
            t_version = None
            print("检测到目标为服务端根目录")
        else:
            t_is_server = False
            if args.target_version:
                t_version = args.target_version
            else:
                t_clients = list_clients(t_root)
                if t_clients:
                    items = ["%s  [%s]" % (n, LOADER_LABEL[l]) if l else n for n, l in t_clients]
                    items.insert(0, "(不使用版本目录，mods 放在根目录)")
                    idx = pick_from_list("选择目标客户端版本:", items)
                    t_version = t_clients[idx - 1][0] if idx > 0 else None
                else:
                    t_version = None
                    print("目标根目录下没有发现版本，将按非隔离客户端处理")

    if src_is_server != t_is_server:
        die("仅支持同类型迁移：服务端→服务端 (S2S) 或 客户端→客户端 (C2C)，"
            "源与目标类型不一致")

    if args.target_mc:
        t_mc = args.target_mc
    elif args.overwrite:
        print("覆盖模式需手动选择目标 MC 版本")
        t_mc = cli_pick_mc_version()
    else:
        t_mc = detect_target_mc(t_root, t_version) if not t_is_server else ""
        if t_mc:
            print("已自动读取目标版本: %s" % t_mc)
        else:
            t_mc = cli_pick_mc_version()
    t_mc = t_mc.split("-")[0].strip()
    if not re.match(r"^\d+\.\d+", t_mc):
        die("目标 MC 版本格式不合法: %s" % t_mc)

    if args.overwrite:
        choices = {k: False for k in CHOICE_KEYS}
        print("覆盖模式：仅更新 mods，不迁移 config/saves/杂项目录等任何数据")
    elif args.migrate:
        keys = {k.strip() for k in args.migrate.split(",") if k.strip() in CHOICE_KEYS}
        choices = {k: (k in keys) for k in CHOICE_KEYS}
    elif args.skip_data:
        choices = {k: False for k in CHOICE_KEYS}
    elif args.yes:
        choices = {k: True for k in CHOICE_KEYS}
    else:
        choices = {}
        for k in CHOICE_KEYS:
            if k == "server" and not src_is_server:
                choices[k] = False
                continue
            label = CHOICE_LABELS[k]
            if k == "saves" and src_is_server:
                label = "world 存档目录（服务端）"
            if k == "optional":
                label += "（资源包/光影/服务器列表）"
            choices[k] = ask_yes_no("是否迁移 %s？" % label, default=(k != "optional"))

    def on_conflicts(conflicts):
        if args.yes:
            return "skip"
        for item in conflicts:
            print("模组 %s：被 %s 依赖；与 %s 冲突" % (
                item.get("name") or item.get("mod"),
                "、".join(item.get("dependents", [])),
                "、".join(item.get("conflicting", []))))
        print("1) 删除模组及依赖它的模组  2) 删除冲突的模组  3) 忽略")
        while True:
            raw = input("请选择 (1/2/3): ").strip()
            if raw in ("1", "2", "3"):
                return {"1": "delete_c", "2": "delete_conflicts", "3": "skip"}[raw]

    cfg = RunConfig(auto_yes=args.yes, skip_deps=args.skip_deps,
                    choices=choices,
                    use_system_proxy=not args.no_system_proxy,
                    download_threads=args.threads,
                    analysis_threads=args.analysis_threads,
                    print_failures=not args.no_failures,
                    ignore_fork=args.ignore_fork,
                    log=log, confirm=confirm)
    cfg.on_conflicts = on_conflicts
    params = {"src_root": mc_root, "src_version": src_version,
              "target_root": t_root, "target_version": t_version,
              "src_force_isolated": src_force_isolated,
              "target_force_isolated": t_force_isolated,
              "target_loader": t_loader, "target_mc": t_mc,
              "stop_event": None}
    report, same_client = run_migration(params, cfg)
    print_summary(report, same_client)
    try:
        f = write_report_file(report,
                              (mc_root + "/" + src_version) if src_version else (mc_root + " (服务端)"),
                              (t_root + "/" + t_version) if t_version else (t_root + " (服务端)" if t_is_server else t_root))
        print("报告已保存: %s" % f)
    except OSError as e:
        print("报告保存失败: %s" % e)
