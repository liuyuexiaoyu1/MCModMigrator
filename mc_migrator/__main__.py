import argparse
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from mc_migrator.core import CHOICE_KEYS, LOADERS
from mc_migrator.cli import run_cli


def parse_args():
    p = argparse.ArgumentParser(description="MC 模组迁移 / 更新工具（默认图形界面）")
    p.add_argument("--cli", action="store_true", help="使用命令行模式")
    p.add_argument("--src-root", help="源 .minecraft 根目录或服务端根目录")
    p.add_argument("--src-version", help="源客户端版本名（C2C）")
    p.add_argument("--target-root", help="目标 .minecraft 根目录或服务端根目录")
    p.add_argument("--target-version", default="", help="目标客户端版本名（留空=mods 放根目录）")
    p.add_argument("--target-loader", choices=LOADERS, help="目标加载器")
    p.add_argument("--target-mc", help="目标 Minecraft 版本，如 1.20.1（省略时客户端自动读取/清单选择）")
    p.add_argument("--yes", action="store_true", help="自动确认所有询问")
    p.add_argument("--threads", type=int, default=4, help="并发下载线程数 (1-16，默认 4)")
    p.add_argument("--no-system-proxy", action="store_true", help="不使用系统代理（直连）")
    p.add_argument("--no-failures", action="store_true",
                   help="不打印匹配失败清单（默认完成后打印并附开源链接）")
    p.add_argument("--skip-deps", action="store_true", help="不自动安装依赖")
    p.add_argument("--skip-data", action="store_true", help="不迁移任何数据目录")
    p.add_argument("--migrate", help="仅迁移指定数据类别，逗号分隔: " + ",".join(CHOICE_KEYS))
    p.add_argument("--overwrite", action="store_true",
                   help="服务端覆盖模式：直接在源服务端目录更新 mods（需手动指定/选择 --target-mc）")
    return p.parse_args()


def main():
    from mc_migrator.gui import HAVE_QT, run_gui
    args = parse_args()
    if args.cli:
        run_cli(args)
    elif HAVE_QT:
        run_gui(args)
    else:
        print("未安装 PySide6，请先执行:  pip install PySide6")
        print("或使用命令行模式:  python mod_migrator.py --cli ...")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(1)
    except requests.RequestException as e:
        print("\n网络错误: %s" % e)
        sys.exit(1)
