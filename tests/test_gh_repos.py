import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mc_migrator.modrinth as _mr

CASES = [
    ("BiliXWhite/litematica-printer", "https://github.com/BiliXWhite/litematica-printer", "26.2"),
    ("MoRanpcy/quickshulker", "https://github.com/MoRanpcy/quickshulker", "26.2"),
    ("liuyuexiaoyu1/Carpet-Igny-Addition",
     "https://github.com/liuyuexiaoyu1/Carpet-Igny-Addition", "1.20.1"),
]

tmp = tempfile.mkdtemp(prefix="gh_repos_")
cdir = os.path.join(tmp, "cmp")
os.makedirs(cdir, exist_ok=True)
failed = 0
try:
    for repo, url, mc in CASES:
        meta = {"id": "probe", "name": "probe", "contact": {"sources": url}}
        try:
            res = _mr.resolve_via_github(meta, mc, None, cdir)
        except Exception as e:
            print("  ✗ %s @%s 异常: %s" % (repo, mc, e))
            failed += 1
            continue
        if res:
            _r, fname, why, dest = res
            size = os.path.getsize(dest) if os.path.exists(dest) else 0
            print("  ✓ %s @%s -> %s (%d KB)\n      %s" % (repo, mc, fname, size // 1024, why))
        else:
            print("  ✗ %s @%s 未能通过 GitHub 兜底解析" % (repo, mc))
            failed += 1
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("真实仓库 GitHub 兜底测试失败 %d 项" % failed)
sys.exit(1 if failed else 0)
