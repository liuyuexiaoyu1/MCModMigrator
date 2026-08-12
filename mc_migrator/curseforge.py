import difflib
import os
import re
import time

import requests

from .core import USER_AGENT, effective_proxies
from .mod_parser import parse_mod_jar

CF_API = "https://api.curseforge.com/v1"
CF_GAME_ID = 432
CF_MOD_CLASS = 6
CF_LOADER_IDS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}

_SESSION = requests.Session()
_logger = None


def configure_cf(log=None):
    global _logger
    _logger = log


def set_api_key(key):
    global CF_API_KEY
    key = (key or "").strip()
    if key:
        CF_API_KEY = key


CF_API_KEY = os.environ.get("CURSEFORGE_API_KEY") or \
    "$2a$10$vzUi1yyCf8oQ6fWeoAqv8.Osj5elqAUSUewRzGmVuGjEE/sjzKZke"


def cf_get(path, params=None):
    if not CF_API_KEY:
        return None
    for attempt in range(3):
        try:
            r = _SESSION.get(CF_API + "/" + path, params=params,
                             headers={"x-api-key": CF_API_KEY, "User-Agent": USER_AGENT},
                             timeout=30, proxies=effective_proxies())
            if r.status_code == 404:
                return None
            if r.status_code == 403:
                if _logger:
                    _logger.warn("CurseForge API Key 无效或已被禁用，请检查设置或环境变量 CURSEFORGE_API_KEY")
                return None
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def cf_post(path, payload):
    if not CF_API_KEY:
        return None
    for attempt in range(3):
        try:
            r = _SESSION.post(CF_API + "/" + path, json=payload,
                              headers={"x-api-key": CF_API_KEY, "User-Agent": USER_AGENT},
                              timeout=30, proxies=effective_proxies())
            if r.status_code == 403:
                if _logger:
                    _logger.warn("CurseForge API Key 无效或已被禁用，请检查设置或环境变量 CURSEFORGE_API_KEY")
                return None
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def cf_search(query, mc_version=None, loader=None, limit=10):
    params = {"gameId": CF_GAME_ID, "classId": CF_MOD_CLASS,
              "searchFilter": query, "pageSize": min(limit, 50), "sortField": 6}
    if mc_version:
        params["gameVersion"] = mc_version
    if loader and loader in CF_LOADER_IDS:
        params["modLoaderType"] = CF_LOADER_IDS[loader]
    data = cf_get("mods/search", params)
    return (data or {}).get("data") or []


def cf_files(mod_id, mc_version=None, loader=None, limit=50):
    params = {"pageSize": min(limit, 50)}
    if mc_version:
        params["gameVersion"] = mc_version
    if loader and loader in CF_LOADER_IDS:
        params["modLoaderType"] = CF_LOADER_IDS[loader]
    data = cf_get("mods/%s/files" % mod_id, params)
    return (data or {}).get("data") or []


def cf_project(mod_id):
    data = cf_get("mods/%s" % mod_id)
    return (data or {}).get("data") or None


def _cf_download_file(cf_file, dest_dir):
    from .modrinth import mr_download_file
    fid = int(cf_file.get("id") or 0)
    url = cf_file.get("downloadUrl") or ("https://edge.forgecdn.net/files/%d/%d/%s"
                                         % (fid // 1000, fid % 1000,
                                            cf_file.get("fileName") or ""))
    return mr_download_file({"files": [{"filename": cf_file.get("fileName") or "mod.jar",
                                        "url": url}]}, dest_dir)


def _murmur2_32(data, seed=1):
    m = 0x5BD1E995
    r = 24
    h = (seed ^ len(data)) & 0xFFFFFFFF
    i, n = 0, len(data)
    while n >= 4:
        k = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
        n -= 4
    if n == 3:
        h ^= data[i + 2] << 16
    if n >= 2:
        h ^= data[i + 1] << 8
    if n >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h & 0xFFFFFFFF


