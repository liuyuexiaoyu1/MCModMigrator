import re

DEFAULT_DEPS = {"minecraft", "java", "fabricloader", "fabric", "quilt_loader",
                "quilted_fabric_api", "forge", "neoforge", "fml", "javafml"}


class ModGraph:
    def __init__(self):
        self.mods = {}
        self.dep_reqs = {}
        self.conf_reqs = {}
        self.dependents = {}
        self.conflicting = {}

    def add_mod(self, mod_id, name, version, file_path, project_id, deps, conflicts):
        mod_id = (mod_id or "").strip()
        if not mod_id:
            return
        self.mods[mod_id] = {"name": name or mod_id, "version": version or "",
                             "file": file_path, "project_id": project_id}
        for dep_id, rng in deps or []:
            dep_id = (dep_id or "").strip()
            if not dep_id or dep_id in DEFAULT_DEPS:
                continue
            self.dep_reqs.setdefault(mod_id, {}).setdefault(dep_id, set()).add(rng or "*")
            self.dependents.setdefault(dep_id, set()).add(mod_id)
        for conf_id, rng in conflicts or []:
            conf_id = (conf_id or "").strip()
            if not conf_id or conf_id in DEFAULT_DEPS:
                continue
            self.conf_reqs.setdefault(mod_id, {}).setdefault(conf_id, set()).add(rng or "*")
            self.conflicting.setdefault(conf_id, set()).add(mod_id)

    def requirements_for(self, mod_id):
        ranges = set()
        for reqs in self.dep_reqs.values():
            if mod_id in reqs:
                ranges |= reqs[mod_id]
        return sorted(ranges)

    def mismatches(self):
        out = []
        for a, reqs in self.dep_reqs.items():
            for b, ranges in reqs.items():
                bv = self.mods.get(b, {}).get("version")
                if bv and not any(version_satisfies(bv, r) for r in ranges):
                    out.append((a, b, sorted(ranges), bv))
        return out

    def conflict_report(self):
        out = []
        for mid in sorted(set(self.dependents) & set(self.conflicting)):
            if mid not in self.mods:
                continue
            out.append({"mod": mid,
                        "name": self.mods[mid].get("name") or mid,
                        "file": self.mods[mid].get("file"),
                        "dependents": sorted(self.dependents[mid]),
                        "conflicting": sorted(self.conflicting[mid])})
        seen = {item["mod"] for item in out}
        for a, reqs in self.conf_reqs.items():
            for b, ranges in reqs.items():
                if b in seen or b not in self.mods:
                    continue
                bv = self.mods[b].get("version")
                if bv and any(version_satisfies(bv, r) for r in ranges):
                    out.append({"mod": b,
                                "name": self.mods[b].get("name") or b,
                                "file": self.mods[b].get("file"),
                                "dependents": [],
                                "conflicting": [a]})
        return out

    def remove(self, mod_id):
        info = self.mods.pop(mod_id, None)
        self.dep_reqs.pop(mod_id, None)
        self.conf_reqs.pop(mod_id, None)
        self.dependents.pop(mod_id, None)
        self.conflicting.pop(mod_id, None)
        for reqs in self.dep_reqs.values():
            reqs.pop(mod_id, None)
        for reqs in self.conf_reqs.values():
            reqs.pop(mod_id, None)
        for s in self.dependents.values():
            s.discard(mod_id)
        for s in self.conflicting.values():
            s.discard(mod_id)
        return info

    def summary(self):
        return (len(self.mods),
                sum(len(v) for v in self.dep_reqs.values()),
                sum(len(v) for v in self.conf_reqs.values()))


PRERELEASE_RANK = {"alpha": 1, "beta": 2, "rc": 3}


def version_satisfies(version, constraint):
    constraint = (constraint or "").strip()
    if not constraint or constraint == "*":
        return True
    parts = [p for p in re.split(r"\|\|", constraint) if p.strip()]
    if len(parts) > 1:
        return any(version_satisfies(version, p) for p in parts)
    checks = []
    for c in re.split(r"[,\s]+", constraint):
        if not c:
            continue
        if c.startswith("~") or c.startswith("^"):
            checks.extend(_expand_tilde_caret(c))
        else:
            checks.append(c)
    return all(_version_cmp(version, c) for c in checks)


def _expand_tilde_caret(c):
    op, body = c[0], c[1:]
    nums = _vkey(body)
    body_has_dash = body.endswith("-")
    body_core = body[:-1] if body_has_dash else body
    lo = body_core
    if op == "~":
        if len(nums) >= 2:
            hi = "%d.%d.0-" % (nums[0], nums[1] + 1)
        else:
            hi = "%d.%d.0-" % (nums[0], 1)
        if not body_has_dash and "-" not in body_core:
            lo = ".".join(str(x) for x in (nums + [0, 0, 0])[:3])
        return [">=" + lo + ("-" if body_has_dash else ""), "<" + hi]
    hi = "%d.0.0-" % ((nums[0] if nums else 0) + 1)
    return [">=" + lo + ("-" if body_has_dash else ""), "<" + hi]


def _vkey(version):
    core = str(version or "").partition("+")[0].partition("-")[0]
    return [int(x) for x in re.findall(r"\d+", core)]


def _prerank(version):
    pre = str(version or "").partition("+")[0].partition("-")[2]
    if not pre:
        return 0
    m = re.match(r"([a-zA-Z]+)", pre)
    return PRERELEASE_RANK.get((m.group(1) if m else "").lower(), 4)


def _vcmp(a, b, b_low_pre=False):
    na, nb = _vkey(a), _vkey(b)
    n = max(len(na), len(nb))
    na += [0] * (n - len(na))
    nb += [0] * (n - len(nb))
    if na != nb:
        return (na > nb) - (na < nb)
    pa, pb = _prerank(a), (-1 if b_low_pre else _prerank(b))
    if pa == pb:
        return 0
    if pa == 0:
        return 1
    if pb == 0:
        return -1
    return (pa > pb) - (pa < pb)


def _version_cmp(version, c):
    c = c.strip()
    head, tail = (c[:1] if c else ""), (c[-1:] if c else "")
    if head in "[(" or tail in "])":
        op = {"]": "<=", ")": "<"}.get(tail, "==")
        if head == "[":
            op = ">="
        elif head == "(":
            op = ">"
        if tail in "])":
            op = {"]": "<=", ")": "<"}.get(tail, op)
        c = c.strip("[]()")
        m = re.match(r"^(>=|<=|>|<|==)?\s*(.+)$", c)
        if m.group(1):
            op = m.group(1)
            c = m.group(2).strip()
    else:
        m = re.match(r"^(>=|<=|>|<|==)?\s*(.+)$", c)
        op = m.group(1) or "=="
        c = m.group(2).strip()
    if not c or c == "*":
        return True
    low_pre = False
    if c.endswith("-"):
        c = c[:-1]
        low_pre = True
    if "x" in c.lower() or "*" in c:
        head = c.split("x")[0].split("X")[0].split("*")[0]
        b = [int(x) for x in re.findall(r"\d+", head)]
        a = _vkey(version)
        if len(a) < len(b):
            return False
        return a[:len(b)] == b
    cmpv = _vcmp(version, c, low_pre)
    return {"==": cmpv == 0, ">": cmpv > 0, ">=": cmpv >= 0,
            "<": cmpv < 0, "<=": cmpv <= 0}[op]
