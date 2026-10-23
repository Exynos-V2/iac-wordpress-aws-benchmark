#!/usr/bin/env python3
"""Manage thesis IaC benchmark runs: show table, add rows, compute stats."""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "runs.csv"
LOGS_DIR = ROOT / "logs"
IDEMPOTENCY_CSV = ROOT / "idempotency.csv"
IDEM_FIELDS = ["stack", "date", "iteration", "changed_count", "status", "summary"]
STACK_DIRS = {"Ansible": ROOT / "terraform-ansible", "Chef": ROOT / "terraform-chef"}
CFN_DIR = ROOT / "cloudformation"

# Patterns are matched ONLY against the failure region (text after the last
# "Error:"), never the full Terraform plan — plan output contains attribute
# names like "...allow_credentials_header..." that caused false positives.
# Keep needles specific; avoid bare common words.
ERROR_PATTERNS = [
    # Environmental (Learner Lab / AWS) — specific API tokens only.
    ("Auth (STS expired)", ["ExpiredToken", "InvalidClientTokenId", "RequestExpired",
                            "security token included in the request is expired",
                            "credentials have expired", "AuthFailure"]),
    # Operator-side: AWS CLI has no creds loaded (e.g. WSL session before Learner
    # Lab creds were exported). Environmental — not a stack failure.
    ("Auth (no credentials)", ["NoCredentials", "Unable to locate credentials",
                               "Unable to load credentials", "configure credentials by running"]),
    ("Provisioning (AWS capacity)", ["InsufficientInstanceCapacity"]),
    ("Provisioning (quota)", ["VcpuLimitExceeded", "InstanceLimitExceeded", "LimitExceeded"]),
    ("Provisioning (AWS API)", ["RequestLimitExceeded", "ThrottlingException", "Throttling"]),
    # CloudFormation stack failures (cfn-run path).
    ("Provisioning (CloudFormation)", ["ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETE",
                                       "CREATE_FAILED", "UPDATE_ROLLBACK",
                                       "ResourceNotReady", "Resource creation cancelled",
                                       "Embedded stack", "WaitCondition", "did not stabilize"]),
    # Configuration management — package/apt failures bucketed before generic CM.
    ("Config (apt/package)", ["Could not get lock", "Unable to acquire the dpkg lock",
                              "dpkg was interrupted", "Failed to fetch",
                              "Temporary failure resolving", "Hash Sum mismatch"]),
    ("Config (Chef cookbook)", ["chef-client", "Chef::", "Chef Infra", "Cookbook"]),
    ("Config (Ansible)", ["ansible-playbook", "PLAY RECAP", "fatal:", "TASK ["]),
    ("Config (SSH)", ["Failed to connect to the host via ssh", "Permission denied (publickey)",
                      "Connection timed out", "ssh:"]),
    ("Config (timeout)", ["timeout while waiting", "context deadline exceeded",
                          "wait_for_capacity_timeout"]),
    ("Terraform (state lock)", ["Error acquiring the state lock"]),
    ("Terraform (dependency)", ["Cycle:"]),
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOXCHARS = "│╷╵╶╴┌┐└┘├┤┬┴┼─╶╷"


def _clean(text: str) -> str:
    """Strip ANSI color codes and box-drawing chars Terraform prints."""
    text = _ANSI_RE.sub("", text)
    return "".join(" " if ch in _BOXCHARS else ch for ch in text)


def _failure_region(log_text: str) -> str:
    """Return the part of the log that actually describes the failure.

    Terraform prints the plan first (huge, full of attribute names that trigger
    false matches) then 'Error:' at the failure. Classify on the error region.
    """
    cleaned = _clean(log_text)
    idx = cleaned.rfind("Error:")
    return cleaned[idx:] if idx != -1 else cleaned[-4000:]


def classify_error(log_text: str) -> str:
    region = _failure_region(log_text).lower()
    # Abrupt Ansible termination: tasks were running but the play never produced
    # a 'PLAY RECAP' and there's no task-level 'fatal:' / 'unreachable'. This is
    # the signature of the control node (WSL / ansible-playbook) being killed
    # mid-run (laptop sleep, WSL shutdown) rather than a real task failure.
    looks_ansible = "task [" in region or "play [" in region
    if (looks_ansible and "play recap" not in region
            and "fatal:" not in region and "unreachable" not in region):
        return "Config (Ansible incomplete/terminated)"
    for label, needles in ERROR_PATTERNS:
        if any(n.lower() in region for n in needles):
            return label
    return "Apply failed (see log)"


def _tail(text: str, n: int = 3) -> str:
    cleaned = _clean(text)
    lines = []
    for ln in cleaned.splitlines():
        ascii_ln = "".join(ch for ch in ln if 32 <= ord(ch) < 127).strip()
        # Skip blank lines and decoration-only lines (asterisks, dashes, etc.).
        if ascii_ln and any(ch.isalnum() for ch in ascii_ln):
            lines.append(" ".join(ascii_ln.split()))
    return " | ".join(lines[-n:])[:300] if lines else "(no readable error text in log; see file)"
FIELDS = ["run_id", "date", "stack", "tf_apply_s", "config_s", "total_s", "status", "error_category", "log_path"]
HEADERS = ["Run", "Date", "Stack", "TF Apply (s)", "Config (s)", "Total (s)", "Status", "Error Category", "Log"]
STACKS = ("Ansible", "Chef")
STATS_STACKS = ("Ansible", "Chef", "CloudFormation")  # groups shown/compared by `stats`


def read_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def fmt(v: str) -> str:
    return v if v not in (None, "") else "-"


def render_table(rows: list[dict]) -> str:
    data = [[fmt(r.get(k, "")) for k in FIELDS] for r in rows]
    widths = [len(h) for h in HEADERS]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    out = [line(HEADERS), sep]
    out.extend(line(r) for r in data)
    return "\n".join(out)


def cmd_show(_args) -> int:
    rows = read_rows()
    if not rows:
        print("No runs recorded yet. Add one with: runs.py add ...")
        return 0
    print(render_table(rows))
    print(f"\n{len(rows)} run(s) in {CSV_PATH.name}")
    return 0


def cmd_add(args) -> int:
    rows = read_rows()
    next_id = max((int(r["run_id"]) for r in rows), default=0) + 1
    total = args.total if args.total is not None else (
        (args.tf_apply or 0) + (args.config or 0) if args.tf_apply is not None else None
    )
    row = {
        "run_id": str(next_id),
        "date": args.date,
        "stack": args.stack,
        "tf_apply_s": "" if args.tf_apply is None else f"{args.tf_apply:.1f}",
        "config_s": "" if args.config is None else f"{args.config:.1f}",
        "total_s": "" if total is None else f"{total:.1f}",
        "status": args.status,
        "error_category": args.error or ("None" if args.status == "Success" else ""),
    }
    rows.append(row)
    write_rows(rows)
    print(f"Added run {next_id} ({row['stack']}, {row['status']}).")
    print(render_table([row]))
    return 0


def shapiro_wilk(xs: list[float]) -> tuple[float, float] | None:
    try:
        from scipy.stats import shapiro
    except ImportError:
        return None
    if len(xs) < 3:
        return None
    s = shapiro(xs)
    return float(s.statistic), float(s.pvalue)


def welch_ttest(a: list[float], b: list[float]) -> tuple[float, float] | None:
    try:
        from scipy.stats import ttest_ind
    except ImportError:
        return None
    if len(a) < 2 or len(b) < 2:
        return None
    t = ttest_ind(a, b, equal_var=False)
    return float(t.statistic), float(t.pvalue)


def mann_whitney(a: list[float], b: list[float]) -> tuple[float, float] | None:
    try:
        from scipy.stats import mannwhitneyu
    except ImportError:
        return None
    if len(a) < 2 or len(b) < 2:
        return None
    # Exact p, not the normal approximation: n = 10 per group is small. SciPy's
    # exact method ignores ties, so tied samples use the exact permutation
    # distribution of U instead (C(20,10) = 184756 splits, still fast).
    if len(set(a) | set(b)) == len(a) + len(b):
        u = mannwhitneyu(a, b, alternative="two-sided", method="exact")
        return float(u.statistic), float(u.pvalue)
    from scipy.stats import permutation_test
    res = permutation_test(
        (a, b), lambda x, y, axis: mannwhitneyu(x, y, axis=axis).statistic,
        permutation_type="independent", n_resamples=math.inf,
        alternative="two-sided", vectorized=True)
    return float(mannwhitneyu(a, b).statistic), float(res.pvalue)


def compare(a: list[float], b: list[float]) -> tuple[str, float, float] | None:
    """Test chosen as in the article: Welch t if both groups pass Shapiro-Wilk, else Mann-Whitney."""
    sa, sb = shapiro_wilk(a), shapiro_wilk(b)
    both_normal = sa is not None and sb is not None and sa[1] > 0.05 and sb[1] > 0.05
    res = welch_ttest(a, b) if both_normal else mann_whitney(a, b)
    if res is None:
        return None
    return ("t Welch" if both_normal else "Mann-Whitney"), res[0], res[1]


# The nine pairwise tests reported in the article form one family for Holm-Bonferroni.
FAMILY_PAIRS = [("Chef", "Ansible"), ("CloudFormation", "Ansible"), ("CloudFormation", "Chef")]
FAMILY_METRICS = ["total_s", "config_s", "tf_apply_s"]


def holm_family(rows: list[dict]) -> dict[tuple[str, str, str], float]:
    """Holm-adjusted p for every (first, second, metric) test in the article's family."""
    def vals(stack, metric):
        return [float(r[metric]) for r in rows if r["stack"] == stack and r.get(metric)]
    keys, ps = [], []
    for first, second in FAMILY_PAIRS:
        for metric in FAMILY_METRICS:
            res = compare(vals(first, metric), vals(second, metric))
            if res is not None:
                keys.append((first, second, metric))
                ps.append(res[2])
    adj, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(sorted(range(len(ps)), key=lambda i: ps[i])):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    out = dict(zip(keys, adj))
    out.update({(s, f, m): p for (f, s, m), p in list(out.items())})
    return out


def cohens_d(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    na, nb = len(a), len(b)
    sa, sb = stdev(a), stdev(b)
    pooled = math.sqrt(((na - 1) * sa**2 + (nb - 1) * sb**2) / (na + nb - 2))
    if pooled == 0:
        return None
    return (mean(a) - mean(b)) / pooled


def d_magnitude(d: float) -> str:
    ad = abs(d)
    return "negligible" if ad < 0.2 else "small" if ad < 0.5 else "medium" if ad < 0.8 else "large"


def fmt_p(p: float) -> str:
    """Avoid printing a rounded '0.0000' that looks like a literal zero."""
    return "<0.0001" if p < 0.0001 else f"{p:.4f}"


def wilson_ci(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion x/n. Returns (low, high) in [0,1]."""
    if n == 0:
        return (0.0, 1.0)
    p = x / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# Classify a stored error_category string into one of three buckets so
# environmental noise (Learner Lab / AWS) isn't blamed on the CM tool.
ENVIRONMENTAL_PREFIXES = (
    "Auth (", "Provisioning (AWS capacity", "Provisioning (quota",
    "Provisioning (AWS API", "Environment (",
)
STACK_PREFIXES = ("Config (Ansible", "Config (Chef")
# SSH / config-timeout / script-timeout / apt are usually network/runtime or
# host-side timing (e.g. unattended-upgrades holding the dpkg lock), not the CM
# tool's own logic — bucket separately so you can footnote the judgment call.
INDETERMINATE_PREFIXES = (
    "Config (SSH", "Config (timeout", "Config (apt/package",
    "Config (Ansible incomplete", "Script timeout", "Terraform (state lock",
)


def attribution(error_category: str) -> str:
    label = (error_category or "").split("::", 1)[0].strip()
    if any(label.startswith(p) for p in ENVIRONMENTAL_PREFIXES):
        return "environmental"
    # Indeterminate checked before stack: more specific labels like
    # "Config (Ansible incomplete..." must not be swallowed by the broader
    # "Config (Ansible" stack prefix.
    if any(label.startswith(p) for p in INDETERMINATE_PREFIXES):
        return "indeterminate"
    if any(label.startswith(p) for p in STACK_PREFIXES):
        return "stack"
    return "indeterminate"


def cmd_reliability(_args) -> int:
    rows = read_rows()
    print("Reliability (all attempted runs)\n")
    header = f"{'Stack':<10} {'Attempt':>8} {'Success':>8} {'Fail':>5} {'Rate':>7} {'95% Wilson CI':>18}"
    print(header)
    for s in STACKS:
        sub = [r for r in rows if r["stack"] == s]
        n = len(sub)
        succ = sum(1 for r in sub if r["status"] == "Success")
        fail = n - succ
        if n == 0:
            print(f"{s:<10} {0:>8}  (no runs)")
            continue
        rate = succ / n
        lo, hi = wilson_ci(succ, n)
        print(f"{s:<10} {n:>8} {succ:>8} {fail:>5} {rate*100:>6.1f}% "
              f"   [{lo*100:>5.1f}, {hi*100:>5.1f}]")
        if fail == 0:
            ro3 = 3 / n
            print(f"{'':<10} 0 failures -> failure rate <= {ro3*100:.1f}% (rule of three, 95%)")

    # Failure breakdown by attribution + category.
    fails = [r for r in rows if r["status"] != "Success"]
    print("\nFailure breakdown:")
    if not fails:
        print("  No failures recorded across either stack.")
    else:
        for s in STACKS:
            sfails = [r for r in fails if r["stack"] == s]
            if not sfails:
                continue
            print(f"  {s}:")
            buckets: dict[str, list[str]] = {"stack": [], "environmental": [], "indeterminate": []}
            for r in sfails:
                cat = (r.get("error_category") or "").split("::", 1)[0].strip()
                buckets[attribution(r.get("error_category", ""))].append(cat or "(unlabeled)")
            for bucket in ("stack", "environmental", "indeterminate"):
                items = buckets[bucket]
                if not items:
                    continue
                counts: dict[str, int] = {}
                for c in items:
                    counts[c] = counts.get(c, 0) + 1
                detail = ", ".join(f"{k} x{v}" for k, v in counts.items())
                print(f"    [{bucket:<13}] {len(items)} :: {detail}")

    # Stack-attributable rate (the number that actually compares the two tools).
    print("\nStack-attributable failures only (environmental excluded):")
    for s in STACKS:
        sub = [r for r in rows if r["stack"] == s]
        n = len(sub)
        if n == 0:
            continue
        stack_fail = sum(1 for r in sub
                         if r["status"] != "Success" and attribution(r.get("error_category", "")) == "stack")
        eff_n = sum(1 for r in sub
                    if r["status"] == "Success" or attribution(r.get("error_category", "")) == "stack")
        if eff_n == 0:
            print(f"  {s}: no completed or stack-failed runs")
            continue
        succ = eff_n - stack_fail
        lo, hi = wilson_ci(succ, eff_n)
        note = f" (95% Wilson CI [{lo*100:.1f}, {hi*100:.1f}])" if eff_n else ""
        print(f"  {s}: {stack_fail} stack failure(s) in {eff_n} non-environmental run(s) "
              f"-> {succ/eff_n*100:.1f}% success{note}")
    return 0


def cmd_stats(args) -> int:
    rows = [r for r in read_rows() if r["status"] == "Success"]
    metric = args.metric
    groups: dict[str, list[float]] = {s: [] for s in STATS_STACKS}
    for r in rows:
        v = r.get(metric, "")
        if v and r["stack"] in groups:
            try:
                groups[r["stack"]].append(float(v))
            except ValueError:
                pass

    present = [s for s in STATS_STACKS if groups[s]]

    print(f"Metric: {metric}  (successful runs only)\n")
    print(f"{'Stack':<15} {'n':>3} {'mean':>10} {'stdev':>10} {'min':>8} {'max':>8}")
    for s in present:
        xs = groups[s]
        m = mean(xs)
        sd = stdev(xs) if len(xs) > 1 else 0.0
        print(f"{s:<15} {len(xs):>3} {m:>10.2f} {sd:>10.2f} {min(xs):>8.2f} {max(xs):>8.2f}")

    print("\nNormality (Shapiro-Wilk, alpha=0.05):")
    normal_flags: dict[str, bool | None] = {}
    for s in present:
        sw = shapiro_wilk(groups[s])
        if sw is None:
            normal_flags[s] = None
            print(f"  {s}: need n>=3 (or scipy missing)")
        else:
            w, p = sw
            is_normal = p > 0.05
            normal_flags[s] = is_normal
            print(f"  {s}: W={w:.4f}, p={fmt_p(p)}  -> {'normal' if is_normal else 'NOT normal'}")

    # Which two stacks to compare (default Ansible vs Chef = original hypothesis).
    g1, g2 = args.compare
    if not groups[g1] or not groups[g2]:
        print(f"\nNo successful data for one of the compared stacks "
              f"({g1}={len(groups[g1])}, {g2}={len(groups[g2])}); nothing to test.")
        return 0
    print(f"\nComparison: {g1} vs {g2}")
    a, b = groups[g1], groups[g2]

    # Pick the correct test: parametric only when BOTH groups pass normality.
    f1, f2 = normal_flags.get(g1), normal_flags.get(g2)
    both_known = f1 is not None and f2 is not None
    both_normal = both_known and f1 and f2

    if not both_known:
        chosen = "t-test"  # default before normality can be assessed (n<3)
        reason = "normality not yet assessable (n<3); showing t-test by default"
    elif both_normal:
        chosen = "t-test"
        reason = "both groups normal -> parametric"
    else:
        chosen = "mann-whitney"
        reason = "at least one group not normal -> non-parametric"

    print(f"\nChosen test: {chosen}  ({reason})")

    sig: bool | None = None
    test_line = ""
    if chosen == "t-test":
        tt = welch_ttest(a, b)
        if tt is None:
            print("  Independent t-test (Welch): need n>=2 in both groups (or scipy missing)")
        else:
            t, p = tt
            sig = p < 0.05
            test_line = f"t={t:.4f}, p={fmt_p(p)}"
            print(f"  Independent t-test (Welch): {test_line}  -> {'significant' if sig else 'not significant'}")
    else:
        mw = mann_whitney(a, b)
        if mw is None:
            print("  Mann-Whitney U: need n>=2 in both groups (or scipy missing)")
        else:
            u, p = mw
            sig = p < 0.05
            test_line = f"U={u:.1f}, p={fmt_p(p)}"
            print(f"  Mann-Whitney U: {test_line}  -> {'significant' if sig else 'not significant'}")

    d = cohens_d(a, b)
    if d is None:
        print("\nCohen's d: need n>=2 in both groups")
    else:
        print(f"\nCohen's d = {d:.4f}  ({d_magnitude(d)} effect)")

    # Plain-language interpretation for the thesis writeup.
    print("\nInterpretation:")
    if not a or not b or sig is None or d is None:
        print("  Not enough data in both groups to interpret.")
    else:
        faster, slower = (g1, g2) if mean(a) < mean(b) else (g2, g1)
        diff = abs(mean(a) - mean(b))
        if sig:
            print(f"  {faster} is significantly faster than {slower} on {metric} "
                  f"by {diff:.1f}s on average ({test_line}, {d_magnitude(d)} effect, d={d:.2f}).")
        else:
            print(f"  No significant difference between {g1} and {g2} on {metric} "
                  f"({test_line}); observed gap {diff:.1f}s, {d_magnitude(d)} effect (d={d:.2f}).")
    return 0


def _terraform_outputs(stack_dir: Path) -> dict:
    try:
        r = subprocess.run(
            ["terraform", "-chdir=" + str(stack_dir), "output", "-json"],
            capture_output=True, text=True, check=True,
        )
        return {k: v.get("value") for k, v in json.loads(r.stdout).items()}
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return {}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the terraform process and its children (apply spawns child processes)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # taskkill /T kills the whole tree; plain proc.kill() would orphan children.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass


def cmd_run(args) -> int:
    if args.stack not in STACK_DIRS:
        print(f"Unknown stack: {args.stack}", file=sys.stderr)
        return 2
    stack_dir = STACK_DIRS[args.stack]
    if not stack_dir.exists():
        print(f"Stack directory not found: {stack_dir}", file=sys.stderr)
        return 2

    chdir = "-chdir=" + str(stack_dir)
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not args.skip_init:
        print(f"==> terraform {chdir} init")
        init = subprocess.run(["terraform", chdir, "init", "-input=false"])
        if init.returncode != 0:
            print("terraform init failed", file=sys.stderr)
            return init.returncode

    LOGS_DIR.mkdir(exist_ok=True)
    rows_so_far = read_rows()
    pending_id = max((int(r["run_id"]) for r in rows_so_far), default=0) + 1
    log_file = LOGS_DIR / f"run-{pending_id:03d}-{args.stack.lower()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    cap_note = f"   (wall-clock cap: {args.timeout:g} min)" if args.timeout else ""
    print(f"==> terraform {chdir} apply -auto-approve   (log: {log_file.relative_to(ROOT)}){cap_note}")
    t0 = time.monotonic()
    captured: list[str] = []
    timed_out = threading.Event()

    # On POSIX, start a new session so we can kill the whole process group.
    popen_kwargs: dict = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    with log_file.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            ["terraform", chdir, "apply", "-auto-approve", "-input=false"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            **popen_kwargs,
        )
        assert proc.stdout is not None

        watchdog: threading.Timer | None = None
        if args.timeout:
            def _on_timeout() -> None:
                timed_out.set()
                msg = f"\n!! Wall-clock cap of {args.timeout:g} min reached — killing terraform apply.\n"
                sys.stdout.write(msg)
                lf.write(msg)
                _kill_tree(proc)
            watchdog = threading.Timer(args.timeout * 60.0, _on_timeout)
            watchdog.daemon = True
            watchdog.start()

        # Killing the process closes the pipe, which ends this loop.
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
            captured.append(line)
        rc = proc.wait()
        if watchdog is not None:
            watchdog.cancel()

    total_s = time.monotonic() - t0
    status = "Success" if (rc == 0 and not timed_out.is_set()) else "Fail"
    log_blob = "".join(captured)
    if status == "Success":
        error = None
    elif timed_out.is_set():
        error = f"Script timeout (>{args.timeout:g} min) :: {_tail(log_blob)}"
    elif args.error:
        error = args.error
    else:
        error = f"{classify_error(log_blob)} :: {_tail(log_blob)}"

    tf_apply_s: float | None = None
    config_s: float | None = None
    if status == "Success":
        outs = _terraform_outputs(stack_dir)
        start_ts = outs.get("config_start_ts")
        end_ts = outs.get("config_end_ts")
        try:
            if start_ts is not None and end_ts is not None:
                config_s = float(end_ts) - float(start_ts)
                tf_apply_s = total_s - config_s
        except (TypeError, ValueError):
            pass

    rows = read_rows()
    next_id = max((int(r["run_id"]) for r in rows), default=0) + 1
    row = {
        "run_id": str(next_id),
        "date": date_str,
        "stack": args.stack,
        "tf_apply_s": "" if tf_apply_s is None else f"{tf_apply_s:.1f}",
        "config_s": "" if config_s is None else f"{config_s:.1f}",
        "total_s": f"{total_s:.1f}",
        "status": status,
        "error_category": error or "None",
        "log_path": str(log_file.relative_to(ROOT)).replace("\\", "/"),
    }
    rows.append(row)
    write_rows(rows)

    print()
    print(render_table([row]))
    if status == "Fail":
        print(f"\nFull log: {log_file}")
    if status == "Success" and tf_apply_s is None:
        print("\nNote: tf_apply_s / config_s blank — add Terraform outputs "
              "`config_start_ts` and `config_end_ts` (Unix epoch) to split timing.")
    return 0 if status == "Success" else 1


def _wsl_path(p: Path) -> str:
    """Convert a Windows path to its WSL (/mnt/c/...) form via `wsl wslpath`."""
    r = subprocess.run(["wsl", "wslpath", "-a", str(p)], capture_output=True, text=True)
    return r.stdout.strip() or str(p)


_AWS_ENV_NAMES = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION", "AWS_REGION",
)


def _aws_creds_env() -> tuple[dict, str]:
    """Resolve AWS credentials from the current (PowerShell) session so they can be
    forwarded into WSL — no manual copying. Prefers the session's env vars (what
    Terraform on Windows uses), else falls back to the Windows ~/.aws/credentials
    default profile. Returns (env_dict, human-readable source)."""
    env = {n: os.environ[n] for n in _AWS_ENV_NAMES if os.environ.get(n)}
    source = "PowerShell environment"
    if "AWS_ACCESS_KEY_ID" not in env:
        cred_file = Path.home() / ".aws" / "credentials"
        if cred_file.exists():
            cp = configparser.ConfigParser()
            try:
                cp.read(cred_file)
                prof = os.environ.get("AWS_PROFILE", "default")
                if cp.has_section(prof):
                    for key, name in (("aws_access_key_id", "AWS_ACCESS_KEY_ID"),
                                      ("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY"),
                                      ("aws_session_token", "AWS_SESSION_TOKEN")):
                        if cp.has_option(prof, key):
                            env[name] = cp.get(prof, key)
                    source = f"Windows ~/.aws/credentials [{prof}]"
            except (configparser.Error, OSError):
                pass
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return env, (source if "AWS_ACCESS_KEY_ID" in env else "none found")


def _wsl_aws_env() -> tuple[dict, bool, str]:
    """Build a subprocess env that forwards the session's AWS creds into WSL via WSLENV."""
    aws_env, cred_src = _aws_creds_env()
    run_env = os.environ.copy()
    run_env.update(aws_env)
    fwd = ":".join(aws_env.keys())
    prior = run_env.get("WSLENV", "")
    run_env["WSLENV"] = f"{prior}:{fwd}".strip(":") if prior else fwd
    return run_env, ("AWS_ACCESS_KEY_ID" in aws_env), cred_src


def cmd_cfn_run(args) -> int:
    """Deploy the CloudFormation stack via WSL (cloudformation/deploy.sh), time it, and
    append a row to runs.csv with stack='CloudFormation'. Mirrors cmd_run: total_s by
    wall-clock; config_s / tf_apply_s split from the builder's config_start/end markers,
    which deploy.sh writes to cloudformation/.last_run.json."""
    if not CFN_DIR.exists():
        print(f"CloudFormation directory not found: {CFN_DIR}", file=sys.stderr)
        return 2
    deploy_sh = CFN_DIR / "deploy.sh"
    if not deploy_sh.exists():
        print(f"Missing {deploy_sh}", file=sys.stderr)
        return 2

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    LOGS_DIR.mkdir(exist_ok=True)
    rows_so_far = read_rows()
    pending_id = max((int(r["run_id"]) for r in rows_so_far), default=0) + 1
    log_file = LOGS_DIR / f"run-{pending_id:03d}-cloudformation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    # Run deploy.sh inside WSL from the cloudformation/ dir (aws cli lives in WSL).
    wsl_dir = _wsl_path(CFN_DIR)
    inner = f"cd {shlex.quote(wsl_dir)} && bash deploy.sh"
    cmd = ["wsl", "bash", "-lc", inner]

    # Stale marker would otherwise be misread if the deploy dies before writing a fresh one.
    last_run = CFN_DIR / ".last_run.json"
    try:
        last_run.unlink()
    except FileNotFoundError:
        pass

    # Forward the current session's AWS credentials into WSL via WSLENV, so the user
    # doesn't have to copy creds into ~/.aws inside WSL separately.
    run_env, has_creds, cred_src = _wsl_aws_env()
    if has_creds:
        print(f"==> forwarding AWS credentials to WSL (source: {cred_src})")
    else:
        print("!! No AWS credentials found in the PowerShell session or Windows ~/.aws/credentials.\n"
              "   The deploy will fail with NoCredentials. Load Learner Lab creds first "
              "(set AWS_* env vars or fill %USERPROFILE%\\.aws\\credentials).")

    cap_note = f"   (wall-clock cap: {args.timeout:g} min)" if args.timeout else ""
    print(f"==> wsl bash deploy.sh   (log: {log_file.relative_to(ROOT)}){cap_note}")
    t0 = time.monotonic()
    captured: list[str] = []
    timed_out = threading.Event()

    with log_file.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            env=run_env,
        )
        assert proc.stdout is not None

        watchdog: threading.Timer | None = None
        if args.timeout:
            def _on_timeout() -> None:
                timed_out.set()
                msg = f"\n!! Wall-clock cap of {args.timeout:g} min reached — killing deploy.\n"
                sys.stdout.write(msg)
                lf.write(msg)
                _kill_tree(proc)
            watchdog = threading.Timer(args.timeout * 60.0, _on_timeout)
            watchdog.daemon = True
            watchdog.start()

        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
            captured.append(line)
        rc = proc.wait()
        if watchdog is not None:
            watchdog.cancel()

    total_s = time.monotonic() - t0
    status = "Success" if (rc == 0 and not timed_out.is_set()) else "Fail"
    log_blob = "".join(captured)
    if status == "Success":
        error = None
    elif timed_out.is_set():
        error = f"Script timeout (>{args.timeout:g} min) :: {_tail(log_blob)}"
    elif args.error:
        error = args.error
    else:
        error = f"{classify_error(log_blob)} :: {_tail(log_blob)}"

    tf_apply_s: float | None = None
    config_s: float | None = None
    if status == "Success" and last_run.exists():
        try:
            meta = json.loads(last_run.read_text(encoding="utf-8"))
            start_ts, end_ts = meta.get("config_start_ts"), meta.get("config_end_ts")
            if start_ts not in (None, "", "None") and end_ts not in (None, "", "None"):
                config_s = float(end_ts) - float(start_ts)
                tf_apply_s = total_s - config_s
        except (json.JSONDecodeError, TypeError, ValueError, OSError):
            pass

    rows = read_rows()
    next_id = max((int(r["run_id"]) for r in rows), default=0) + 1
    row = {
        "run_id": str(next_id),
        "date": date_str,
        "stack": "CloudFormation",
        "tf_apply_s": "" if tf_apply_s is None else f"{tf_apply_s:.1f}",
        "config_s": "" if config_s is None else f"{config_s:.1f}",
        "total_s": f"{total_s:.1f}",
        "status": status,
        "error_category": error or "None",
        "log_path": str(log_file.relative_to(ROOT)).replace("\\", "/"),
    }
    rows.append(row)
    write_rows(rows)

    print()
    print(render_table([row]))
    if status == "Fail":
        print(f"\nFull log: {log_file}")
    if status == "Success" and tf_apply_s is None:
        print("\nNote: tf_apply_s / config_s blank — deploy.sh did not write config "
              "markers to cloudformation/.last_run.json (check builder tagging).")
    return 0 if status == "Success" else 1


def cmd_cfn_events(args) -> int:
    """Print the CREATE_FAILED / failure events for a CloudFormation stack, forwarding the
    session's AWS creds into WSL. Use this to diagnose a failed `cfn-run`."""
    run_env, has_creds, cred_src = _wsl_aws_env()
    if not has_creds:
        print("!! No AWS credentials found in the PowerShell session or Windows ~/.aws/credentials.",
              file=sys.stderr)
        return 2
    print(f"==> forwarding AWS credentials to WSL (source: {cred_src})")
    query = ("StackEvents[?contains(ResourceStatus,'FAILED')]"
             ".[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]")
    inner = (f"aws cloudformation describe-stack-events --stack-name {shlex.quote(args.stack_name)} "
             f"--region {shlex.quote(args.region)} --query {shlex.quote(query)} --output text")
    print(f"==> {inner}\n")
    # Reverse so the EARLIEST failure (usually the root cause) prints last / is easy to spot.
    proc = subprocess.run(["wsl", "bash", "-lc", inner], env=run_env,
                          capture_output=True, text=True)
    out = (proc.stdout or "").strip()
    if not out:
        print(proc.stderr.strip() or "(no FAILED events found — stack may have rolled back "
              "and been deleted, or the name is wrong)")
        return proc.returncode
    lines = out.splitlines()
    print("Failure events (newest first; the OLDEST below is usually the root cause):\n")
    print("\n".join(lines))
    print("\nRoot cause (oldest failure):")
    print("  " + lines[-1] if lines else "  (none)")
    return 0


def cmd_cfn_health(args) -> int:
    """Run cloudformation/health.sh via WSL (forwards creds) to diagnose unhealthy ASG
    targets: stack status, ASG instances, ALB target-health reasons, and the HTTP code."""
    health_sh = CFN_DIR / "health.sh"
    if not health_sh.exists():
        print(f"Missing {health_sh}", file=sys.stderr)
        return 2
    run_env, has_creds, cred_src = _wsl_aws_env()
    if not has_creds:
        print("!! No AWS credentials found.", file=sys.stderr)
        return 2
    print(f"==> forwarding AWS credentials to WSL (source: {cred_src})")
    inner = f"cd {shlex.quote(_wsl_path(CFN_DIR))} && bash health.sh"
    return subprocess.run(["wsl", "bash", "-lc", inner], env=run_env).returncode


def cmd_cfn_destroy(args) -> int:
    """Run cloudformation/destroy.sh via WSL, forwarding the session's AWS creds."""
    destroy_sh = CFN_DIR / "destroy.sh"
    if not destroy_sh.exists():
        print(f"Missing {destroy_sh}", file=sys.stderr)
        return 2
    run_env, has_creds, cred_src = _wsl_aws_env()
    if not has_creds:
        print("!! No AWS credentials found in the PowerShell session or Windows ~/.aws/credentials.",
              file=sys.stderr)
        return 2
    print(f"==> forwarding AWS credentials to WSL (source: {cred_src})")
    inner = f"cd {shlex.quote(_wsl_path(CFN_DIR))} && bash destroy.sh"
    return subprocess.run(["wsl", "bash", "-lc", inner], env=run_env).returncode


def _parse_ansible_changed(text: str) -> int | None:
    """Extract the last `changed=N` from an Ansible PLAY RECAP."""
    matches = re.findall(r"changed=(\d+)", text)
    return int(matches[-1]) if matches else None


def _parse_chef_updated(text: str) -> int | None:
    """Extract X from Chef's 'X/Y resources updated' summary.

    Matches both wordings: 'Chef Infra Client finished, X/Y resources updated'
    (older) and 'Infra Phase complete, X/Y resources updated' (Chef 18+).
    """
    matches = re.findall(r"(\d+)/(\d+)\s+resources updated", text)
    return int(matches[-1][0]) if matches else None


def _run_streaming(cmd, cwd=None, shell=False, env=None) -> tuple[int, str]:
    captured: list[str] = []
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        captured.append(line)
    proc.wait()
    return proc.returncode, "".join(captured)


def _append_idem(row: dict) -> None:
    new = not IDEMPOTENCY_CSV.exists()
    with IDEMPOTENCY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=IDEM_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def cmd_idempotency(args) -> int:
    stack = args.stack
    if stack not in STACK_DIRS:
        print(f"Unknown stack: {stack}", file=sys.stderr)
        return 2

    if getattr(args, "config_only", False):
        stack_dir = STACK_DIRS[stack]
        # -target only builds the resource + its dependencies. The builder doesn't
        # depend on the IGW/route-table association, so we must target it explicitly
        # or the public subnet has no internet route and SSH times out.
        targets = [
            f"null_resource.{stack.lower()}_provision",
            "aws_route_table_association.public",
        ]
        target_flags = [f"-target={t}" for t in targets]
        print(f"--config-only: terraform -chdir={stack_dir} apply {' '.join(target_flags)}")
        print("(Builds the configured builder + public routing without the AMI bake, "
              "so it never reboots and /tmp stays intact.)\n")
        rc = subprocess.run(
            ["terraform", f"-chdir={stack_dir}", "apply", *target_flags,
             "-auto-approve", "-input=false"]
        ).returncode
        if rc != 0:
            print("config-only apply failed; aborting idempotency test.", file=sys.stderr)
            return rc
        print()

    # Build the command that re-runs the configuration on the EXISTING builder.
    # Requires a prior `terraform apply` (infra + configured builder still up).
    if stack == "Ansible":
        ansible_dir = STACK_DIRS["Ansible"] / "ansible"
        if not (ansible_dir / "inventory.ini").exists():
            print("No inventory.ini found — run a terraform apply first so the "
                  "builder exists and is in inventory.", file=sys.stderr)
            return 2
        run_cmd = ("wsl ansible-playbook -i inventory.ini -u ubuntu "
                   "--extra-vars @extra_vars.json playbook.yml")
        cwd: str | None = str(ansible_dir)
        parser = _parse_ansible_changed
        unit = "changed tasks"
    else:  # Chef
        outs = _terraform_outputs(STACK_DIRS["Chef"])
        ip = outs.get("builder_public_ip")
        if not ip:
            print("No builder_public_ip output — run a terraform apply first.", file=sys.stderr)
            return 2
        inner = ("sudo chef-client --chef-license accept --local-mode "
                 "--config /tmp/chef/solo.rb --json-attributes /tmp/chef/attributes.json "
                 "--runlist 'recipe[wordpress::default]'")
        run_cmd = (f'wsl ssh -i {args.key} -o StrictHostKeyChecking=no '
                   f'ubuntu@{ip} "{inner}"')
        cwd = None
        parser = _parse_chef_updated
        unit = "resources updated"

    print(f"Idempotency test: {stack}, {args.repeats} re-run(s) on the existing builder.")
    print(f"(Each iteration re-converges the SAME host; nothing is destroyed.)\n")

    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    results: list[int | None] = []
    for i in range(1, args.repeats + 1):
        print(f"=== {stack} idempotency iteration {i}/{args.repeats} ===")
        rc, out = _run_streaming(run_cmd, cwd=cwd, shell=True)
        log_file = LOGS_DIR / f"idem-{stack.lower()}-{ts}-iter{i}.log"
        log_file.write_text(out, encoding="utf-8")
        changed = parser(out)
        if rc != 0:
            status = "error"
        elif changed is None:
            status = "unparsed"
        elif changed == 0:
            status = "idempotent"
        else:
            status = "non-idempotent"
        _append_idem({
            "stack": stack,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iteration": str(i),
            "changed_count": "" if changed is None else str(changed),
            "status": status,
            "summary": f"rc={rc}; {unit}={changed}",
        })
        results.append(changed)
        print(f"\n-> iteration {i}: {unit} = {changed}  [{status}]  (log: {log_file.name})\n")

    clean = sum(1 for c in results if c == 0)
    print(f"{stack}: {clean}/{args.repeats} iterations fully idempotent (0 {unit}).")
    nonzero = [c for c in results if c not in (0, None)]
    if nonzero:
        print(f"  Note: {len(nonzero)} iteration(s) reported {unit} > 0. For Chef this is "
              f"expected if the chmod/chown `execute` resources are unguarded (they re-run "
              f"every converge); for Ansible the chmod tasks use changed_when:false. This "
              f"asymmetry reflects config authoring, not the tool — symmetrize the guards "
              f"before treating the counts as a tool comparison.")
    print(f"\nResults appended to {IDEMPOTENCY_CSV.name}")
    return 0


def _parse_cfn_idem(text: str) -> tuple[int | None, dict]:
    """Read the KEY=VALUE markers cloudformation/idempotency.sh prints last.

    Returns (total_changes, detail_dict). total is None if unparsed or the script
    reported ERR for any sub-check.
    """
    keys = ("CHANGESET_S1", "CHANGESET_S2", "DRIFT_S1", "DRIFT_S2", "TOTAL_CHANGES")
    detail = {}
    for k in keys:
        m = re.findall(rf"^{k}=(\S+)\s*$", text, re.MULTILINE)
        if m:
            detail[k] = m[-1]
    total = detail.get("TOTAL_CHANGES")
    if total is None or not total.isdigit():
        return None, detail
    return int(total), detail


def cmd_cfn_idempotency(args) -> int:
    """Idempotency test for the CloudFormation stack, recorded in the same CSV.

    CloudFormation's configuration layer (builder user-data) runs once at launch
    and has no re-converge command, so the analogue of re-running the playbook /
    chef-client is re-submitting the IDENTICAL templates and asking the service
    what it would change. cloudformation/idempotency.sh does that per iteration:
      * a change set per stack (created, counted, deleted - never executed), and
      * drift detection per stack.
    changed_count = change-set changes + drifted resources, so 0 means fully
    idempotent, matching the Ansible/Chef convention already in idempotency.csv.
    Nothing is created, modified, or destroyed by this test.
    """
    script = CFN_DIR / "idempotency.sh"
    if not script.exists():
        print(f"Missing {script}", file=sys.stderr)
        return 2
    # .last_run.json is NOT required: destroy.sh removes it, and idempotency.sh can
    # read the golden AMI id back from the live stack's own GoldenAmiId parameter.
    # What is required is that both stacks are still up — the script checks that and
    # exits 2 with the stack status if they are not.
    run_env, has_creds, cred_src = _wsl_aws_env()
    if not has_creds:
        print("!! No AWS credentials found in the PowerShell session or Windows "
              "~/.aws/credentials.", file=sys.stderr)
        return 2
    print(f"==> forwarding AWS credentials to WSL (source: {cred_src})")

    stack = "CloudFormation"
    unit = "change-set + drift changes"
    print(f"Idempotency test: {stack}, {args.repeats} re-run(s) against the live stacks.")
    print("(Each iteration re-submits the SAME templates as a change set and runs "
          "drift detection; change sets are never executed and are deleted.)\n")

    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    wsl_dir = shlex.quote(_wsl_path(CFN_DIR))
    prefix = "SKIP_DRIFT=1 " if args.skip_drift else ""
    results: list[int | None] = []

    for i in range(1, args.repeats + 1):
        print(f"=== {stack} idempotency iteration {i}/{args.repeats} ===")
        inner = f"cd {wsl_dir} && {prefix}bash idempotency.sh {i}"
        rc, out = _run_streaming(["wsl", "bash", "-lc", inner], env=run_env)
        log_file = LOGS_DIR / f"idem-cloudformation-{ts}-iter{i}.log"
        log_file.write_text(out, encoding="utf-8")
        changed, detail = _parse_cfn_idem(out)

        if changed is None:
            status = "error" if rc != 0 else "unparsed"
        elif rc != 0:
            status = "error"
        elif changed == 0:
            status = "idempotent"
        else:
            status = "non-idempotent"

        parts = ", ".join(f"{k.lower()}={v}" for k, v in detail.items()
                          if k != "TOTAL_CHANGES") or "no markers parsed"
        _append_idem({
            "stack": stack,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "iteration": str(i),
            "changed_count": "" if changed is None else str(changed),
            "status": status,
            "summary": f"rc={rc}; {parts}",
        })
        results.append(changed)
        print(f"\n-> iteration {i}: {unit} = {changed}  [{status}]  (log: {log_file.name})\n")

    clean = sum(1 for c in results if c == 0)
    print(f"{stack}: {clean}/{args.repeats} iterations fully idempotent (0 {unit}).")
    if any(c not in (0, None) for c in results):
        print("  Note: a non-zero count means CloudFormation would act on a re-submit of "
              "the identical template (change set) or found live resources diverging from "
              "it (drift). Check the iteration log for the resource list.")
    print("  Scope note: this measures the DECLARATIVE layer only. The in-guest "
          "configuration (packages, wp-config.php, EFS mount) is baked by builder "
          "user-data, which CloudFormation runs once and cannot re-converge — unlike "
          "the Ansible/Chef iterations, which re-run the full config on the same host.")
    print(f"\nResults appended to {IDEMPOTENCY_CSV.name}")
    return 0


# Metrics shown by `table`, in the order they appear in the article's Tabel 3.
TABLE_METRICS = [("total_s", "Total"), ("config_s", "Konfigurasi"), ("tf_apply_s", "Infrastruktur")]
SHORT_NAME = {"CloudFormation": "CFN", "Ansible": "Ansible", "Chef": "Chef"}


def _id_num(x: float, nd: int = 2, signed: bool = False) -> str:
    """Indonesian number format: comma decimal, optional explicit + sign."""
    s = f"{x:+.{nd}f}" if signed else f"{x:.{nd}f}"
    return s.replace(".", ",")


def _id_p(p: float) -> str:
    """Match the article's p formatting: 3 decimals from 0.01 up, 4 below."""
    if p < 0.0001:
        return "<0,0001"
    return _id_num(p, 3 if p >= 0.01 else 4)


def cmd_table(args) -> int:
    """Print the article's comparative table (Tabel 3) straight from runs.csv.

    Reuses the same helpers as `stats`, so the numbers are identical to the ones
    in the article: test chosen by Shapiro-Wilk on both groups, Welch t when both
    are normal and Mann-Whitney otherwise, Cohen's d on the pooled SD.
    The difference column is signed as baseline minus comparator, so a negative
    value means the baseline is faster.
    """
    rows = [r for r in read_rows() if r["status"] == "Success"]
    base = args.baseline
    others = [s for s in args.vs if s != base]
    if not others:
        print("Nothing to compare against the baseline.", file=sys.stderr)
        return 2

    def values(stack: str, metric: str) -> list[float]:
        out = []
        for r in rows:
            if r["stack"] != stack:
                continue
            v = r.get(metric, "")
            if v:
                try:
                    out.append(float(v))
                except ValueError:
                    pass
        return out

    bshort = SHORT_NAME.get(base, base)
    header = ["Perbandingan", "Metrik", f"Selisih {bshort} (detik)", "Uji", "Statistik", "p", "p Holm", "d", "r", "Hasil"]
    table: list[list[str]] = []
    holm = holm_family(rows)

    for metric, label in TABLE_METRICS:
        for other in others:
            a, b = values(base, metric), values(other, metric)
            if len(a) < 2 or len(b) < 2:
                continue
            # Same test-selection rule as `stats`: parametric only if BOTH normal.
            res = compare(a, b)
            if res is None:
                continue
            test_name, stat, p = res
            both_normal = test_name == "t Welch"
            p_holm = holm.get((base, other, metric))
            d = cohens_d(a, b)
            # Rank-biserial r from U (baseline as first group), reported as |r|.
            # It needs no normality assumption, unlike Cohen's d.
            mw = mann_whitney(a, b)
            r_rb = None if mw is None else abs(1 - 2 * mw[0] / (len(a) * len(b)))
            stat_txt = (f"t = {_id_num(stat, 2)}" if both_normal
                        else f"U = {stat:g}".replace(".", ","))
            diff = mean(a) - mean(b)          # negative => baseline faster
            if p >= 0.05:
                verdict = "Tidak signifikan"
            elif diff < 0:
                verdict = f"{bshort} lebih cepat"
            else:
                verdict = f"{SHORT_NAME.get(other, other)} lebih cepat"
            table.append([
                f"{bshort} vs {SHORT_NAME.get(other, other)}", label,
                _id_num(diff, 2, signed=True), test_name, stat_txt, _id_p(p),
                "-" if p_holm is None else _id_p(p_holm),
                "-" if d is None else _id_num(abs(d), 2),
                "-" if r_rb is None else _id_num(r_rb, 2), verdict,
            ])

    if not table:
        print("Not enough successful runs to build the table.", file=sys.stderr)
        return 2

    widths = [max(len(r[i]) for r in [header, *table]) for i in range(len(header))]
    line = "-+-".join("-" * w for w in widths)
    print(" | ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    print(line)
    for r in table:
        print(" | ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    print(line)
    print(f"Selisih = rata-rata {base} dikurangi rata-rata pembanding; "
          f"negatif berarti {bshort} lebih cepat. Cohen's d dan r rank-biserial "
          f"sebagai nilai mutlak; U dihitung dengan {bshort} sebagai kelompok pertama.")
    for metric, label in TABLE_METRICS:
        print(f"Median {label}: " + ", ".join(
            f"{SHORT_NAME.get(s, s)} {_id_num(median(values(s, metric)), 2)}" for s in [base, *others]))
    print("p Holm: koreksi Holm-Bonferroni atas sembilan uji berpasangan di artikel.")
    print(f"n = " + ", ".join(f"{SHORT_NAME.get(s, s)} {len(values(s, 'total_s'))}"
                              for s in [base, *others]) + " run sukses.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Thesis IaC benchmark run tracker.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Print the runs table").set_defaults(func=cmd_show)

    a = sub.add_parser("add", help="Append a run")
    a.add_argument("--date", required=True, help='e.g. "2026-05-11 10:30"')
    a.add_argument("--stack", required=True, choices=STACKS)
    a.add_argument("--tf-apply", type=float, dest="tf_apply", help="Terraform apply seconds")
    a.add_argument("--config", type=float, help="Config (Ansible/Chef) seconds")
    a.add_argument("--total", type=float, help="Total seconds (defaults to tf-apply + config)")
    a.add_argument("--status", required=True, choices=["Success", "Fail"])
    a.add_argument("--error", help='Error category (default "None" on Success)')
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("run", help="Run `terraform apply` for a stack, time it, append to CSV")
    r.add_argument("--stack", required=True, choices=STACKS)
    r.add_argument("--skip-init", action="store_true", help="Skip `terraform init`")
    r.add_argument("--error", help="Error category to record if apply fails")
    r.add_argument("--timeout", type=float, metavar="MIN",
                   help="Wall-clock cap in minutes; kills apply and records Fail (Script timeout) if exceeded")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("cfn-run", help="Deploy the CloudFormation stack via WSL, time it, append to CSV")
    c.add_argument("--error", help="Error category to record if deploy fails")
    c.add_argument("--timeout", type=float, metavar="MIN",
                   help="Wall-clock cap in minutes; kills deploy and records Fail (Script timeout) if exceeded")
    c.set_defaults(func=cmd_cfn_run)

    ce = sub.add_parser("cfn-events", help="Show CloudFormation stack failure events (diagnose a failed cfn-run)")
    ce.add_argument("--stack-name", default="skripsi-iac-cfn-1",
                    help="Stack to inspect (default skripsi-iac-cfn-1)")
    ce.add_argument("--region", default="us-east-1")
    ce.set_defaults(func=cmd_cfn_events)

    ch = sub.add_parser("cfn-health", help="Show ALB target health + HTTP code (diagnose unhealthy ASG)")
    ch.add_argument("--stack-name", default="skripsi-iac-cfn-2", help="Compute stack (default skripsi-iac-cfn-2)")
    ch.add_argument("--region", default="us-east-1")
    ch.set_defaults(func=cmd_cfn_health)

    sub.add_parser("cfn-destroy", help="Tear down the CloudFormation stacks via WSL (forwards creds)") \
        .set_defaults(func=cmd_cfn_destroy)

    sub.add_parser(
        "reliability",
        help="Success/fail rates per stack (Wilson CI), failure breakdown by attribution",
    ).set_defaults(func=cmd_reliability)

    idem = sub.add_parser(
        "idempotency",
        help="Re-run config N times on the existing builder (no destroy); record changed-count",
    )
    idem.add_argument("--stack", required=True, choices=STACKS)
    idem.add_argument("--repeats", type=int, default=5, help="Number of re-runs (default 5)")
    idem.add_argument("--key", default="~/.ssh/labsuser.pem",
                      help="WSL path to SSH private key (Chef only; default ~/.ssh/labsuser.pem)")
    idem.add_argument("--config-only", action="store_true", dest="config_only",
                      help="First run `terraform apply -target=...provision` to build a "
                           "config-only builder (no AMI bake/reboot, /tmp intact). Run on a "
                           "DESTROYED stack — it won't un-reboot an already-baked builder.")
    idem.set_defaults(func=cmd_idempotency)

    ci = sub.add_parser(
        "cfn-idempotency",
        help="Re-submit the identical CloudFormation templates N times (change set + drift, "
             "never executed); record changed-count",
    )
    ci.add_argument("--repeats", type=int, default=5, help="Number of re-runs (default 5)")
    ci.add_argument("--skip-drift", action="store_true", dest="skip_drift",
                    help="Change-set check only; skip drift detection (much faster)")
    ci.set_defaults(func=cmd_cfn_idempotency)

    tb = sub.add_parser(
        "table",
        help="Print the article's comparative table (Tabel 3) computed from runs.csv",
    )
    tb.add_argument("--baseline", default="CloudFormation", choices=STATS_STACKS,
                    help="Stack the difference is measured FROM (default CloudFormation)")
    tb.add_argument("--vs", nargs="+", default=["Ansible", "Chef"], choices=STATS_STACKS,
                    help="Stacks to compare the baseline against (default: Ansible Chef)")
    tb.set_defaults(func=cmd_table)

    s = sub.add_parser("stats", help="Compute Shapiro-Wilk, Welch t-test, Cohen's d")
    s.add_argument("--metric", default="total_s", choices=["tf_apply_s", "config_s", "total_s"])
    s.add_argument("--compare", nargs=2, metavar=("STACK_A", "STACK_B"),
                   default=["Ansible", "Chef"], choices=STATS_STACKS,
                   help="Two stacks to test (default: Ansible Chef). e.g. --compare CloudFormation Chef")
    s.set_defaults(func=cmd_stats)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.func(args))