def cf_fingerprint(jpath):
    buf = bytearray()
    with open(jpath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            for b in chunk:
                if b not in (0x09, 0x0A, 0x0D, 0x20):
                    buf.append(b)
    return _murmur2_32(bytes(buf), 1)


def cf_fingerprint_match(jpath):
    try:
        fp = cf_fingerprint(jpath)
        data = cf_post("fingerprints/%d" % CF_GAME_ID, {"fingerprints": [fp]})
        if not data:
            return None
        for m in ((data.get("data") or {}).get("exactMatches") or []):
            f = m.get("file") or {}
            if f.get("modId"):
                return f["modId"], f.get("id"), f
    except Exception:
        pass
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _cf_score(meta, hit):
    mid = _norm(meta.get("id") or "")
    mname = _norm(meta.get("name") or "")
    slug = _norm(hit.get("slug") or "")
    title = _norm(hit.get("name") or "")
    if mid and (mid == slug or mid == title):
        return 1.0
    if mname and (mname == slug or mname == title):
        return 0.95
    scores = []
    if mname and title:
        scores.append(difflib.SequenceMatcher(None, mname, title).ratio())
    if mid and title:
        scores.append(difflib.SequenceMatcher(None, mid, title).ratio())
    return max(scores) if scores else 0.0


def _cf_file_ok(g, mc):
    from .modrinth import ver_compatible
    return g == mc or ver_compatible(g, mc)


def _pick_cf_file(files, mc_version):
    cands = [f for f in files
             if f.get("isAvailable", True)
             and any(_cf_file_ok(g, mc_version) for g in f.get("gameVersions") or [])]
    if not cands:
        return None
    rel = [f for f in cands if f.get("releaseType") == 1]
    pool = rel or cands
    return max(pool, key=lambda f: f.get("fileDate") or "")


def _cf_accept(mid, cf_file, mc_version, compare_dir, note):
    dest, err = _cf_download_file(cf_file, compare_dir)
    if not dest:
        return None
    cm = parse_mod_jar(dest)
    adapted = bool(cm)
    if adapted:
        from .graph import version_satisfies
        for mid2, rng in cm.get("deps", []):
            if mid2 == "minecraft" and not version_satisfies(mc_version, rng):
                adapted = False
                break
    if not adapted:
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    return ("cf:%s" % mid, cf_file.get("fileName") or "mod.jar", note, dest)


def resolve_via_curseforge(meta, mc_version, loader, compare_dir, jar_path=None):
    if not compare_dir:
        return None
    if jar_path:
        fm = cf_fingerprint_match(jar_path)
        if fm:
            mid, _fid, _f = fm
            files = cf_files(mid, mc_version, loader)
            if not files:
                files = cf_files(mid)
            f = _pick_cf_file(files, mc_version)
            if f:
                res = _cf_accept(mid, f, mc_version, compare_dir,
                                 "通过 CurseForge 指纹精确匹配（项目 %s，文件 %s）"
                                 % (mid, f.get("fileName")))
                if res:
                    return res
    query = (meta.get("name") or meta.get("id") or "").strip()
    if not query:
        return None
    try:
        hits = cf_search(query, mc_version, loader)
        if not hits:
            hits = cf_search(query)
    except Exception:
        return None
    if not hits:
        return None
    best, best_s = None, 0.0
    for h in hits:
        s = _cf_score(meta, h)
        if s > best_s:
            best_s, best = s, h
    if best is None or best_s <= 0:
        return None
    mid = best.get("id")
    if not mid:
        return None
    files = cf_files(mid, mc_version, loader)
    if not files:
        files = cf_files(mid)
    f = _pick_cf_file(files, mc_version)
    if not f:
        return None
    return _cf_accept(mid, f, mc_version, compare_dir,
                      "通过 CurseForge 搜索定位（项目 %s，文件 %s）"
                      % (best.get("name"), f.get("fileName")))
