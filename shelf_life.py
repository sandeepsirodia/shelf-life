"""shelf-life: how long does your agent's code actually survive?

Survival analysis on your own git history. Every added line is tracked from birth until it is
modified or deleted; lines still alive at HEAD are censored. Kaplan-Meier curves, Greenwood
intervals and a log-rank test (all implemented here, stdlib only) compare agent-written lines
with human-written ones.
"""
import argparse
import bisect
import fnmatch
import glob
import html
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
import zlib
from datetime import datetime, timezone

__version__ = "0.1.0"
DAY = 86400.0
CACHE_VERSION = 2

# (agent name, regex over "Co-authored-by:" trailers and the author "name <email>")
AGENTS = [
    ("claude", r"\bclaude\b|noreply@anthropic\.com"),
    ("codex", r"\bcodex\b|codex@openai\.com"),
    ("copilot", r"\bcopilot\b"),
    ("cursor", r"\bcursor(agent)?\b|@cursor\.com"),
    ("devin", r"\bdevin(-ai)?\b"),
    ("aider", r"\baider\b"),
    ("gemini", r"\bgemini\b"),
]
BOTS = r"\[bot\]|dependabot|renovate|github-actions|pre-commit-ci"
# Regenerated wholesale by tools, not written by anyone: tracking them would measure file types, not authors.
GENERATED_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|Cargo\.lock|poetry\.lock|uv\.lock|Gemfile\.lock|"
    r"composer\.lock|go\.sum|Pipfile\.lock|npm-shrinkwrap\.json|CHANGELOG[^/]*)$|"
    r"\.(lock|snap|map|min\.js|min\.css|pb\.go|pyc|svg|ipynb)$|_pb2\.py$|\.generated\.|\.api\.(md|json)$|"
    r"(^|/)(dist|build|vendor|third_party|node_modules|__snapshots__|snapshots|generated|\.yarn)/", re.I)
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|specs?)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")


# ------------------------------------------------------------------ attribution

def classify(author, message, agents=AGENTS):
    """'claude' / 'codex' / … for agent commits, 'bot' for dependency bots, else 'human'."""
    trailers = " ".join(re.findall(r"^co-authored-by:(.*)$", message, re.I | re.M))
    for name, rx in agents:
        if re.search(rx, trailers, re.I) or re.search(rx, author, re.I):
            return name
    if re.search(BOTS, author, re.I):
        return "bot"
    return "human"


def group_of(cls):
    return "human" if cls == "human" else "bot" if cls == "bot" else "agent"


# ------------------------------------------------------------------ statistics

def kaplan_meier(durations, events):
    """Return [(t, n_at_risk, n_events, S(t), greenwood_se)] at each distinct event time."""
    data = sorted(zip(durations, events))
    n, i, s, gw = len(data), 0, 1.0, 0.0
    out = []
    while i < len(data):
        t = data[i][0]
        d = c = 0
        while i < len(data) and data[i][0] == t:
            d += data[i][1]
            c += 1 - data[i][1]
            i += 1
        if d:
            s *= 1 - d / n
            gw = gw + d / (n * (n - d)) if n > d else float("inf")
            out.append((t, n, d, s, s * math.sqrt(gw) if math.isfinite(gw) else 0.0))
        n -= d + c
    return out


def survival_at(curve, t):
    """S(t) and a 95% log-log confidence interval (bounded in [0, 1])."""
    s, se = 1.0, 0.0
    for tt, _, _, ss, sse in curve:
        if tt > t:
            break
        s, se = ss, sse
    if s in (0.0, 1.0) or se == 0:
        return s, (s, s)
    # log(-log) transform, the default in most survival software; keeps the interval inside [0, 1]
    sd = math.sqrt((se / s) ** 2 / math.log(s) ** 2)
    return s, (s ** math.exp(1.96 * sd), s ** math.exp(-1.96 * sd))


