"""Tests map 1:1 to SPEC.md (E1..E12). Statistical references come from published outputs of R's
survival package on the classic Freireich (1963) leukemia trial, never from this code."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shelf_life as hl  # noqa: E402

DAY = 86400
T0 = 1_700_000_000  # fixed epoch for fixtures

# Freireich et al. (1963), 6-MP arm: remission times in weeks, 0 = censored.
SIX_MP = [(6, 1), (6, 1), (6, 1), (6, 0), (7, 1), (9, 0), (10, 1), (10, 0), (11, 0), (13, 1), (16, 1), (17, 0),
          (19, 0), (20, 0), (22, 1), (23, 1), (25, 0), (32, 0), (32, 0), (34, 0), (35, 0)]
PLACEBO = [1, 1, 2, 2, 3, 4, 4, 5, 5, 8, 8, 8, 8, 11, 11, 12, 12, 15, 17, 22, 23]
# R: summary(survfit(Surv(time, status) ~ 1, data = six_mp)): time, surv, std.err
R_KM = [(6, 0.857, 0.0764), (7, 0.807, 0.0869), (10, 0.753, 0.0963), (13, 0.690, 0.1068),
        (16, 0.627, 0.1141), (22, 0.538, 0.1282), (23, 0.448, 0.1346)]


class Repo:
    """A git repo whose commits get controlled authors, trailers and timestamps."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="hl-")
        self.git("init", "-q", "-b", "main")

    def git(self, *args, day=0, author="Hana <hana@example.com>"):
        name, email = re.match(r"(.*) <(.*)>", author).groups()
        env = dict(os.environ, GIT_AUTHOR_DATE="%d +0000" % (T0 + day * DAY), GIT_COMMITTER_DATE="%d +0000" % (T0 + day * DAY),
                   GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=self.root, env=env, check=True, capture_output=True, text=True).stdout

    def commit(self, files, day, msg="change", author="Hana <hana@example.com>", trailer=None):
        for path, text in files.items():
            full = os.path.join(self.root, path)
            if text is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg + ("\n\n" + trailer if trailer else ""), day=day, author=author)

    def state(self, **kw):
        return hl.replay(self.root, cache=False, **kw)


CLAUDE = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"


class TestStatistics(unittest.TestCase):
    def test_e6_kaplan_meier_matches_r(self):
        curve = hl.kaplan_meier([t for t, _ in SIX_MP], [e for _, e in SIX_MP])
        got = [(t, round(s, 3), round(se, 4)) for t, _, _, s, se in curve]
        self.assertEqual(got, R_KM)
        self.assertEqual(hl.median_survival(curve), 23)

    def test_e7_logrank_matches_r(self):
        # R: survdiff(Surv(time, status) ~ group) -> Chisq = 16.8 on 1 df, p = 4e-05; 6-MP: O = 9, E = 19.25
        chi2, p, o, e = hl.logrank([t for t, _ in SIX_MP], [e for _, e in SIX_MP], PLACEBO, [1] * len(PLACEBO))
        self.assertAlmostEqual(chi2, 16.8, places=1)
        self.assertAlmostEqual(e, 19.25, places=2)
        self.assertEqual(o, 9)
        self.assertLess(p, 1e-4)

    def test_loglog_interval_stays_in_bounds(self):
        curve = hl.kaplan_meier([t for t, _ in SIX_MP], [e for _, e in SIX_MP])
        for t in (6, 13, 23):
            s, (lo, hi) = hl.survival_at(curve, t)
            self.assertTrue(0 <= lo < s < hi <= 1)


