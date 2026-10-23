# Measurement Limitations & Methodological Notes

This document records known measurement caveats for the Terraform+Ansible vs.
Terraform+Chef benchmark, so they are version-controlled alongside `runs.csv`
and the per-run logs in `logs/`. Cite these in the thesis "Limitations" /
"Threats to Validity" section.

## 1. Phase-boundary placement (`config_s` vs `tf_apply_s`)

**Summary:** `total_s` is fully comparable across stacks. The split between
infrastructure time (`tf_apply_s`) and configuration time (`config_s`) carries a
small, *mirror-image* bias caused by where each stack stamps its
`config_start` timestamp marker.

**Mechanism:**

- The `run` command measures `total_s` as the wall-clock duration of a single
  `terraform apply` — measured identically for both stacks.
- `config_s = config_end_ts - config_start_ts`; `tf_apply_s = total_s - config_s`.
- **Ansible stack:** `time_static.config_start` depends on
  `null_resource.wait_for_ssh`, so the configuration timer starts *after* SSH
  readiness is confirmed. The EC2 boot / SSH-availability interval is therefore
  counted in `tf_apply_s`, not `config_s`.
- **Chef stack:** `time_static.config_start` depends only on
  `aws_instance.builder`, so it stamps at instance creation, *before* SSH is
  reachable. The EC2 boot / SSH-availability interval (inside `null_resource.upload_chef`)
  is therefore counted in `config_s`.

**Effect:**

| Metric       | Boot-wait counted in | Bias direction              |
|--------------|----------------------|-----------------------------|
| `config_s`   | Chef                 | conservatively against Chef |
| `tf_apply_s` | Ansible              | conservatively against Ansible |
| `total_s`    | both (within total)  | **none — fully comparable** |

The boot-wait is environmental (EC2 boot → SSH reachable), typically 20–40s but
up to 172s in one observed run. The two sub-metric biases are mirror images and
cancel in `total_s`.

**Mitigation / reporting decision:** Measurement left unchanged to keep all
collected samples consistent. `total_s` is reported as the primary metric;
`config_s` and `tf_apply_s` are reported as secondary breakdowns with this
caveat. Crucially, the bias on `config_s` works *against* the observed
conclusion (Chef has shorter configuration time), so the conclusion is robust
to it.

## 2. Run 4 (Chef) `config_s` outlier — 309s

Run 4 recorded `config_s = 309s` against a Chef norm of ~145s. Log analysis
(`logs/run-004-chef-20260605-120626.log`) shows:

- `null_resource.upload_chef`: 2m52s (172s), almost entirely SSH-connection
  retries — the builder EC2 was not SSH-reachable until ~2m40s.
- `null_resource.chef_provision`: 2m17s, of which the actual Chef Infra Client
  recipe convergence was 119s — **normal**.

The outlier is therefore attributable to a prolonged EC2 boot / SSH-availability
delay (environmental), **not** to Chef configuration work. It is retained in the
dataset (not deleted) because removing inconvenient points is cherry-picking;
instead, non-parametric testing handles it (see §3).

## 3. Normality and test selection

Because the run-4 outlier makes Chef's `config_s` distribution non-normal
(Shapiro–Wilk p < 0.0001), the analysis script (`scripts/runs.py stats`)
automatically selects the **Mann–Whitney U** test instead of the independent
t-test for any metric where either group fails the Shapiro–Wilk normality test
(α = 0.05). Cohen's d is reported as the effect size regardless. This selection
is automatic and data-driven, not chosen post hoc.

## 4. Legitimate tool differences (NOT artifacts)

These are real differences in how the two stacks operate and are part of the
finding, not measurement error:

- Chef's `config_s` includes **cookbook upload (scp)** and a **one-time
  `chef-client` install** (downloaded from omnitruck on each fresh builder).
  Ansible has no equivalent: it is pre-installed in WSL and pushes per-task over
  SSH. These belong in the discussion as genuine stack characteristics.

## 4a. Discarded trials (experimenter-side interruptions)

A trial aborted by the experimenter's own environment — not by either stack
under test — is treated as an **invalid run**, removed from `runs.csv`, and
re-run to restore the intended sample size. The original log is retained for
audit. Run IDs are **not** renumbered after a removal (gaps are expected and
keep IDs aligned with the `logs/run-0NN-*.log` filenames).