def km_at(rows, horizons):
    """Weighted Kaplan-Meier S(t) at each horizon. rows: sorted [(t, event, weight)]."""
    n = sum(w for _, _, w in rows)
    s, out, hi, i = 1.0, {}, sorted(horizons), 0
    k = 0
    while i < len(rows):
        t = rows[i][0]
        while k < len(hi) and hi[k] < t:
            out[hi[k]] = s
            k += 1
        d = c = 0.0
        while i < len(rows) and rows[i][0] == t:
            if rows[i][1]:
                d += rows[i][2]
            else:
                c += rows[i][2]
            i += 1
        if d and n > 0:
            s *= 1 - d / n
        n -= d + c
    for h in hi[k:]:
        out[h] = s
    return out


def cluster_bootstrap(obs_by_group, horizons=(7, 30, 90), reps=200, seed=0):
    """95% intervals for S(h) per group, and for the agent - human difference, resampling *commits*.
    Lines written in the same commit live and die together, so treating them as independent (Greenwood)
    gives absurdly narrow intervals on big repos."""
    import random
    rng = random.Random(seed)
    prepared = {}
    for g, obs in obs_by_group.items():
        agg = Counter((o["commit"], round(o["days"], 6), o["event"]) for o in obs)
        rows = sorted((days, ev, commit, w) for (commit, days, ev), w in agg.items())
        commits = sorted({c for _, _, c, _ in rows})
        prepared[g] = (rows, commits)
    draws = {g: [] for g in prepared}
    diffs = []
    for _ in range(reps):
        est = {}
        for g, (rows, commits) in prepared.items():
            mult = Counter(rng.choice(commits) for _ in commits)
            weighted = [(t, ev, w * mult[c]) for t, ev, c, w in rows if mult.get(c)]
            est[g] = km_at(weighted, horizons)
            draws[g].append(est[g])
        if "agent" in est and "human" in est:
            diffs.append({h: est["agent"][h] - est["human"][h] for h in horizons})

    def pct(values):
        v = sorted(values)
        return v[int(0.025 * (len(v) - 1))], v[int(0.975 * (len(v) - 1))]

    out = {g: {h: pct([d[h] for d in ds]) for h in horizons} for g, ds in draws.items() if ds}
    if diffs:
        out["diff"] = {h: pct([d[h] for d in diffs]) for h in horizons}
    return out


def median_survival(curve):
    for t, _, _, s, _ in curve:
        if s <= 0.5:
            return t
    return None  # more than half still alive: median not reached


def logrank(d1, e1, d2, e2):
    """Two-group log-rank test. Returns (chi2, p, observed1, expected1)."""
    rows = sorted([(t, e, 0) for t, e in zip(d1, e1)] + [(t, e, 1) for t, e in zip(d2, e2)])
    n = [len(d1), len(d2)]
    o1, exp1, var, i = sum(e1), 0.0, 0.0, 0
    while i < len(rows):
        t = rows[i][0]
        dead, gone = [0, 0], [0, 0]
        while i < len(rows) and rows[i][0] == t:
            _, e, g = rows[i]
            dead[g] += e
            gone[g] += 1
            i += 1
        nt, dt = n[0] + n[1], dead[0] + dead[1]
        if dt and nt:
            exp1 += dt * n[0] / nt
            if nt > 1:
                var += dt * (n[0] / nt) * (1 - n[0] / nt) * (nt - dt) / (nt - 1)
        n[0] -= gone[0]
        n[1] -= gone[1]
    chi2 = (o1 - exp1) ** 2 / var if var else 0.0
    return chi2, math.erfc(math.sqrt(chi2 / 2)), o1, exp1


# ------------------------------------------------------------------ replaying history

