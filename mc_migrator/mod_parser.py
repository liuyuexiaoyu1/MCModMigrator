import hashlib
import io
import json
import re
import zipfile

WRAPPER_SUFFIX = re.compile(r"(?:^|[_-])wrapper$", re.IGNORECASE)


def parse_mods_toml(text):
    entries = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[\[(.+?)\]\]$", line)
        if m:
            cur = {}
            if m.group(1) == "mods":
                entries.append(cur)
            continue
        m = re.match(r"^\[(.+?)\]$", line)
        if m:
            cur = None
            continue
        if cur is None or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in ("modId", "displayName", "version", "library", "authors"):
            cur[k] = v.strip().strip('"').strip("'")
    return entries


def _extract_authors(data):
    """从 fabric/quilt 元数据提取作者名列表（支持 字符串 / [字符串] / [{name: ...}]）"""
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


def _parse_from_zip(z):
    """从已打开的 ZipFile 解析元数据（不做嵌套扫描）"""
    names = set(z.namelist())

    if "fabric.mod.json" in names:
        data = json.loads(z.read("fabric.mod.json"))
        mid = str(data.get("id") or "").strip()
        if not mid:
            return None
        return {"id": mid,
                "name": str(data.get("name") or mid).strip(),
                "version": str(data.get("version") or "").strip(),
                "kind": "fabric", "library": False,
                "authors": _extract_authors(data)}

    if "quilt.mod.json" in names:
        data = json.loads(z.read("quilt.mod.json"))
        mid = str(data.get("id") or "").strip()
        if not mid:
            return None
        return {"id": mid,
                "name": str(data.get("name") or mid).strip(),
                "version": str(data.get("version") or "").strip(),
                "kind": "quilt", "library": False,
                "authors": _extract_authors(data)}

    for toml_name, kind in (("META-INF/neoforge.mods.toml", "neoforge"),
                            ("META-INF/mods.toml", "forge")):
        if toml_name in names:
            for e in parse_mods_toml(z.read(toml_name).decode("utf-8", "replace")):
                if not e.get("modId"):
                    continue
                authors = [a.strip() for a in str(e.get("authors") or "").split(",") if a.strip()]
                return {"id": e["modId"],
                        "name": e.get("displayName") or e["modId"],
                        "version": e.get("version") or "",
                        "kind": kind,
                        "library": (e.get("library") or "").lower() == "true",
                        "authors": authors}
            return None

    if "mcmod.info" in names:
        try:
            data = json.loads(z.read("mcmod.info").decode("utf-8", "replace"))
            rows = data if isinstance(data, list) else data.get("modlist", [])
            for e in rows:
                if not e.get("modid"):
                    continue
                al = e.get("authorList") or []
                authors = [str(x) for x in al if str(x).strip()]
                return {"id": str(e["modid"]),
                        "name": str(e.get("name") or e["modid"]),
                        "version": str(e.get("version") or ""),
                        "kind": "forge-legacy", "library": False,
                        "authors": authors}
        except (ValueError, TypeError):
            pass
    return None


def _collect_nested_metas(z):
    """扫描内嵌 jar（如 META-INF/jars/*.jar），解析其中真实模组的元数据"""
    seen, out = set(), []
    for n in sorted(z.namelist()):
        if not n.lower().endswith(".jar"):
            continue
        try:
            info = z.getinfo(n)
            if info.file_size > 64 * 1024 * 1024:
                continue  # 超大内嵌 jar 跳过，避免解析拖慢主流程
            inner = zipfile.ZipFile(io.BytesIO(z.read(n)))
            m = _parse_from_zip(inner)
        except Exception:
            continue
        if not m:
            continue
        key = (m.get("id"), m.get("name"))
        if key in seen:
            continue
        seen.add(key)
        m["nested_path"] = n
        out.append(m)
    return out


def parse_mod_jar(path):
    """解包 jar 读取模组元数据；wrapper 模组同时提取内嵌真实模组 jar 的元数据。
    返回 {id, name, version, kind, library, nested?: [内嵌元数据]} 或 None（非模组/解析失败）"""
    try:
        with zipfile.ZipFile(path) as z:
            meta = _parse_from_zip(z)
            nested = _collect_nested_metas(z)
            if meta is None:
                # 纯 wrapper：外层没有元数据，用内嵌的第一个真实模组
                return nested[0] if nested else None
            if nested:
                meta["nested"] = nested
            return meta
    except Exception:
        return None


def is_wrapper_meta(meta):
    """外层元数据是否像 wrapper（壳，真实模组在内嵌 jar 里）"""
    if not meta:
        return False
    return bool(WRAPPER_SUFFIX.search(meta.get("id") or "")
                or WRAPPER_SUFFIX.search(meta.get("name") or ""))


def wrapper_base_id(meta):
    """去掉 wrapper 后缀的基底 id：gca_wrapper → gca"""
    mid = meta.get("id") or ""
    m = WRAPPER_SUFFIX.search(mid)
    return mid[:m.start()] if m else mid


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
