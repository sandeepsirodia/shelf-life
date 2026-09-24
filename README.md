<h1 align="center">half-life</h1>

<p align="center">
  <em>How long does your agent's code actually survive?</em>
</p>

<p align="center">
  <a href="https://github.com/sandeepsirodia/half-life/actions/workflows/ci.yml"><img src="https://github.com/sandeepsirodia/half-life/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/dependencies-0-111111?style=flat-square" alt="Zero dependencies">
  <img src="https://img.shields.io/badge/stats-checked%20against%20R-111111?style=flat-square" alt="Stats checked against R">
  <img src="https://img.shields.io/badge/license-MIT-111111?style=flat-square" alt="MIT">
</p>

---

You've seen the headline: **"AI-written code lasts longer than human code."**

Maybe. Or maybe nobody dares touch it. Or maybe it's being compared against lockfiles and a three-year-old prototype phase.

half-life runs **survival analysis** on your own git history, the same statistics medicine uses to ask "how long do patients survive on this drug?". Every line is tracked from the commit that wrote it until the commit that changed or deleted it. Agent-written lines (from `Co-Authored-By: Claude / Codex / Copilot / Cursor…` trailers) are compared with human-written ones.

```console
$ uvx --from git+https://github.com/sandeepsirodia/half-life half-life

Comparing lines written since the first agent commit (2025-10-08).
Agent lines: 10051 written, … 
  still alive after 30 days:  75%  (95% CI 74–76%)
Human lines: 75239 written, …
  still alive after 30 days:  94%  (95% CI 93–94%)

Touched by someone else while alive: agent 96%, human 30%
```

## What happened when I ran it on four well-known repos

Lines written since each repo's first agent commit, with generated files (lockfiles, snapshots, build output) excluded:

| Repo | Agent lines alive after 30 / 90 days | Human lines alive after 30 / 90 days | Touched by someone else (agent / human) |
|---|---|---|---|
| [simonw/llm](https://github.com/simonw/llm) | 99% / 96% | 98% / 94% | 72% / 67% |
| [astral-sh/uv](https://github.com/astral-sh/uv) | 92% / 88% | 87% / 81% | 87% / 86% |
| [tldraw/tldraw](https://github.com/tldraw/tldraw) | 95% / 79% | 88% / 75% | 77% / 75% |
| [simonw/datasette](https://github.com/simonw/datasette) | **75% / 73%** | **94% / 91%** | **96% / 30%** |

- **In three repos, agent code survives slightly longer**, by a few points, not a landslide.
- **In datasette, it's the opposite.** A quarter of agent lines get rewritten within a month, and 96% of them are touched by someone else. That looks like a maintainer reviewing and reworking agent output, which is exactly what you'd hope happens.

Raw results for all four are in [`results/`](results/).

## The same repo, three ways

Here's why "AI code lasts longer" needs an asterisk. Same repo (tldraw), same tool, human lines alive after 30 days:

| How you compare | Human lines alive after 30 days | Agent |
|---|---|---|
| Naive: every file, all of history | **32%** | 95% |
| Excluding regenerated files (lockfiles, snapshots…) | 66% | 95% |
| **Fair: also only lines written since agents arrived** | **88%** | 95% |

A 63-point gap shrinks to 7. Most of it was lockfiles being regenerated, plus the project's own early churn. So half-life does the fair version by default, and tells you what it excluded.

## Built for people who'll check the math

- **Kaplan–Meier with censoring.** A line still alive today isn't "immortal"; it's *censored*. Treating it as immortal is the classic mistake, and the reason survival analysis exists.
- **Greenwood intervals and a log-rank test.** The Kaplan–Meier curve and standard errors match R's `survfit`, and the log-rank test matches R's `survdiff`, on the classic Freireich leukemia dataset (the example in every survival-analysis textbook). Those values are [in the tests](tests/test_half_life.py).
- **The confound check.** "Touched by someone else while alive" separates *code that survives because it's good* from *code that survives because nobody works on it*. If agent code is rarely touched by anyone else, half-life warns you.
- **Renames aren't deaths** (git rename detection), **reindents aren't deaths** (whitespace-only edits keep the line alive), and **binaries and generated files are skipped** and counted.

## Options

| | |
|---|---|
| `half-life [repo]` | Default: fair comparison, breakdown by agent |
| `--by dir` · `--by ext` · `--by kind` | Also break down by top-level directory, file type, or code vs tests |
| `--all-history` | Include lines from before the first agent commit |
| `--include-generated` · `--exclude 'fixtures/*'` | Control which files count |
| `--agent-pattern 'mybot=my-bot@corp\.com'` | Teach it your own agent's signature |
| `--transcripts ~/.claude/projects` | Join Claude Code session costs: **tokens and $ per agent line still alive after 90 days** |
| `--html report.html` | One self-contained file with the survival curves |
| `--json` | Everything, machine-readable |

Speed, measured: uv's 10.6k commits in ~18 s, tldraw's 6.3k (huge diffs) in ~70 s. Results are cached in `.git/half-life/`, so reruns are near-instant.

## Honest limits

- **Attribution relies on commit trailers.** Agent code committed without `Co-Authored-By` counts as human, which dilutes the difference. half-life won't guess authorship from style; that's unreliable and unfair.
- **Squash merges help, merge commits hurt.** It follows first-parent history, so a merge commit's lines are credited to whoever merged.
- **Survival isn't quality.** A line can survive because it's perfect or because it's dead code. The confound check helps, but read the numbers as signals, not verdicts.
- The comparisons are between *lines*, and lines aren't independent (a whole file can be deleted at once). The confidence intervals are therefore somewhat optimistic, and the p-values much more so.

<details>
<summary><b>Development</b></summary>

```bash
python -m unittest discover -s tests -v
```

Tests map 1:1 to [SPEC.md](SPEC.md). Line tracking is tested on real git repos built with controlled authors and timestamps, and the 10k-commit performance test builds its repo with `git fast-import`.

</details>

<p align="center"><sub>MIT © Sandeep Sirodia · Ran it on your repo and found something surprising? Open an issue with your numbers. A ⭐ helps others find it.</sub></p>