class State:
    """Line-level replay of first-parent history. Saved as JSON for the incremental cache."""
    FIELDS = ("files", "file_keys", "touches", "dead", "last_commit", "head_ts", "commits", "skipped", "next_key",
              "unknown", "commit_no")

    def __init__(self):
        self.files = {}      # path -> [line, …]; line = [birth_ts, cls, author, file_key, content, commit_no]
        self.unknown = {}    # paths whose earlier content we never saw (excluded, then renamed in): never guessed at
        self.commit_no = 0   # ordinal of the commit being replayed: the cluster id (timestamps can collide)
        self.file_keys = {}  # path -> stable key that survives renames
        self.touches = {}    # str(file_key) -> [[ts, author_identity], …]
        self.dead = []       # [birth_ts, death_ts, cls, author, file_key, killer_identity, path, commit_no]
        self.last_commit = None
        self.head_ts = 0.0
        self.commits = Counter()
        self.skipped = Counter()
        self.next_key = 0

    def key(self, path):
        if path not in self.file_keys:
            self.file_keys[path] = self.next_key
            self.next_key += 1
        return self.file_keys[path]

    def to_json(self):
        return json.dumps({"v": CACHE_VERSION, **{f: getattr(self, f) for f in self.FIELDS}})

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        if data.get("v") != CACHE_VERSION:
            raise ValueError("stale cache")
        st = cls()
        for f in cls.FIELDS:
            setattr(st, f, data[f])
        st.commits, st.skipped = Counter(st.commits), Counter(st.skipped)
        return st


def identity(cls, author):
    """Who touched a file: all commits by one agent share an identity; humans by email."""
    return "agent:" + cls if group_of(cls) == "agent" else author


def norm_ws(s):
    return re.sub(r"\s+", "", s)


def git_log(repo, since_commit=None):
    fmt = "%x1e%H%x1f%ct%x1f%an <%ae>%x1f%B%x1f"
    args = ["git", "-c", "core.quotepath=off", "log", "--reverse", "--first-parent", "--diff-merges=first-parent",
            "-p", "-U0", "-M", "--no-color", "--no-ext-diff", "--format=" + fmt]
    if since_commit:
        args.append("%s..HEAD" % since_commit)
    p = subprocess.Popen(args, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buf = b""
    for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
        buf += chunk
        *records, buf = buf.split(b"\x1e")
        for r in records:
            if r.strip():
                yield r.decode("utf-8", "replace")
    if buf.strip():
        yield buf.decode("utf-8", "replace")
    p.wait()


def apply_commit(state, record, agents, count_whitespace=False, exclude=None):
    sha, ts, author, message, patch = record.split("\x1f", 4)
    ts = float(ts)
    cls = classify(author, message, agents)
    who = identity(cls, author)
    state.commits[cls] += 1
    state.commit_no += 1
    cid = state.commit_no
    state.last_commit, state.head_ts = sha, max(state.head_ts, ts)

    for fpatch in re.split(r"^diff --git ", patch, flags=re.M)[1:]:
        header, _, body = fpatch.partition("\n@@")
        body = "@@" + body if body else ""
        rename = re.search(r"^rename from (.+)\nrename to (.+)$", header, re.M)
        if rename:
            old, new = rename.group(1), rename.group(2)
            if old in state.files:
                state.files[new] = state.files.pop(old)
                if old in state.file_keys:
                    state.file_keys[new] = state.file_keys.pop(old)
            else:
                # Renamed in from a path we never tracked (excluded, vendored, or unknown): we don't know its
                # lines, and guessing "empty" would misplace every later hunk. Skip the file instead.
                state.files.pop(new, None)
                state.unknown[new] = True
            if old in state.unknown:
                state.unknown.pop(old)
                state.unknown[new] = True
                state.files.pop(new, None)
        if re.search(r"^Binary files|^GIT binary patch", header + body, re.M):
            state.skipped["binary file changes"] += 1
            continue
        m_new = re.search(r"^\+\+\+ (?:b/)?(.+)$", header, re.M)
        m_old = re.search(r"^--- (?:a/)?(.+)$", header, re.M)
        if not m_new:
            continue
        path = m_new.group(1) if m_new.group(1) != "/dev/null" else (m_old.group(1) if m_old else None)
        if not path:
            continue
        if exclude is not None and exclude.search(path):
            state.skipped["generated/excluded file changes"] += 1
            state.files.pop(path, None)
            continue
        if path in state.unknown:
            continue
        is_new = "--- /dev/null" in header or re.search(r"^new file mode", header, re.M)
        if path not in state.files and not is_new:
            state.unknown[path] = True
            state.skipped["files that entered tracking mid-history"] += 1
            continue
        lines = state.files.setdefault(path, [])
        fkey = state.key(path)
        state.touches.setdefault(str(fkey), []).append([ts, who])
        delta = 0
        for hunk in re.split(r"^(?=@@ )", body, flags=re.M):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hunk)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            hl = hunk.split("\n")[1:]
            removed = [l[1:] for l in hl if l.startswith("-")]
            added = [l[1:] for l in hl if l.startswith("+")]
            pos = max(0, min((a - 1 + delta) if b > 0 else (a + delta), len(lines)))
            old = lines[pos:pos + b]
            new, kept_ids = [], set()
            for i, content in enumerate(added):
                if not count_whitespace and i < len(old) and i < len(removed) and norm_ws(removed[i]) == norm_ws(content):
                    old[i][4] = content  # whitespace-only edit: same line, still alive
                    new.append(old[i])
                    kept_ids.add(id(old[i]))
                else:
                    new.append([ts, cls, author, fkey, content, cid])
            for line in old:
                if id(line) not in kept_ids:
                    state.dead.append([line[0], ts, line[1], line[2], line[3], who, path, line[5]])
            lines[pos:pos + b] = new
            delta += len(added) - b
        if m_new.group(1) == "/dev/null":
            state.files.pop(path, None)


