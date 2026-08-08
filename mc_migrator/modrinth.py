import difflib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .core import ASK_CONF, USER_AGENT, LOADER_LABEL, effective_proxies, human_size
from .mod_parser import is_wrapper_meta, sha1_of, wrapper_base_id

API_BASE = "https://api.modrinth.com/v2"

_RATE_LOCK = threading.Lock()
_RATE_UNTIL = 0.0
_rate_logger = None


def configure_http(log=None):
    global _rate_logger
    _rate_logger = log


def parse_retry_after(value):
    if not value:
        return 0
    try:
        n = int(float(str(value).strip()))
        return max(1, min(n, 60))
    except ValueError:
        return 5


def _throttle_wait(response):
    global _RATE_UNTIL
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    with _RATE_LOCK:
        now = time.time()
        if now < _RATE_UNTIL:
            time.sleep(_RATE_UNTIL - now)
            return
        if retry_after:
            _RATE_UNTIL = now + retry_after
            if _rate_logger:
                _rate_logger.warn("Modrinth 触发限速，暂停 %d 秒后自动重试" % retry_after)
            time.sleep(retry_after)
        else:
            time.sleep(1.0)


def mr_get(path, params=None, retries=3):
    for attempt in range(retries + 2):
        try:
            r = requests.get(API_BASE + "/" + path, params=params,
                             headers={"User-Agent": USER_AGENT}, timeout=30,
                             proxies=effective_proxies())
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                _throttle_wait(r)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(1.5 * (attempt + 1))


def mr_search(query, mc_version=None, loader=None, limit=10):
    facets = [["project_type:mod"]]
    if mc_version:
        facets.append(["versions:" + mc_version])
    if loader:
        facets.append(["categories:" + loader])
    data = mr_get("search", {"query": query, "facets": json.dumps(facets), "limit": limit})
    return (data or {}).get("hits", [])


def mr_versions(project_id, mc_version=None, loader=None):
    params = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader:
        params["loaders"] = json.dumps([loader])
    data = mr_get("project/%s/version" % project_id, params) or []
    data.sort(key=lambda v: v.get("date_published") or "", reverse=True)
    return data


def mr_lookup_sha1(sha1):
    return mr_get("version_file/%s" % sha1, {"algorithm": "sha1"})


def primary_filename(file_info):
    files = file_info.get("files") or []
    if not files:
        return None
    f = next((x for x in files if x.get("primary")), files[0])
    return f.get("filename") or os.path.basename(f.get("url") or "mod.jar")


def mr_download_file(file_info, dest_dir):
    files = file_info.get("files") or []
    if not files:
        return None, "该版本没有可下载文件"
    f = next((x for x in files if x.get("primary")), files[0])
    fname = f.get("filename") or os.path.basename(f.get("url") or
                                                  ("mod-%s.jar" % file_info.get("version_number")))
    url = f.get("url")
    if not url:
        return None, "文件缺少下载地址"
    dest = os.path.join(dest_dir, fname)
    for attempt in range(4):
        try:
            r = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT},
                             timeout=(10, 30), proxies=effective_proxies())
            if r.status_code == 429:
                _throttle_wait(r)
                continue
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as fp:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    fp.write(chunk)
                    done += len(chunk)
            if total and done < total:
                os.remove(dest)
                return None, "下载不完整 (%s / %s)" % (human_size(done), human_size(total))
            return dest, None
        except requests.RequestException as e:
            if attempt == 3:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return None, "下载失败: %s" % e
    return None, "下载失败"


def ver_compatible(game, target):
    if game == target:
        return True
    if game.startswith(target + ".") or target.startswith(game + "."):
        return True
    return False


def pick_version(project_id, mc_version, loader):
    vs = mr_versions(project_id, mc_version, loader)
    if vs:
        return vs[0], None
    allv = mr_versions(project_id)
    cand = [v for v in allv
            if (not loader or loader in v.get("loaders", []))
            and (mc_version in v.get("game_versions", [])
                 or any(ver_compatible(g, mc_version) for g in v.get("game_versions", [])))]
    if cand:
        return cand[0], ("该项目没有明确支持 %s 的 %s 版本，将使用兼容版本 %s（请确认）"
                         % (mc_version, LOADER_LABEL.get(loader, loader),
                            cand[0].get("version_number")))
    return None, None


