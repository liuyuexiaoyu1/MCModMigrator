import sys

__version__ = "1.0.0"

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行:  pip install requests")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from .core import (ASK_CONF, CHOICE_KEYS, CHOICE_LABELS, HIGH_CONF, KNOWN_DIRS,
                   KNOWN_FILES, LOADERS, LOADER_LABEL, LOADER_MARKERS,
                   OPTIONAL_DIRS, OPTIONAL_FILES, PROXY_SETTINGS, SERVER_FILES,
                   USER_AGENT, Logger, default_mc_root, effective_proxies,
                   human_size)
from .clients import (client_paths, detect_loader, is_server_root, list_clients,
                      resolve_version_dir, sniff_server_loader)
from .mod_parser import (is_wrapper_meta, parse_mod_jar, parse_mods_toml,
                         sha1_of, wrapper_base_id)
from .modrinth import (Downloader, authors_equal, collect_deps,
                       configure_http, lookup_project_links,
                       match_to_project, mr_download_file, mr_get,
                       mr_lookup_sha1, mr_search, mr_versions,
                       parse_retry_after, pick_version, pick_version_in_range,
                       primary_filename, project_members, resolve_via_mcmod,
                       score_hit, ver_compatible)
from .versions import base_mc_version, detect_target_mc, fetch_mc_versions
from .graph import DEFAULT_DEPS, ModGraph, version_satisfies
from .migrator import (RunConfig, copy_saves_merge, copy_tree_missing,
                       copy_tree_overwrite, dir_size, find_stray,
                       migrate_game_data, migrate_mods, print_failure_links,
                       print_summary, resolve_conflicts, run_migration,
                       write_report_file)
from .cli import (ask, ask_path, ask_yes_no, choose_loader, cli_pick_mc_version,
                  pick_from_list, run_cli)
from .gui import HAVE_QT, MainWindow, MigrateWorker, VersionFetcher, run_gui

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    QtCore = QtGui = QtWidgets = None