class TestClusteredIntervals(unittest.TestCase):
    def test_km_at_matches_unweighted_km(self):
        rows = sorted((t, e, 1) for t, e in SIX_MP)
        got = hl.km_at(rows, (6, 13, 23, 40))
        self.assertAlmostEqual(got[13], 0.690, places=3)
        self.assertAlmostEqual(got[23], 0.448, places=3)
        self.assertAlmostEqual(got[40], 0.448, places=3)

    def test_clustered_interval_is_wider_than_greenwood_when_lines_move_together(self):
        # 40 commits of 200 lines each; every line in a commit dies (or not) together
        import random
        rng = random.Random(1)
        obs = []
        for c in range(40):
            dies = rng.random() < 0.5
            days = rng.uniform(1, 60) if dies else 100.0
            obs += [{"commit": c, "days": days, "event": int(dies)} for _ in range(200)]
        cb = hl.cluster_bootstrap({"agent": obs}, horizons=(30,), reps=300)
        lo, hi = cb["agent"][30]
        curve = hl.kaplan_meier([o["days"] for o in obs], [o["event"] for o in obs])
        _, (glo, ghi) = hl.survival_at(curve, 30)
        self.assertGreater(hi - lo, 5 * (ghi - glo))  # naive interval pretends 8000 lines = 8000 samples

    def test_bootstrap_is_deterministic(self):
        obs = [{"commit": c % 7, "days": float(c), "event": c % 2} for c in range(50)]
        self.assertEqual(hl.cluster_bootstrap({"agent": obs}), hl.cluster_bootstrap({"agent": obs}))


class TestAttribution(unittest.TestCase):
    def test_e1_trailers(self):
        self.assertEqual(hl.classify("Hana <h@x>", "fix\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"), "claude")
        self.assertEqual(hl.classify("Hana <h@x>", "fix\n\nco-authored-by: Codex <codex@openai.com>"), "codex")
        self.assertEqual(hl.classify("Hana <h@x>", "fix the claude integration docs"), "human")  # body mention ≠ trailer
        self.assertEqual(hl.classify("dependabot[bot] <x@github.com>", "bump"), "bot")
        self.assertEqual(hl.classify("Copilot <198982749+Copilot@users.noreply.github.com>", "x"), "copilot")