_members_cache = {}


def project_members(project_id):
    if project_id in _members_cache:
        return _members_cache[project_id]
    data = mr_get("project/%s/members" % project_id)
    names = [str(m.get("user", {}).get("username") or "") for m in (data or [])]
    names = [n for n in names if n]
    _members_cache[project_id] = names
    return names


def _norm_names(names):
    return {re.sub(r"[^a-z0-9]+", "", str(n).lower()) for n in names if str(n).strip()}


def authors_equal(jar_authors, member_names):
    a, b = _norm_names(jar_authors), _norm_names(member_names)
    return bool(a) and bool(b) and a == b


def collect_deps(version_obj, mc_version, loader, target_mods_dir, visited, log, downloader):
    for dep in version_obj.get("dependencies") or []:
        if dep.get("dependency_type") != "required":
            continue
        pid = dep.get("project_id")
        if not pid or pid in visited:
            continue
        visited.add(pid)
        ver, warn = pick_version(pid, mc_version, loader)
        if not ver:
            log.warn("依赖 %s 没有适配 %s %s 的版本，请手动安装"
                     % (pid, LOADER_LABEL.get(loader, loader), mc_version))
            continue
        fname = primary_filename(ver) or "mod.jar"
        log.info("提交依赖下载: %s" % fname)
        downloader.submit(ver.get("name") or pid, ver, target_mods_dir, kind="dep", extra=pid)
        if warn:
            log.warn(warn)
        collect_deps(ver, mc_version, loader, target_mods_dir, visited, log, downloader)


def lookup_project_links(meta):
    query = (meta.get("name") or meta.get("id") or "").strip()
    if not query:
        return None
    try:
        hits = mr_search(query, limit=3)
    except requests.RequestException:
        return None
    if not hits:
        return None
    best = max(hits, key=lambda h: score_hit(meta, h)[0])
    if not best or score_hit(meta, best)[0] <= 0:
        return None
    pid = best.get("project_id") or best.get("id")
    if not pid:
        return None
    slug = best.get("slug") or pid
    try:
        data = mr_get("project/%s" % pid)
    except requests.RequestException:
        data = None
    return {"modrinth": "https://modrinth.com/project/%s" % slug,
            "source": (data or {}).get("source_url") or ""}


def _words(s):
    return set(re.findall(r"[a-z0-9一-鿿]+", s.lower()))


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def score_hit(meta, hit):
    title = hit.get("title") or ""
    slug = hit.get("slug") or ""
    mid = _norm(meta.get("id") or "")
    mname = _norm(meta.get("name") or "")
    n_title, n_slug = _norm(title), _norm(slug)
    hit_id = _norm(str(hit.get("project_id") or hit.get("id") or ""))

    if mid and (mid == n_slug or mid == hit_id):
        return 1.0, "modid 与 Modrinth slug 精确匹配"
    if mid and n_title and mid == n_title:
        return 1.0, "modid 与项目标题完全一致"
    if mname and mname == n_slug:
        return 1.0, "模组名与项目 slug 完全一致"

    scores = []
    if mname and n_title:
        scores.append(difflib.SequenceMatcher(None, mname, n_title).ratio())
        a, b = _words(meta.get("name") or ""), _words(title)
        if a and b:
            scores.append(len(a & b) / len(a | b))
    if mid and n_title:
        scores.append(difflib.SequenceMatcher(None, mid, n_title).ratio())
    if mname and n_slug:
        scores.append(difflib.SequenceMatcher(None, mname, n_slug).ratio())
    best = max(scores) if scores else 0.0
    return best, "标题相似度 %.2f" % best


_versions_cache = {}


def _find_same_version(pid, jar_version, mc_version, loader):
    if not jar_version:
        return None
    try:
        if pid not in _versions_cache:
            _versions_cache[pid] = mr_versions(pid)
        allv = _versions_cache[pid]
    except requests.RequestException:
        return None
    for v in allv:
        if (_same_version_number(v.get("version_number"), jar_version)
                and (mc_version in v.get("game_versions", [])
                     or any(ver_compatible(g, mc_version) for g in v.get("game_versions", [])))
                and (not loader or loader in v.get("loaders", []))):
            return v
    return None