def build_exclude(include_generated=False, extra=()):
    parts = [] if include_generated else [GENERATED_RE.pattern]
    parts += [fnmatch.translate(g) for g in extra]
    return re.compile("|".join("(?:%s)" % p for p in parts), re.I) if parts else None


_DEFAULT = object()


def replay(repo, agents=AGENTS, count_whitespace=False, cache=True, exclude=_DEFAULT):
    """`exclude`: compiled regex of paths to skip, None to track everything; default skips generated files."""
    exclude = build_exclude() if exclude is _DEFAULT else exclude
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    tag = "%s-%08x" % ("ws" if count_whitespace else "nows", zlib.crc32(((exclude.pattern if exclude else "") +
                                                                        repr(agents)).encode()))
    cache_path = os.path.join(repo, ".git", "shelf-life", "state-%s.json" % tag)
    state = None
    if cache and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                state = State.from_json(f.read())
            if state.last_commit == head:
                return state
            # Resume only from a commit on HEAD's *first-parent* line (what we replay). Merely being an ancestor
            # isn't enough: a side-branch commit would splice a different path of history into the state.
            first_parent = subprocess.run(["git", "rev-list", "--first-parent", head], cwd=repo,
                                          capture_output=True, text=True).stdout.split()
            if state.last_commit not in set(first_parent):
                state = None  # checked out elsewhere, or history rewritten: start over
        except (ValueError, KeyError, OSError):
            state = None
    since = state.last_commit if state else None
    state = state or State()
    for rec in git_log(repo, since):
        apply_commit(state, rec, agents, count_whitespace, exclude)
    if cache and state.last_commit:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(state.to_json())
    return state


# ------------------------------------------------------------------ analysis

def first_agent_ts(state):
    times = agent_commit_times(state)
    return times[0] if times else None


