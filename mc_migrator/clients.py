import os

from .core import LOADER_MARKERS
from .mod_parser import parse_mod_jar


def list_clients(mc_root):
    out = []
    vdir = os.path.join(mc_root, "versions")
    if os.path.isdir(vdir):
        for name in sorted(os.listdir(vdir)):
            if os.path.isdir(os.path.join(vdir, name)):
                out.append((name, detect_loader(mc_root, name)))
    return out


def detect_loader(mc_root, version):
    vj = os.path.join(mc_root, "versions", version, version + ".json")
    if not os.path.exists(vj):
        return None
    try:
        with open(vj, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    for loader, marker in LOADER_MARKERS:
        if marker in text:
            return loader
    return None


def client_paths(mc_root, version, force_isolated=False):
    vroot = os.path.join(mc_root, "versions", version)
    isolated = force_isolated or any(
        os.path.exists(os.path.join(vroot, x))
        for x in ("mods", "config", "saves", "options.txt")
    )
    return (vroot if isolated else mc_root), isolated


def resolve_version_dir(path):
    if not os.path.isdir(path):
        return None
    path = path.rstrip("\\/")
    name = os.path.basename(path)
    parent = os.path.dirname(path)
    if os.path.basename(parent).lower() != "versions":
        return None
    if not (os.path.exists(os.path.join(path, name + ".json"))
            or any(os.path.exists(os.path.join(path, x))
                   for x in ("mods", "config", "saves", "options.txt"))):
        return None
    return os.path.dirname(parent), name


def is_server_root(path):
    if not os.path.isdir(path):
        return False
    return (os.path.exists(os.path.join(path, "server.properties"))
            or os.path.exists(os.path.join(path, "eula.txt"))
            or (os.path.isdir(os.path.join(path, "world"))
                and os.path.isdir(os.path.join(path, "mods"))))


_SNIFF_CACHE = {}


def sniff_server_loader(root):
    key = os.path.normcase(os.path.realpath(root)) if root else ""
    if key in _SNIFF_CACHE:
        return _SNIFF_CACHE[key]
    result = None
    mdir = os.path.join(root, "mods")
    if os.path.isdir(mdir):
        kind_to_loader = {"fabric": "fabric", "quilt": "quilt",
                          "forge": "forge", "neoforge": "neoforge"}
        try:
            jars = [f for f in sorted(os.listdir(mdir)) if f.lower().endswith(".jar")][:50]
        except OSError:
            jars = []
        for f in jars:
            meta = parse_mod_jar(os.path.join(mdir, f))
            if meta and meta.get("kind") in kind_to_loader:
                result = kind_to_loader[meta["kind"]]
                break
    _SNIFF_CACHE[key] = result
    return result