Discarded so far:

| Original run_id | Stack   | Date             | Reason                                          | Log retained                              |
|-----------------|---------|------------------|-------------------------------------------------|-------------------------------------------|
| 15              | Ansible | 2026-06-06 23:42 | Confirmed WSL/control-node shutdown (host slept mid-run); `ansible-playbook` killed, exit 1, no `PLAY RECAP`/`fatal:`. Unrelated to the Ansible stack's logic. | `logs/run-015-ansible-20260606-234234.log` |
| 33              | CloudFormation | 2026-08-26 21:19 | Not a sampling trial: a deployment run purely to bring the stacks up so the CloudFormation idempotency test (`cfn-idempotency`) had a live stack to inspect. Excluded to keep the CloudFormation timing sample at the intended n=10 (runs 23-32). The run itself succeeded. | `logs/run-033-cloudformation-20260826-211904.log` |

Note: because the Ansible stack runs its configuration from a WSL control node
(push over SSH), it is exposed to a control-node-termination failure surface
that the Chef stack (which runs `chef-client` locally on the builder) does not
share. The run-15 interruption was operator-induced (host sleep) rather than a
spontaneous WSL fault, so it is discarded rather than counted; the architectural
difference itself is noted in the discussion, not the reliability figures.

## 5. Failed runs excluded from timing

Runs with `status = Fail` are excluded from all timing statistics (a run that
aborts mid-apply is not a valid provisioning-time sample). They are retained in
`runs.csv` and analyzed separately for reliability (`scripts/runs.py reliability`),
where failures are bucketed as environmental, stack-attributable, or
indeterminate so that environmental noise (e.g. Learner Lab STS expiry, AWS
capacity) is not wrongly counted against either CM tool.

## 6. Error-rate metric — raw vs. stack-attributable

Reliability is reported two ways (`scripts/runs.py reliability`):

- **Raw success rate:** Ansible 90.9% (10/11), Chef 100% (10/10).
- **Stack-attributable success rate:** both 100% (95% Wilson CI [72.2, 100]).

The single Ansible failure (run 19) was a `Script timeout (>55 min)` caused by an
**EFS-latency stall** on the recursive `chmod` over the EFS-mounted `wp-content`
— an environmental condition, bucketed `indeterminate`, that could equally have
affected Chef (the recipe shares the identical task). It is therefore counted in
the raw rate but excluded from the stack-attributable rate. Both figures are
reported: the raw rate reflects what occurred during collection; the
stack-attributable rate is the fair tool comparison. The raw difference reflects
which stack the environmental hiccup happened to strike, not a tool weakness.

## 7. Idempotency testing (Scenarios C & D)

### Method
Idempotency = re-running the configuration tool on an already-configured host
produces no changes. Each stack was re-converged **5×** on the same builder
without destroying it, recording the changed-count per pass
(`scripts/runs.py idempotency --repeats 5`, results in `idempotency.csv`).

### Builder must NOT be the post-AMI-bake instance
`aws_ami_from_instance` reboots the builder to snapshot it, and Ubuntu wipes
`/tmp` on reboot — destroying the build scratch (`/tmp/chef` cookbook,
`/tmp/wordpress.tar.gz`, `/tmp/wp-salts.txt`). Re-running against a rebooted
builder therefore measures *reboot recovery*, not config idempotency (and Chef
cannot even start without its cookbook). The idempotency scenario instead builds
a **config-only builder** that never reboots, via
`terraform apply -target=null_resource.<stack>_provision` plus
`-target=aws_route_table_association.public` (the latter is required because the
builder does not depend on the IGW/route association, so `-target` would
otherwise leave the public subnet unroutable and SSH would time out). The
`--config-only` flag of `scripts/runs.py idempotency` automates this.

### Idempotency guards added (and why timing remains valid)
The original configs were not fully idempotent on re-convergence. The following
guards were added **symmetrically** to both stacks. All affect only *repeat*
convergence; first-run execution and resulting state are unchanged, so the
timing replications (Scenarios A/B) remain valid across the modification.