def observations(state, end_ts=None, born_after=None):
    """Yield dicts: group, cls, days, event (1 = died), exposed (someone else touched the file meanwhile), path.
    `born_after`: only lines born at/after this timestamp (same-era comparison)."""
    end = end_ts or state.head_ts
    touch_ts = {k: [t for t, _ in v] for k, v in state.touches.items()}

    def exposed(fkey, birth, until, author, cls):
        me = identity(cls, author)
        ts, who = touch_ts.get(str(fkey), []), state.touches.get(str(fkey), [])
        lo, hi = bisect.bisect_right(ts, birth), bisect.bisect_right(ts, until)
        return any(w != me for _, w in who[lo:hi])

    for birth, death, cls, author, fkey, killer, path, cid in state.dead:
        if born_after is not None and birth < born_after:
            continue
        yield {"group": group_of(cls), "cls": cls, "days": (death - birth) / DAY, "event": 1, "path": path, "commit": cid,
               "exposed": killer != identity(cls, author) or exposed(fkey, birth, death, author, cls)}
    for path, lines in state.files.items():
        for birth, cls, author, fkey, _, cid in lines:
            if born_after is not None and birth < born_after:
                continue
            yield {"group": group_of(cls), "cls": cls, "days": (end - birth) / DAY, "event": 0, "path": path, "commit": cid,
                   "exposed": exposed(fkey, birth, end, author, cls)}


def summarize(obs, horizons=(7, 30, 90)):
    d, e = [o["days"] for o in obs], [o["event"] for o in obs]
    curve = kaplan_meier(d, e)
    return {"lines": len(obs), "deaths": sum(e), "median_days": median_survival(curve),
            "survival": {h: survival_at(curve, h) for h in horizons},
            "curve": thin([(t, s) for t, _, _, s, _ in curve])}


def thin(points, k=400):
    """At most ~k points for charts/JSON; always keeps the first and last. Stats use the full curve."""
    if len(points) <= k:
        return points
    step = len(points) / k
    return [points[int(i * step)] for i in range(k)] + [points[-1]]


def analyze(state, breakdown_by=("cls",), all_history=False, bootstrap=200):
    """Default: compare only lines born since the first agent commit, so both groups come from the same
    era of the project (early history churns more, and agents only exist in recent history)."""
    since = None if all_history else first_agent_ts(state)
    obs = [o for o in observations(state, born_after=since) if o["group"] != "bot"]
    groups = defaultdict(list)
    for o in obs:
        groups[o["group"]].append(o)
    out = {"commits": dict(state.commits), "skipped": dict(state.skipped), "groups": {}, "by": {},
           "since": since}
    for g, os_ in groups.items():
        out["groups"][g] = summarize(os_)
        out["groups"][g]["exposed_share"] = sum(o["exposed"] for o in os_) / len(os_)
    if bootstrap and groups:
        cb = cluster_bootstrap(dict(groups), reps=bootstrap)
        for g in out["groups"]:
            if g in cb:
                out["groups"][g]["survival_clustered"] = {
                    hz: (out["groups"][g]["survival"][hz][0], cb[g][hz]) for hz in cb[g]}
                out["groups"][g]["commits"] = len({o["commit"] for o in groups[g]})
        if "diff" in cb:
            out["diff_clustered"] = cb["diff"]
    if "agent" in groups and "human" in groups:
        a, h = groups["agent"], groups["human"]
        chi2, p, o1, e1 = logrank([o["days"] for o in a], [o["event"] for o in a],
                                  [o["days"] for o in h], [o["event"] for o in h])
        out["logrank"] = {"chi2": chi2, "p": p, "agent_observed": o1, "agent_expected": e1}
        ea, eh = out["groups"]["agent"]["exposed_share"], out["groups"]["human"]["exposed_share"]
        flag = ea < 0.5 * eh
        out["confound"] = {"agent_exposed_share": ea, "human_exposed_share": eh, "flag": flag,
                           "note": "agent lines are rarely touched by anyone else, so a survival advantage may mean "
                                   "nobody works on that code, not that it's better" if flag else ""}
        exposed_only = {g: [o for o in groups[g] if o["exposed"]] for g in ("agent", "human")}
        if all(len(v) >= 20 for v in exposed_only.values()):
            out["exposed_only"] = {g: summarize(v) for g, v in exposed_only.items()}
    for key in breakdown_by:
        sub = defaultdict(list)
        for o in obs:
            k = {"cls": o["cls"], "dir": o["path"].split("/")[0] if "/" in o["path"] else ".",
                 "ext": os.path.splitext(o["path"])[1] or "(none)",
                 "kind": "tests" if TEST_RE.search(o["path"]) else "code"}[key]
            sub[k].append(o)
        out["by"][key] = {}
        for k, v in sorted(sub.items()):
            if len(v) < 10:
                continue
            out["by"][key][k] = summarize(v)
            out["by"][key][k]["commits"] = len({o["commit"] for o in v})
            if bootstrap:
                cb = cluster_bootstrap({k: v}, reps=bootstrap)[k]
                out["by"][key][k]["survival_clustered"] = {
                    hz: (out["by"][key][k]["survival"][hz][0], cb[hz]) for hz in cb}
    return out


