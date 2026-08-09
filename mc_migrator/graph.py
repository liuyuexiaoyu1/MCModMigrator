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


def version_satisfies(version, constraint):
    constraint = (constraint or "").strip()
    if not constraint or constraint == "*":
        return True
    return all(_version_cmp(version, c) for c in re.split(r"[,\s]+", constraint) if c)


def _vkey(version):
    return [int(x) for x in re.findall(r"\d+", str(version))]


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
    req = c
    if not req or req == "*":
        return True
    a = _vkey(version)
    if "x" in req.lower():
        b = _vkey(re.sub(r"[xX]", "0", req))
        if len(a) < len(b):
            return False
        return a[:len(b)] == b
    b = _vkey(req)
    n = max(len(a), len(b))
    a = a + [0] * (n - len(a))
    b = b + [0] * (n - len(b))
    cmpv = (a > b) - (a < b)
    return {"==": cmpv == 0, ">": cmpv > 0, ">=": cmpv >= 0,
            "<": cmpv < 0, "<=": cmpv <= 0}[op]
