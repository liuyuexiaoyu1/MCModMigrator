import json
import os
import re

import requests

from .core import USER_AGENT

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def fetch_mc_versions():
    r = requests.get(MANIFEST_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    releases, all_ids = [], []
    for v in r.json().get("versions", []):
        vid = v.get("id")
        if not vid:
            continue
        if v.get("type") == "release" and vid not in releases:
            releases.append(vid)
        if vid not in all_ids:
            all_ids.append(vid)
    return releases, all_ids


_dates_cache = None


def mc_release_date(mc_version):
    global _dates_cache
    if _dates_cache is None:
        _dates_cache = {}
        try:
            r = requests.get(MANIFEST_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            for v in r.json().get("versions", []):
                if v.get("id") and v.get("releaseTime"):
                    _dates_cache.setdefault(v["id"], v["releaseTime"])
        except Exception:
            pass
    return _dates_cache.get(mc_version)


def base_mc_version(name, known_ids=None):
    name = (name or "").strip()
    if not name:
        return ""
    for vid in known_ids or []:
        if name == vid or name.startswith(vid + "-"):
            return vid
    m = re.match(r"^(\d+\.\d+(?:\.\d+)*)", name)
    return m.group(1) if m else ""


def detect_target_mc(root, version):
    if not version:
        return ""
    vname = version
    vj = os.path.join(root, "versions", version, version + ".json")
    if os.path.exists(vj):
        try:
            with open(vj, "r", encoding="utf-8", errors="ignore") as f:
                vname = json.load(f).get("id") or version
        except (ValueError, OSError):
            pass
    return base_mc_version(vname)