class TestTracking(unittest.TestCase):
    def test_e2_modification_is_a_death(self):
        r = Repo()
        r.commit({"a.py": "x = 1\ny = 2\n"}, day=0)
        r.commit({"a.py": "x = 1\ny = 3\n"}, day=10)
        st = r.state()
        self.assertEqual([(d[0], d[1]) for d in st.dead], [(T0, T0 + 10 * DAY)])

    def test_e3_alive_lines_are_censored(self):
        r = Repo()
        r.commit({"a.py": "x = 1\n"}, day=0)
        r.commit({"b.py": "z = 1\n"}, day=50)
        obs = [o for o in hl.observations(r.state()) if o["path"] == "a.py"]
        self.assertEqual([(o["days"], o["event"]) for o in obs], [(50.0, 0)])

    def test_e4_rename_is_not_a_death(self):
        r = Repo()
        r.commit({"a.py": "x = 1\ny = 2\n"}, day=0)
        r.git("mv", "a.py", "b.py")
        r.git("commit", "-q", "-m", "rename", day=5)
        st = r.state()
        self.assertEqual(st.dead, [])
        self.assertEqual(len(st.files["b.py"]), 2)

    def test_e5_whitespace_only(self):
        r = Repo()
        r.commit({"a.py": "if x:\n  y = 1\n"}, day=0)
        r.commit({"a.py": "if x:\n    y = 1\n"}, day=3)
        self.assertEqual(r.state().dead, [])
        self.assertEqual(len(hl.replay(r.root, cache=False, count_whitespace=True).dead), 1)

    def test_insertions_and_deletions_keep_other_lines_alive(self):
        r = Repo()
        r.commit({"a.py": "a\nb\nc\nd\n"}, day=0)
        r.commit({"a.py": "a\nNEW\nb\nd\n"}, day=1)  # insert after a, delete c
        st = r.state()
        self.assertEqual(len(st.dead), 1)
        self.assertEqual([l[4] for l in st.files["a.py"]], ["a", "NEW", "b", "d"])
        self.assertEqual([l[0] for l in st.files["a.py"]], [T0, T0 + DAY, T0, T0])

    def test_e8_confound_flag(self):
        r = Repo()
        # agent writes a module nobody else ever touches; two humans keep editing each other's file
        r.commit({"agent.py": "".join("a%d = %d\n" % (i, i) for i in range(30))}, day=0, trailer=CLAUDE)
        r.commit({"human.py": "".join("h%d = %d\n" % (i, i) for i in range(30))}, day=0)
        for d in range(1, 12):
            who = "Ana <ana@example.com>" if d % 2 else "Hana <hana@example.com>"
            r.commit({"human.py": "".join("h%d = %d\n" % (i, i + d) for i in range(30))}, day=d * 3, author=who)
        res = hl.analyze(r.state(), all_history=True)
        self.assertTrue(res["confound"]["flag"])
        self.assertIn("rarely touched", res["confound"]["note"])
        self.assertLess(res["confound"]["agent_exposed_share"], res["confound"]["human_exposed_share"])

    def test_same_era_comparison_by_default(self):
        r = Repo()
        r.commit({"old.py": "a = 1\n"}, day=0)                       # human, long before any agent
        r.commit({"old.py": "a = 2\n"}, day=1)
        r.commit({"agent.py": "b = 1\n"}, day=100, trailer=CLAUDE)
        r.commit({"new.py": "c = 1\n"}, day=101)
        res = hl.analyze(r.state())
        self.assertEqual(res["groups"]["human"]["lines"], 1)       # only new.py: same era as the agent
        self.assertEqual(hl.analyze(r.state(), all_history=True)["groups"]["human"]["lines"], 3)

    def test_e12_merge_binary_empty(self):
        r = Repo()
        r.commit({"a.py": "x = 1\n"}, day=0)
        r.git("checkout", "-q", "-b", "feat")
        r.commit({"b.py": "y = 1\n"}, day=1, trailer=CLAUDE)
        with open(os.path.join(r.root, "img.bin"), "wb") as f:
            f.write(bytes(range(256)))
        r.commit({}, day=2, msg="add binary")
        r.git("checkout", "-q", "main")
        r.git("merge", "-q", "--no-ff", "feat", "-m", "merge feat", day=3)
        r.commit({}, day=4, msg="empty")
        st = r.state()
        self.assertIn("b.py", st.files)
        self.assertEqual(st.skipped.get("binary file changes"), 1)
        hl.analyze(st)  # no crash


class TestGenerated(unittest.TestCase):
    def test_e13_generated_files_excluded_by_default(self):
        # Regression: on tldraw, regenerated lockfiles/snapshots made "human" lines look like they die in 0 days.
        r = Repo()
        r.commit({"src/a.py": "x = 1\n", "yarn.lock": "a@1\n", "tests/__snapshots__/a.snap": "s1\n",
                  "dist/app.min.js": "m1\n", "CHANGELOG.md": "v1\n"}, day=0)
        r.commit({"yarn.lock": "a@2\n", "tests/__snapshots__/a.snap": "s2\n", "dist/app.min.js": "m2\n",
                  "CHANGELOG.md": "v2\n"}, day=1)
        st = r.state()
        self.assertEqual(sorted(st.files), ["src/a.py"])
        self.assertEqual(st.dead, [])
        self.assertEqual(st.skipped["generated/excluded file changes"], 8)
        everything = hl.replay(r.root, cache=False, exclude=hl.build_exclude(include_generated=True))
        self.assertEqual(len(everything.dead), 4)

    def test_user_exclude_glob(self):
        r = Repo()
        r.commit({"src/a.py": "x = 1\n", "fixtures/big.json": "{}\n"}, day=0)
        st = hl.replay(r.root, cache=False, exclude=hl.build_exclude(extra=["fixtures/*"]))
        self.assertEqual(sorted(st.files), ["src/a.py"])


