import base64
import difflib
import json
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

from .core import ASK_CONF, USER_AGENT, LOADER_LABEL, effective_proxies, human_size
from .curseforge import resolve_via_curseforge
from .graph import version_satisfies
from .mod_parser import is_wrapper_meta, parse_mod_jar, sha1_of, wrapper_base_id
from .versions import mc_release_date

API_BASE = "https://api.modrinth.com/v2"
GH_API = "https://api.github.com"
_SESSION = requests.Session()

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
            r = _SESSION.get(API_BASE + "/" + path, params=params,
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
            r = _SESSION.get(url, stream=True, headers={"User-Agent": USER_AGENT},
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


def pick_version_in_range(project_id, mc_version, loader, ranges):
    if not ranges:
        return pick_version(project_id, mc_version, loader)[0]
    vs = mr_versions(project_id, mc_version, loader)
    if not vs:
        vs = mr_versions(project_id)
    for v in vs:
        if ((mc_version in v.get("game_versions", [])
             or any(ver_compatible(g, mc_version) for g in v.get("game_versions", [])))
                and (not loader or loader in v.get("loaders", []))
                and all(version_satisfies(v.get("version_number"), r) for r in ranges)):
            return v
    return None


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


def _mcmod_search(query):
    url = "https://search.mcmod.cn/s?key=" + quote(query)
    r = _SESSION.get(url, headers={"User-Agent": USER_AGENT}, timeout=20,
                     proxies=effective_proxies())
    r.raise_for_status()
    r.encoding = "utf-8"
    m = re.search(r'href="(https?://www\.mcmod\.cn/class/\d+\.html)"', r.text)
    return m.group(1) if m else None


def _mcmod_modrinth_url(class_url):
    r = _SESSION.get(class_url, headers={"User-Agent": USER_AGENT}, timeout=20,
                     proxies=effective_proxies())
    r.raise_for_status()
    r.encoding = "utf-8"
    for m in re.finditer(r'href="//link\.mcmod\.cn/target/([A-Za-z0-9+/=]+)"', r.text):
        try:
            decoded = base64.b64decode(m.group(1)).decode("utf-8", "replace")
        except Exception:
            continue
        if "modrinth.com" in decoded:
            return decoded
    return None


def _modrinth_id_from_url(url):
    m = re.search(r"modrinth\.com/(?:mod|project|plugin)/([^/?#]+)", url or "")
    return m.group(1) if m else None


def resolve_via_mcmod(meta):
    query = (meta.get("name") or meta.get("id") or "").strip()
    if not query:
        return None
    try:
        class_url = _mcmod_search(query)
        if not class_url:
            return None
        link = _mcmod_modrinth_url(class_url)
        if not link:
            return None
        slug = _modrinth_id_from_url(link)
        if not slug:
            return None
        data = mr_get("project/%s" % slug)
        if not data or not data.get("id"):
            return None
        return data["id"], slug
    except Exception:
        return None


def _github_repo(meta, pid):
    contact = meta.get("contact") or {}
    for k in ("sources", "issues", "homepage"):
        m = re.search(r"github\.com/([^/?#]+)/([^/?#]+)", str(contact.get(k) or ""))
        if m:
            return _clean_gh_repo(m.group(1), m.group(2))
    if pid:
        try:
            data = mr_get("project/%s" % pid)
        except requests.RequestException:
            data = None
        if data and data.get("source_url"):
            m = re.search(r"github\.com/([^/?#]+)/([^/?#]+)", str(data["source_url"]))
            if m:
                return _clean_gh_repo(m.group(1), m.group(2))
    return None


def _clean_gh_repo(owner, name):
    if not owner or not name or owner in ("topics", "settings"):
        return None
    if name.endswith(".git"):
        name = name[:-4]
    return "%s/%s" % (owner, name)


def _gh_releases(repo):
    for path in ("repos/%s/releases?per_page=10" % repo,
                 "repos/%s/releases/latest" % repo):
        try:
            r = _SESSION.get(GH_API + "/" + path, headers={"User-Agent": USER_AGENT},
                             timeout=20, proxies=effective_proxies())
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else [data]
        except requests.RequestException:
            continue
    return _gh_releases_html(repo)


def _gh_html_get(url):
    for attempt in range(3):
        try:
            r = _SESSION.get(url, headers={"User-Agent": USER_AGENT}, timeout=20,
                             proxies=effective_proxies())
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(1.5)


def _gh_releases_html(repo):
    out = []
    r = _gh_html_get("https://github.com/%s/releases.atom" % repo)
    if r is None:
        return out
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S)[:20]:
        tag = ""
        im = re.search(r"<id>.*?/([^/]+)</id>", e, re.S)
        tm = re.search(r"<title>(.*?)</title>", e, re.S)
        if im:
            tag = im.group(1).strip()
        elif tm:
            tag = tm.group(1).strip()
        dm = re.search(r"<updated>(.*?)</updated>", e)
        if not tag:
            continue
        assets = []
        ar = _gh_html_get("https://github.com/%s/releases/expanded_assets/%s"
                          % (repo, quote(tag, safe="")))
        if ar is not None:
            for href in re.findall(r'href="(/[^"]+/releases/download/[^"]+)"', ar.text):
                if href.lower().endswith(".jar"):
                    assets.append({"name": os.path.basename(href),
                                   "browser_download_url": "https://github.com" + href})
        if assets:
            out.append({"tag_name": tag, "name": tag, "body": "",
                        "published_at": (dm.group(1) if dm else ""), "assets": assets})
            if len(out) >= 10:
                break
    return out


def _pick_gh_asset(release, mc_version):
    jars = [a for a in release.get("assets") or []
            if (a.get("name") or "").lower().endswith(".jar")]
    runnable = [a for a in jars
                if not re.search(r"[-_.](sources|src|javadoc)(?=[.-]|$)",
                                 (a.get("name") or "").lower())]
    if runnable:
        jars = runnable
    if len(jars) == 1:
        return jars[0], "release 仅含一个 jar 文件，直接下载"
    for a in jars:
        if mc_version and mc_version in (a.get("name") or ""):
            return a, "release 文件包含目标版本 %s" % mc_version
    blob = "%s %s %s" % (release.get("tag_name") or "", release.get("name") or "",
                         release.get("body") or "")
    if mc_version and mc_version in blob and jars:
        return jars[0], "release 标题/说明包含目标版本 %s" % mc_version
    return None, None


def resolve_via_github(meta, mc_version, pid, compare_dir):
    if not compare_dir:
        return None
    repo = _github_repo(meta, pid)
    if not repo:
        return None
    releases = _gh_releases(repo)
    if not releases:
        return None
    mc_date = mc_release_date(mc_version)
    for rel in releases:
        if rel.get("draft"):
            continue
        pub = rel.get("published_at") or ""
        if mc_date and pub and pub < mc_date:
            continue
        asset, why = _pick_gh_asset(rel, mc_version)
        if not asset:
            continue
        url = asset.get("browser_download_url")
        if not url:
            continue
        dest, err = mr_download_file({"files": [{"filename": asset.get("name") or "mod.jar",
                                                  "url": url}]}, compare_dir)
        if not dest:
            continue
        cm = parse_mod_jar(dest)
        adapted = bool(cm)
        if adapted:
            try:
                with zipfile.ZipFile(dest) as z:
                    names = z.namelist()
                if any(n.lower().endswith(".java") for n in names) \
                        and not any(n.lower().endswith(".class") for n in names):
                    adapted = False
            except Exception:
                pass
            if adapted:
                for mid, rng in cm.get("deps", []):
                    if mid == "minecraft" and not version_satisfies(mc_version, rng):
                        adapted = False
                        break
        if not adapted:
            try:
                os.remove(dest)
            except OSError:
                pass
            continue
        return (repo, asset.get("name") or "",
                "从 GitHub release 兜底下载（%s，发布于 %s，%s）" % (repo, (pub or "?")[:10], why),
                dest)
    return None


def _same_version_compare(same, compare_dir, local_sha1):
    if compare_dir:
        dest, err = mr_download_file(same, compare_dir)
        if dest:
            if sha1_of(dest) == local_sha1:
                return dest
            try:
                os.remove(dest)
            except OSError:
                pass
        return None
    files = same.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    fsha = ((f or {}).get("hashes") or {}).get("sha1") or ""
    if fsha and fsha.lower() == local_sha1:
        return True
    return None


def match_to_project(meta, mc_version, loader, jar_path, ignore_fork=False,
                     src_mc_version=None, compare_dir=None):
    filter_mc = src_mc_version or mc_version
    jar_sha1 = sha1_of(jar_path)

    try:
        ver = mr_lookup_sha1(jar_sha1)
        if ver and ver.get("project_id"):
            return ver["project_id"], 1.0, "源文件 sha1 与 Modrinth 完全一致", None
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

            if is_wrapper_meta(meta):
                rep = (meta.get("nested") or [None])[0]
                rep_ver = (rep or {}).get("version") or ""
                rep_sha1 = (rep or {}).get("sha1") or ""
                if rep_ver and rep_sha1:
                    same = _find_same_version(pid, rep_ver, filter_mc, loader)
                    if same:
                        matched = _same_version_compare(same, compare_dir, rep_sha1)
                        if matched:
                            return pid, 1.0, ("wrapper 内嵌 jar 同版本号 %s 且文件 sha1 完全一致"
                                              % rep_ver), (matched if isinstance(matched, str) else None)

            if best[0] >= ASK_CONF and jar_authors and compare_dir:
                cand = pick_version(pid, filter_mc, loader)[0]
                if cand:
                    dest, err = mr_download_file(cand, compare_dir)
                    if dest:
                        cm = parse_mod_jar(dest)
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                        if cm and cm.get("authors"):
                            if authors_equal(jar_authors, cm["authors"]):
                                why = best[1] + ("（来自内嵌模组 jar）" if best[3] else "")
                                return pid, best[0], why + "（作者校验通过）", None
                            if jar_version:
                                same = _find_same_version(pid, jar_version, filter_mc, loader)
                                if same:
                                    matched = _same_version_compare(same, compare_dir, jar_sha1)
                                    if matched:
                                        return pid, 1.0, ("作者不一致，但同版本号 %s 且文件 sha1 "
                                                          "完全一致（原版文件）" % jar_version), \
                                            (matched if isinstance(matched, str) else None)
                            if ignore_fork:
                                return pid, 1.0, "作者校验未通过，已按选项忽略（疑似 fork 版，请留意）", None
                            gh = resolve_via_github(m_used, mc_version, pid, compare_dir)
                            if gh:
                                return "github:" + gh[0], 1.0, gh[2], gh[3]
                            return None, 0.0, "作者不一致（疑似 fork 版模组）", None
                else:
                    gh = resolve_via_github(m_used, mc_version, pid, compare_dir)
                    if gh:
                        return "github:" + gh[0], 1.0, gh[2], gh[3]

            why = best[1] + ("（来自内嵌模组 jar）" if best[3] else "")
            return pid, best[0], why, None
    cf = resolve_via_curseforge(meta, mc_version, loader, compare_dir, jar_path)
    if cf:
        return cf[0], 1.0, cf[2], cf[3]
    resolved = resolve_via_mcmod(meta)
    if resolved:
        return resolved[0], 1.0, "通过 mcmod.cn 定位到 Modrinth 项目（%s）" % resolved[1], None
    gh = resolve_via_github(meta, mc_version, None, compare_dir)
    if gh:
        return "github:" + gh[0], 1.0, gh[2], gh[3]
    return None, 0.0, "无搜索结果", None


class Downloader:

    def __init__(self, max_workers=4, worker=None, log=None):
        self._worker = worker or mr_download_file
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        self._tasks = []
        self._log = log

    def _log_done(self, fut, label, kind):
        try:
            dest, err = fut.result()
        except Exception as e:
            dest, err = None, str(e)
        if dest:
            size = human_size(os.path.getsize(dest)) if os.path.exists(dest) else "?"
            if kind == "mod":
                self._log.info("已下载: %s (%s)" % (os.path.basename(dest), size))
            else:
                self._log.info("依赖 %s -> %s (%s)" % (label, os.path.basename(dest), size))
        else:
            if kind == "mod":
                self._log.error("下载失败: %s" % err)
            else:
                self._log.error("依赖 %s 下载失败: %s" % (label, err))

    def submit(self, label, file_info, dest_dir, kind="mod", extra=None):
        fut = self._pool.submit(self._worker, file_info, dest_dir)
        self._tasks.append((label, kind, extra, fut))
        if self._log is not None:
            fut.add_done_callback(lambda f, lb=label, kd=kind: self._log_done(f, lb, kd))
        return fut

    @property
    def pending(self):
        return len(self._tasks)

    def gather(self, stop_event=None, log=None):
        results = []
        total = len(self._tasks)
        completed = [0]

        def _count(_f):
            completed[0] += 1

        for _label, _kind, _extra, fut in self._tasks:
            fut.add_done_callback(_count)
        for label, kind, extra, fut in self._tasks:
            if stop_event is not None and stop_event.is_set():
                break
            while True:
                try:
                    dest, err = fut.result(timeout=5)
                    break
                except TimeoutError:
                    if log is not None:
                        log.info("下载进行中... 已完成 %d/%d" % (completed[0], total))
            results.append((label, kind, extra, dest, err))
        return results

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait)