# ------------------------------------------------------------------ cost join

def load_sessions(path, repo):
    """Claude Code transcripts -> [(start_ts, end_ts, tokens, cost_usd)] for sessions run inside `repo`."""
    repo = os.path.realpath(repo)
    sessions = []
    for f in glob.glob(os.path.join(os.path.expanduser(path), "**", "*.jsonl"), recursive=True):
        ts, tokens, cost, inside = [], 0, 0.0, False
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cwd = rec.get("cwd")
                if cwd and (os.path.realpath(cwd) + os.sep).startswith(repo + os.sep):
                    inside = True
                t = rec.get("timestamp")
                if t:
                    try:
                        ts.append(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        pass
                usage = (rec.get("message") or {}).get("usage") or {}
                tokens += sum(v for k, v in usage.items() if k.endswith("tokens") and isinstance(v, int))
                cost += rec.get("costUSD") or 0.0
        if inside and ts:
            sessions.append((min(ts), max(ts), tokens, cost))
    return sessions


def agent_commit_times(state):
    return sorted({d[0] for d in state.dead if group_of(d[2]) == "agent"} |
                  {l[0] for ls in state.files.values() for l in ls if group_of(l[1]) == "agent"})


def join_costs(state, sessions, window=2 * 3600):
    """Attribute each session to the agent commit(s) made during it (or within `window` after).
    A session matching several commits is split evenly; a session matching none is left out."""
    times = agent_commit_times(state)
    tokens, cost, matched = defaultdict(float), defaultdict(float), 0
    for start, end, tok, usd in sessions:
        hits = [t for t in times if start <= t <= end + window]
        if not hits:
            continue
        matched += 1
        for t in hits:
            tokens[t] += tok / len(hits)
            cost[t] += usd / len(hits)
    return tokens, cost, matched, len(times)


# ------------------------------------------------------------------ output

MIN_COMMITS = 10


def fmt_surv(v, commits=None):
    s, (lo, hi) = v
    if commits is not None and commits < MIN_COMMITS:
        return "%3.0f%%  (too few commits for an interval)" % (100 * s)
    return "%3.0f%%  (95%% CI %.0f–%.0f%%)" % (100 * s, 100 * lo, 100 * hi)


def report(res, out):
    c = res["commits"]
    if res.get("since"):
        out.write("Comparing lines written since the first agent commit (%s). Use --all-history for everything.\n" %
                  datetime.fromtimestamp(res["since"], timezone.utc).strftime("%Y-%m-%d"))
    out.write("Commits: %s\n\n" % ", ".join("%s %d" % (k, v) for k, v in sorted(c.items(), key=lambda x: -x[1])))
    for g in ("agent", "human"):
        if g not in res["groups"]:
            out.write("No %s-written lines found.\n\n" % g)
            continue
        s = res["groups"][g]
        med = "%.0f days" % s["median_days"] if s["median_days"] is not None else "not reached"
        out.write("%s lines: %d written in %s commits, %d later modified or deleted, median life %s\n" % (
            g.capitalize(), s["lines"], s.get("commits", "?"), s["deaths"], med))
        for h, v in (s.get("survival_clustered") or s["survival"]).items():
            out.write("  still alive after %2d days: %s\n" % (h, fmt_surv(v, s.get("commits"))))
        out.write("\n")
    if res.get("diff_clustered"):
        for h in (30, 90):
            a_ = res["groups"]["agent"]["survival"][h][0] - res["groups"]["human"]["survival"][h][0]
            lo, hi = res["diff_clustered"][h]
            verdict = "no detectable difference" if lo <= 0 <= hi else ("agent lines last longer" if lo > 0 else
                                                                         "agent lines die sooner")
            out.write("Agent − human, alive after %d days: %+.0f pts (95%% CI %+.0f…%+.0f) → %s\n" % (
                h, 100 * a_, 100 * lo, 100 * hi, verdict))
        out.write("(Intervals resample whole commits: lines written together aren't independent.)\n")
    if "logrank" in res:
        cf = res["confound"]
        out.write("Touched by someone else while alive: agent %.0f%%, human %.0f%%\n" % (
            100 * cf["agent_exposed_share"], 100 * cf["human_exposed_share"]))
        if cf["flag"]:
            out.write("⚠ %s\n" % cf["note"])
    if len(res["by"].get("cls", {})) > 1:
        out.write("\nBy author:\n")
        for k, s in res["by"]["cls"].items():
            out.write("  %-8s %6d lines in %4d commits   alive after 30 days: %s\n" % (
                k, s["lines"], s["commits"], fmt_surv((s.get("survival_clustered") or s["survival"])[30], s["commits"])))
    for key in ("dir", "ext", "kind"):
        if res["by"].get(key):
            out.write("\nBy %s:\n" % key)
            for k, s in res["by"][key].items():
                out.write("  %-16s %6d lines in %4d commits   alive after 30 days: %s\n" % (
                    k, s["lines"], s["commits"], fmt_surv((s.get("survival_clustered") or s["survival"])[30], s["commits"])))
    if res.get("cost"):
        c = res["cost"]
        out.write("\nCost join: %d session(s) matched to %d agent commit(s)\n" % (c["sessions"], c["agent_commits"]))
        if c["tokens_per_line"] is not None:
            out.write("  tokens per agent line still alive after 90 days: %.0f\n" % c["tokens_per_line"])
        if c["usd_per_line"]:
            out.write("  $ per agent line still alive after 90 days: $%.3f\n" % c["usd_per_line"])
    if res["skipped"]:
        out.write("\nSkipped: %s\n" % ", ".join("%s %d" % kv for kv in res["skipped"].items()))


def svg_curves(res, width=640, height=320):
    colors = {"agent": "#d9480f", "human": "#1971c2"}
    max_t = max([t for g in res["groups"].values() for t, _ in g["curve"]] + [90])
    x = lambda t: 50 + (width - 90) * min(t, max_t) / max_t  # noqa: E731
    y = lambda s: 20 + (height - 60) * (1 - s)  # noqa: E731
    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="Survival curves">' % (width, height),
         '<line x1="50" y1="%d" x2="%d" y2="%d" stroke="currentColor" stroke-opacity=".4"/>' % (y(0), width - 40, y(0)),
         '<line x1="50" y1="20" x2="50" y2="%d" stroke="currentColor" stroke-opacity=".4"/>' % y(0)]
    for s in (0, 0.5, 1):
        p.append('<text x="44" y="%d" font-size="11" text-anchor="end" fill="currentColor">%d%%</text>' % (y(s) + 4, 100 * s))
    for t in sorted({0, 30, 90, int(max_t)}):
        p.append('<text x="%d" y="%d" font-size="11" text-anchor="middle" fill="currentColor">%dd</text>' % (x(t), y(0) + 16, t))
    for g, s in res["groups"].items():
        if g not in colors:
            continue
        d, last = ["M %.1f %.1f" % (x(0), y(1))], 1.0
        for t, sv in s["curve"]:
            d.append("H %.1f V %.1f" % (x(t), y(sv)))
            last = sv
        d.append("H %.1f" % x(max_t))
        p.append('<path d="%s" fill="none" stroke="%s" stroke-width="2"/>' % (" ".join(d), colors[g]))
        p.append('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>' % (x(max_t) + 4, y(last) + 4, colors[g], g))
    p.append("</svg>")
    return "".join(p)


