import os
import urllib.request

USER_AGENT = "mc-mod-migrator/1.0 (local personal tool)"

LOADERS = ["fabric", "quilt", "forge", "neoforge"]
LOADER_LABEL = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}
LOADER_MARKERS = [
    ("fabric", "fabric-loader"),
    ("quilt", "quilt-loader"),
    ("neoforge", "net.neoforged"),
    ("forge", "net.minecraftforge"),
]

KNOWN_DIRS = {
    "assets", "libraries", "versions", "mods", "config", "saves", "world",
    "resourcepacks", "shaderpacks", "screenshots", "logs", "crash-reports",
    "server-resource-packs", "stats", "achievements", "texturepacks",
    "webcache", "temp", "dumps", "downloads",
}
KNOWN_FILES = {
    "options.txt", "optionsof.txt", "usercache.json", "usernamecache.json",
    "launcher_profiles.json", "launcher_accounts.json", "servers.dat",
    "realms_persistence.json", "global_resource_packs.json",
    "commandhistory.txt", "splashes.txt", "hotbar.nbt",
    "allowed_schematics.txt", "banned-ips.json", "banned-players.json",
    "server.properties", "eula.txt", "whitelist.json", "ops.json",
    "server-icon.png", "version_history.json",
}
OPTIONAL_DIRS = ["resourcepacks", "shaderpacks", "texturepacks"]
OPTIONAL_FILES = ["servers.dat", "servers.dat_old", "optionsof.txt"]

SERVER_FILES = [
    "server.properties", "eula.txt", "whitelist.json", "ops.json",
    "banned-players.json", "banned-ips.json", "server-icon.png",
    "version_history.json",
]

CHOICE_KEYS = ["config", "options", "saves", "stray", "optional", "server"]
CHOICE_LABELS = {
    "config": "config 目录",
    "options": "游戏设置",
    "saves": "存档",
    "stray": "模组生成的杂项目录",
    "optional": "资源包 / 光影",
    "server": "服务端配置文件",
}

HIGH_CONF = 0.85
ASK_CONF = 0.60


def human_size(n):
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
    return "%.1f GB" % n


def default_mc_root():
    cands = [
        os.path.expandvars(r"%APPDATA%\.minecraft"),
        os.path.expanduser("~/Library/Application Support/minecraft"),
        os.path.expanduser("~/.minecraft"),
    ]
    return next((c for c in cands if os.path.isdir(c)), cands[0])

class Logger:

    def __init__(self, sink):
        self._sink = sink

    def info(self, msg):
        self._sink(str(msg), "info")

    def warn(self, msg):
        self._sink(str(msg), "warn")

    def error(self, msg):
        self._sink(str(msg), "error")


def plain_log_sink(msg):
    print(msg)

PROXY_SETTINGS = {"use_system": True, "manual": ""}

def effective_proxies():
    if PROXY_SETTINGS["manual"]:
        m = PROXY_SETTINGS["manual"].strip()
        return {"http": m, "https": m} if m else None
    if PROXY_SETTINGS["use_system"]:
        p = urllib.request.getproxies() or {}
        return p or None
    return {}