def _same_version_number(mr_number, jar_version):
    mr_number = (mr_number or "").lstrip("vV").strip()
    jar_version = (jar_version or "").strip()
    return (mr_number == jar_version
            or mr_number.startswith(jar_version + "+")
            or mr_number.startswith(jar_version + "-"))


def match_to_project(meta, mc_version, loader, jar_path):
    jar_sha1 = sha1_of(jar_path)

    try:
        ver = mr_lookup_sha1(jar_sha1)
        if ver and ver.get("project_id"):
            return ver["project_id"], 1.0, "源文件 sha1 与 Modrinth 完全一致"
    except requests.RequestException:
        pass

    nested = meta.get("nested") or []
    if is_wrapper_meta(meta):
        base = wrapper_base_id(meta)
        ordered = [m for m in nested if (m.get("id") or "") == base] + \
                  [m for m in nested if (m.get("id") or "") != base]
        candidates = [(m, True) for m in ordered] + [(meta, False)]
    else:
        candidates = [(meta, False)] + [(m, True) for m in nested]

    best = None

    for m, from_nested in candidates:
        if not (m.get("name") or m.get("id") or "").strip():
            continue
        for mcv, ldr in ((mc_version, loader), (None, loader), (None, None)):
            try:
                hits = mr_search((m.get("name") or m.get("id") or "").strip(), mcv, ldr)
            except requests.RequestException:
                continue
            for h in hits:
                s, why = score_hit(m, h)
                if best is None or s > best[0]:
                    best = (s, why, h, from_nested, m)

    if best and best[0] > 0:
        pid = best[2].get("project_id") or best[2].get("id")
        if pid:
            m_used = best[4]
            jar_version = (m_used.get("version") or "").strip()
            jar_authors = m_used.get("authors") or []

            members = None
            if best[0] >= ASK_CONF and jar_authors:
                try:
                    members = project_members(pid)
                except requests.RequestException:
                    members = None
            if members:
                if authors_equal(jar_authors, members):
                    why = best[1] + ("（来自内嵌模组 jar）" if best[3] else "")
                    return pid, best[0], why + "（作者校验通过）"
                if jar_version:
                    same = _find_same_version(pid, jar_version, mc_version, loader)
                    if same:
                        files = same.get("files") or []
                        f = next((x for x in files if x.get("primary")),
                                 files[0] if files else None)
                        fsha = ((f or {}).get("hashes") or {}).get("sha1") or ""
                        if fsha and fsha.lower() == jar_sha1:
                            return pid, 1.0, ("作者不一致，但同版本号 %s 且文件 sha1 完全一致"
                                              "（原版文件）" % jar_version)
                return None, 0.0, ("作者列表与 Modrinth 项目不一致，且同版本号文件校验未通过"
                                   "（疑似 fork 版模组）")

            why = best[1] + ("（来自内嵌模组 jar）" if best[3] else "")
            return pid, best[0], why
    return None, 0.0, "无搜索结果"


class Downloader:

    def __init__(self, max_workers=4, worker=None):
        self._worker = worker or mr_download_file
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        self._tasks = []

    def submit(self, label, file_info, dest_dir, kind="mod", extra=None):
        fut = self._pool.submit(self._worker, file_info, dest_dir)
        self._tasks.append((label, kind, extra, fut))
        return fut

    @property
    def pending(self):
        return len(self._tasks)

    def gather(self, stop_event=None, log=None):
        results = []
        total = len(self._tasks)
        done = 0
        for label, kind, extra, fut in self._tasks:
            if stop_event is not None and stop_event.is_set():
                break
            while True:
                try:
                    dest, err = fut.result(timeout=5)
                    break
                except TimeoutError:
                    if log is not None:
                        log.info("下载进行中... 已完成 %d/%d" % (done, total))
            done += 1
            results.append((label, kind, extra, dest, err))
        return results

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait)