class TestCostJoin(unittest.TestCase):
    def test_e9_sessions_match_agent_commits(self):
        r = Repo()
        r.commit({"a.py": "x = 1\n"}, day=0, trailer=CLAUDE)
        r.commit({"b.py": "y = 1\n"}, day=100)
        st = r.state()
        tdir = tempfile.mkdtemp()
        iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))  # noqa: E731
        with open(os.path.join(tdir, "s1.jsonl"), "w") as f:  # a session around the agent commit
            for ts in (T0 - 600, T0 - 60):
                f.write(json.dumps({"cwd": r.root, "timestamp": iso(ts), "costUSD": 0.5,
                                    "message": {"usage": {"input_tokens": 1000, "output_tokens": 200}}}) + "\n")
        with open(os.path.join(tdir, "s2.jsonl"), "w") as f:  # unrelated project: must be ignored
            f.write(json.dumps({"cwd": "/elsewhere", "timestamp": iso(T0), "costUSD": 9.0}) + "\n")
        with open(os.path.join(tdir, "s3.jsonl"), "w") as f:  # same repo, but no agent commit nearby
            f.write(json.dumps({"cwd": r.root, "timestamp": iso(T0 + 50 * DAY), "costUSD": 3.0}) + "\n")
        sessions = hl.load_sessions(tdir, r.root)
        self.assertEqual(len(sessions), 2)
        tokens, cost, matched, n = hl.join_costs(st, sessions)
        self.assertEqual((matched, n), (1, 1))
        self.assertAlmostEqual(sum(cost.values()), 1.0)
        self.assertEqual(sum(tokens.values()), 2400)


class TestOutputAndSpeed(unittest.TestCase):
    def test_e11_html_is_self_contained(self):
        r = Repo()
        r.commit({"a.py": "".join("x%d = 1\n" % i for i in range(20))}, day=0, trailer=CLAUDE)
        r.commit({"b.py": "".join("y%d = 1\n" % i for i in range(20))}, day=1)
        r.commit({"a.py": "x0 = 2\n", "b.py": "y0 = 2\n"}, day=40)
        path = os.path.join(tempfile.mkdtemp(), "r.html")
        hl.main([r.root, "--no-cache", "--html", path], out=io.StringIO())
        page = open(path).read()
        self.assertIn("<svg", page)
        self.assertEqual(re.findall(r"(?:src|href)=\"(https?:[^\"]+)", page), [])
        self.assertEqual([u for u in re.findall(r"https?://[^\"' )]+", page) if "w3.org/2000/svg" not in u], [])

    def test_e10_ten_thousand_commits(self):
        """10k commits via git fast-import: full replay < 60 s, cached rerun < 5 s."""
        r = Repo()
        stream, mark = [], 0
        for i in range(10_000):
            body = "".join("line %d of %d\n" % (j, i if j == i % 20 else 0) for j in range(20))
            msg = "c%d" % i + ("\n\n" + CLAUDE if i % 3 == 0 else "")
            stream.append("commit refs/heads/main\ncommitter Hana <hana@example.com> %d +0000\n"
                          "data %d\n%s\nM 644 inline f%d.txt\ndata %d\n%s\n" % (
                              T0 + i * 600, len(msg.encode()), msg, i % 50, len(body.encode()), body))
        subprocess.run(["git", "fast-import", "--quiet"], cwd=r.root, input="".join(stream).encode(), check=True)
        subprocess.run(["git", "checkout", "-q", "-f", "main"], cwd=r.root, check=True)
        start = time.time()
        st = hl.replay(r.root)
        first = time.time() - start
        self.assertEqual(sum(st.commits.values()), 10_000)
        start = time.time()
        hl.replay(r.root)
        self.assertLess(first, 60)
        self.assertLess(time.time() - start, 5)


if __name__ == "__main__":
    unittest.main()