def html_report(res, name):
    body = []
    report(res, type("W", (), {"write": lambda self, s: body.append(s)})())
    return ("<!doctype html><meta charset=utf-8><title>shelf-life: %s</title>"
            "<style>body{font:15px system-ui;max-width:760px;margin:40px auto;padding:0 16px;color:#1a1a1a;background:#fff}"
            "pre{white-space:pre-wrap}@media(prefers-color-scheme:dark){body{background:#111;color:#eee}}</style>"
            "<h1>shelf-life: %s</h1><p>Share of lines still alive, by age (Kaplan–Meier).</p>%s<pre>%s</pre>") % (
        html.escape(name), html.escape(name), svg_curves(res), html.escape("".join(body)))


# ------------------------------------------------------------------ CLI

def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="shelf-life", description="How long does your agent's code actually survive?")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--by", action="append", choices=["cls", "dir", "ext", "kind"], help="breakdowns (default: cls)")
    ap.add_argument("--count-whitespace", action="store_true", help="count whitespace-only edits as deaths")
    ap.add_argument("--agent-pattern", action="append", metavar="NAME=REGEX", help="add or override an agent pattern")
    ap.add_argument("--exclude", action="append", metavar="GLOB", default=[], help="skip matching paths (repeatable)")
    ap.add_argument("--include-generated", action="store_true",
                    help="also track lockfiles, snapshots, build output etc. (skewed: they churn by regeneration)")
    ap.add_argument("--transcripts", metavar="DIR", help="Claude Code transcripts for the cost join (e.g. ~/.claude/projects)")
    ap.add_argument("--all-history", action="store_true",
                    help="compare all lines ever written (default: only since the first agent commit, same era)")
    ap.add_argument("--bootstrap", type=int, default=200, metavar="N",
                    help="commit-level bootstrap resamples for the intervals (default 200; 0 = off)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="FILE")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    agents = list(AGENTS)
    for spec in a.agent_pattern or []:
        name, _, rx = spec.partition("=")
        agents = [(n, r) for n, r in agents if n != name] + [(name, rx)]
    repo = os.path.abspath(a.repo)
    state = replay(repo, agents, a.count_whitespace, cache=not a.no_cache,
                   exclude=build_exclude(a.include_generated, a.exclude))
    res = analyze(state, tuple(a.by or ["cls"]), a.all_history, a.bootstrap)
    if a.transcripts:
        tokens, cost, matched, n_commits = join_costs(state, load_sessions(a.transcripts, repo))
        n90 = sum(1 for o in observations(state) if o["group"] == "agent" and o["days"] >= 90)
        res["cost"] = {"sessions": matched, "agent_commits": n_commits, "lines_alive_90d": n90,
                       "tokens": sum(tokens.values()), "usd": sum(cost.values()),
                       "tokens_per_line": sum(tokens.values()) / n90 if n90 else None,
                       "usd_per_line": sum(cost.values()) / n90 if n90 else None}
    if a.json:
        out.write(json.dumps(res, indent=2, default=str) + "\n")
    else:
        report(res, out)
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(html_report(res, os.path.basename(repo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