| Issue | Fix (both stacks) |
|-------|-------------------|
| `chmod`/`chown` permission steps re-ran every converge | Skip via a `/etc/wp-perms-done` sentinel (Ansible `when:` guard; Chef `not_if`) |
| Chef `ruby_block` for salts reported "updated" every run | Removed; template reads the salts file directly via `lazy` |
| Chef `nginx -t` `execute` ran every converge | `action :nothing` + `subscribes` to the nginx config resources |
| Blanket `chmod 644` clobbered `wp-config.php`'s stricter `0640` | Exclude `wp-config.php` from the `find … chmod 644` |
| Ansible salt fetch re-downloaded NEW random salts every run (`get_url force:no` unreliable) | Explicit `stat` + `when` cache guard (mirrors Chef's `creates`) |

### Result
Both stacks: **5/5 iterations idempotent (0 changes)**.

### Observation (qualitative finding, not a metric)
Reaching idempotency required **one** guard in Ansible's permission path vs.
**three** distinct fixes in Chef (unguarded `execute`s, `ruby_block`, inline
`nginx -t`). Chef's `execute`/`ruby_block` resources are not idempotent by
default and require explicit guards, whereas Ansible's stateful modules
(`file`, `template`, `mount`) and `changed_when` are idempotent more naturally.
This is a legitimate discussion point on ease-of-writing idempotent config.

### CloudFormation idempotency (different mechanism, reported separately)
CloudFormation has **no re-converge command** at the configuration layer. Its
in-guest configuration is the builder's user-data script, which the EC2 service
runs exactly once at first boot; CloudFormation cannot re-execute it without
replacing the instance. Re-running `deploy.sh` is therefore *not* the analogue
of re-running `ansible-playbook` / `chef-client` — it would bake a new AMI and
create a new stack.

The equivalent question CloudFormation *can* answer is declarative: given the
identical templates and parameters, does the service consider the live stack
already in the desired state? `scripts/runs.py cfn-idempotency --repeats 5`
(driving `cloudformation/idempotency.sh`) measures this per iteration with two
checks over **both** stacks:

| Check | Command | Meaning of 0 |
|-------|---------|--------------|
| Change set | `create-change-set --change-set-type UPDATE` with the same template/params | Re-submitting the identical declaration is a no-op ("submitted information didn't contain changes") |
| Drift | `detect-stack-drift` | No live resource has diverged from its template declaration |

`changed_count` recorded in `idempotency.csv` = change-set changes + drifted
resources, so **0 = fully idempotent**, matching the Ansible/Chef convention in
the same file. Change sets are created, counted, and then **deleted without ever
being executed**, and drift detection is read-only, so the test modifies nothing.

**Scope caveat — do not report this as the same measurement.** The Ansible/Chef
iterations re-execute the *complete configuration* (packages, EFS mount,
WordPress files, `wp-config.php`, services) on a live host and count what the
tool changed. The CloudFormation iterations only exercise the *declarative
infrastructure* layer. A CloudFormation result of 0 means "the declared AWS
resources still match the template", not "re-applying the WordPress
configuration produces no changes". The two numbers answer different questions
and must be presented as a qualitative architectural difference, not as a
head-to-head idempotency metric.

## 8. Environment

AWS Academy Learner Lab: STS credentials rotate ~every 4 hours; no NAT Gateways;
`LabRole` instance profile only; AWS-managed encryption keys only. See
`README.md` for full environment constraints.

## 9. CloudFormation (third stack)

A third IaC stack was added after the two Terraform stacks:
**CloudFormation** as the provisioning layer, deploying the same Multi-AZ WordPress
architecture (`cloudformation/`). 10 successful runs were collected with the same
`scripts/runs.py` timing tool (`cfn-run`), recorded in `runs.csv` as `stack=CloudFormation`.

### Measurement (how it maps onto the existing columns)
- `total_s` — wall-clock of the whole `deploy.sh` (stack-1 + AMI bake + stack-2 + health
  wait), measured identically in spirit to the Terraform `terraform apply` wall-clock.
- `config_s` — the WordPress install window on the builder. `CONFIG_START` is stamped
  **before** `apt-get install`, so `config_s` spans the full configuration work
  (packages + WordPress + config), matching the *scope* of the Ansible/Chef `config_s`.
- `tf_apply_s` — reused as the CloudFormation **infrastructure/provisioning** time
  (`total_s − config_s`).

### Caveats (cite these when reporting the CloudFormation comparison)
1. **`config_s` is apples-to-oranges in *kind*, not scope.** CloudFormation configures via a
   plain `bash` user-data script, not a configuration-management tool. The measured scope is
   identical (apt + WordPress + config), so the number is fair, but it should be framed as
   "shell-based configuration has no CM-tool overhead" — **not** "CloudFormation is a better
   config tool than Ansible/Chef." Its speed advantage comes precisely from *not* being a CM
   tool (no per-task SSH round-trips like Ansible, no chef-client bootstrap like Chef).
2. **CloudFormation `tf_apply_s` carries extra orchestration that the single Terraform apply
   does not.** It spans two separate stacks (foundation+builder, then compute), an explicit
   `aws ec2 create-image` bake, and an explicit target-health poll in `deploy.sh`. This is
   *analogous* to — but heavier than — Terraform's in-apply `aws_ami_from_instance` +
   `wait_for_capacity`/`min_elb_capacity`. So the CloudFormation infra figure is inflated
   relative to the single-command Terraform stacks; treat the infra comparison as
   directional, not a like-for-like provisioning benchmark.
3. **Golden-AMI parity required two CloudFormation-specific mechanisms** (documented in code):
   the builder signals completion via a **`curl`-based `WaitConditionHandle`** (not
   `cfn-signal`, whose `aws-cfn-bootstrap` is fragile on Ubuntu 24.04), and the ASG Launch
   Template carries a **boot hook** (`mount -a` + `systemctl enable --now php8.3-fpm nginx`)
   because golden-AMI instances do not re-run a configuration tool on boot. Without the boot
   hook, instances returned HTTP 502 (php-fpm not up) and the ASG churned. The AMI bake is
   **reboot-validated** (no `--no-reboot`), kept consistent across all 10 runs.

### Results (n=10 each; exact Mann-Whitney p, Holm-adjusted over the article's nine tests)
| Comparison | Result | p (exact) | p Holm | Cohen's d |
|------------|--------|---|---|-----------|
| `config_s`: CloudFormation vs Chef   | CloudFormation significantly faster | <0.0001 | <0.0001 | 2.53 (large) |
| `config_s`: Chef vs Ansible          | Chef significantly faster           | 0.0009 | 0.0046 | 2.54 (large) |
| `total_s`: CloudFormation vs Chef    | **No significant difference**       | 0.089  | 0.178 | 0.51 (medium) |
| `tf_apply_s`: CloudFormation vs Chef    | Chef significantly faster (infra)  | <0.0001 | <0.0001 | 2.53 (large) |
| `tf_apply_s`: CloudFormation vs Ansible | Ansible significantly faster (infra) | 0.0068 | 0.021 | 1.29 (large) |

Descriptive means (s): config — CloudFormation **68 ± 2** < Chef 162 < Ansible 270;
infra — Chef 1590 < Ansible 1625 < CloudFormation **1717**; total — Chef 1752 ≈
CloudFormation 1785 < Ansible 1894.

**Summary finding:** CloudFormation has the fastest (and most consistent) configuration time
but the slowest provisioning time; the two effects roughly cancel, leaving its total
deployment time statistically indistinguishable from Chef and faster than Ansible. Reproduce
with `python scripts/runs.py stats --metric <m> --compare CloudFormation <Chef|Ansible>`.

### Qualitative recommendation (architectural, not from the timing data)
Beyond the measured metrics, a standard tool-property trade-off informs tool choice and
belongs in the discussion/recommendation (not the empirical results): **CloudFormation is a
first-party / AWS-native tool** (AWS-managed state and rollback, deep AWS integration, no
extra tooling) at the cost of **AWS-only lock-in**, whereas **Terraform (+ Ansible/Chef) is
cloud-agnostic and portable** across providers (Terraform supports 4000+ providers; Ansible/
Chef are platform-agnostic). This is consistent with the Bab II classification (Chiari et
al., Aviv et al.) of Terraform as cloud-agnostic provisioning. Recommendation: choose
CloudFormation when committed to AWS and wanting a native tool; choose Terraform-Ansible/Chef
when portability or multi-cloud is a priority.
