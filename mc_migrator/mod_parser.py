import hashlib
import io
import json
import os
import re
import zipfile
from collections import Counter

WRAPPER_SUFFIX = re.compile(r"(?:^|[_-])wrapper$", re.IGNORECASE)
_JAR_VERSION_SUFFIX = re.compile(r"(?:[-_](?:mc)?\d[\w.+\-]*)+$")
_META_INF_PREFIX = "meta-inf/"


def parse_mods_toml(text):
    entries = []
    cur = None
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[\[(.+?)\]\]$", line)
        if m:
            section = m.group(1)
            cur = {}
            entries.append((section, cur))
            continue
        m = re.match(r"^\[(.+?)\]$", line)
        if m:
            section = m.group(1)
            cur = None
            continue
        if cur is None or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in ("modId", "displayName", "version", "library", "authors",
                 "mandatory", "versionRange"):
            cur[k] = v.strip().strip('"').strip("'")
    return entries


def _loads_tolerant(text):
    return json.loads(text, strict=False)


def _extract_authors(data):
    authors = data.get("authors")
    out = []
    if isinstance(authors, str):
        out = [authors]
    elif isinstance(authors, list):
        for a in authors:
            if isinstance(a, str):
                out.append(a)
            elif isinstance(a, dict):
                nm = a.get("name")
                if nm:
                    out.append(str(nm))
    return [str(x).strip() for x in out if str(x).strip()]


def _extract_relations(data):
    deps, conflicts = [], []
    for key, out in (("depends", deps), ("conflicts", conflicts)):
        rel = data.get(key) or {}
        if isinstance(rel, dict):
            for mid, val in rel.items():
                rng = val.get("version") if isinstance(val, dict) else val
                out.append((str(mid), str(rng or "*")))
    return deps, conflicts


def _parse_from_zip(z):
    names = set(z.namelist())

    if "fabric.mod.json" in names:
        data = _loads_tolerant(z.read("fabric.mod.json"))
        mid = str(data.get("id") or "").strip()
        if not mid:
            return None
        deps, conflicts = _extract_relations(data)
        return {"id": mid,
                "name": str(data.get("name") or mid).strip(),
                "version": str(data.get("version") or "").strip(),
                "kind": "fabric", "library": False,
                "entrypoints": bool(data.get("entrypoints")),
                "authors": _extract_authors(data),
                "deps": deps, "conflicts": conflicts}

    if "quilt.mod.json" in names:
        data = _loads_tolerant(z.read("quilt.mod.json"))
        mid = str(data.get("id") or "").strip()
        if not mid:
            return None
        deps, conflicts = _extract_relations(data)
        return {"id": mid,
                "name": str(data.get("name") or mid).strip(),
                "version": str(data.get("version") or "").strip(),
                "kind": "quilt", "library": False,
                "entrypoints": bool(data.get("entrypoints")),
                "authors": _extract_authors(data),
                "deps": deps, "conflicts": conflicts}

    for toml_name, kind in (("META-INF/neoforge.mods.toml", "neoforge"),
                            ("META-INF/mods.toml", "forge")):
        if toml_name in names:
            toml_entries = parse_mods_toml(z.read(toml_name).decode("utf-8", "replace"))
            for section, e in toml_entries:
                if section != "mods" or not e.get("modId"):
                    continue
                authors = [a.strip() for a in str(e.get("authors") or "").split(",") if a.strip()]
                deps = [(d.get("modId"), d.get("versionRange") or "*")
                        for s2, d in toml_entries
                        if s2.startswith("dependencies.") and d.get("modId")
                        and d.get("mandatory", "true").lower() != "false"]
                return {"id": e["modId"],
                        "name": e.get("displayName") or e["modId"],
                        "version": e.get("version") or "",
                        "kind": kind,
                        "library": (e.get("library") or "").lower() == "true",
                        "entrypoints": False,
                        "authors": authors,
                        "deps": deps, "conflicts": []}
            return None

    if "mcmod.info" in names:
        try:
            data = _loads_tolerant(z.read("mcmod.info").decode("utf-8", "replace"))
            rows = data if isinstance(data, list) else data.get("modlist", [])
            for e in rows:
                if not e.get("modid"):
                    continue
                al = e.get("authorList") or []
                authors = [str(x) for x in al if str(x).strip()]
                deps = [(str(x), "*") for x in (e.get("requiredMods") or []) if str(x).strip()]
                conflicts = [(str(x), "*") for x in (e.get("conflicts") or []) if str(x).strip()]
                return {"id": str(e["modid"]),
                        "name": str(e.get("name") or e["modid"]),
                        "version": str(e.get("version") or ""),
                        "kind": "forge-legacy", "library": False,
                        "entrypoints": False,
                        "authors": authors,
                        "deps": deps, "conflicts": conflicts}
        except (ValueError, TypeError):
            pass
    return None


def _jar_stem(name):
    base = os.path.basename(name)
    if base.lower().endswith(".jar"):
        base = base[:-4]
    return _JAR_VERSION_SUFFIX.sub("", base).lower()


def _stem_groups(nested):
    groups = []
    for m in nested:
        s = _jar_stem(m.get("nested_path") or "")
        for g in groups:
            if len(os.path.commonprefix([g[0], s])) >= 6:
                g[1].append(m)
                break
        else:
            groups.append([s, [m]])
    return groups


def _similar_nested_count(nested):
    return max((len(g[1]) for g in _stem_groups(nested)), default=0)


def _representative_nested(nested):
    if not nested:
        return None
    return max(_stem_groups(nested), key=lambda g: len(g[1]))[1][0]


def _collect_nested_metas(z):
    out = []
    for n in sorted(z.namelist()):
        if not n.lower().startswith(_META_INF_PREFIX):
            continue
        if not n.lower().endswith(".jar"):
            continue
        try:
            info = z.getinfo(n)
            if info.file_size > 64 * 1024 * 1024:
                continue
            raw = z.read(n)
            inner = zipfile.ZipFile(io.BytesIO(raw))
            m = _parse_from_zip(inner)
        except Exception:
            continue
        if not m:
            continue
        m["nested_path"] = n
        m["sha1"] = hashlib.sha1(raw).hexdigest()
        out.append(m)
    return out


def _dedup_nested(nested):
    seen, out = set(), []
    for m in nested:
        key = (m.get("id"), m.get("name"))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def parse_mod_jar(path):
    try:
        with zipfile.ZipFile(path) as z:
            meta = _parse_from_zip(z)
            nested = _collect_nested_metas(z)
            if meta is None:
                return nested[0] if nested else None
            if nested:
                meta["nested"] = _dedup_nested(nested)
            meta["wrapper"] = (not meta.get("entrypoints")) and _similar_nested_count(nested) >= 2
            if meta["wrapper"]:
                rep = _representative_nested(nested)
                meta["nested"] = [rep] if rep else []
            return meta
    except Exception:
        return None


def is_wrapper_meta(meta):
    return bool(meta and meta.get("wrapper"))


def wrapper_base_id(meta):
    mid = meta.get("id") or ""
    m = WRAPPER_SUFFIX.search(mid)
    return mid[:m.start()] if m else mid


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
