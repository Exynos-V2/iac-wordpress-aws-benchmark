# CloudFormation Stack (Stack C) — WordPress Multi-AZ on AWS

A third IaC stack alongside `terraform-ansible/` and `terraform-chef/`, this time using
**AWS CloudFormation** as the provisioning layer. It deploys the **same architecture** (VPC,
6 subnets, no NAT, 5 security groups, EFS, RDS MySQL Multi-AZ, ALB, private ASG) and a
**fully working WordPress site**, configured with **native CloudFormation** (`cfn-init` /
user-data on a builder, a shell port of the Ansible playbook) and the **golden-AMI** pattern.

Because the private ASG has no internet route (no NAT), WordPress is installed once on a
builder in the *public* subnet, baked into a golden AMI, and the ASG launches from that AMI —
identical to how the Terraform stacks work.

## Layout
| File | Purpose |
|------|---------|
| `01-infra-builder.yaml` | Stack 1: network + security + EFS + RDS + builder (cfn-init WordPress, records timing tags, cfn-signal). |
| `02-compute.yaml` | Stack 2: Launch Template + ASG + ALB + target group + listener (uses the golden AMI). |
| `deploy.sh` | Orchestrates the full deploy (Stack 1 → bake AMI → Stack 2 → wait healthy). Run in WSL. |
| `destroy.sh` | Tears everything down (Stack 2 → deregister AMI + snapshot → Stack 1). Run in WSL. |
| `health.sh` | Diagnoses unhealthy ASG targets (stack status, target-health reasons, HTTP code). |
| `idempotency.sh` | One idempotency iteration: change set + drift detection over both stacks. Read-only. |
| `parameters.example.json` | Parameter values; copy and edit `KeyPairName` / `DbPassword`. |

CloudFormation has no native "snapshot instance to AMI" step (the Terraform analogue is
`aws_ami_from_instance`), so `deploy.sh` performs exactly one non-CloudFormation action —
`aws ec2 create-image` — to bridge Stack 1 and Stack 2.

## Prerequisites
- **AWS CLI in WSL**, and valid **Learner Lab credentials** available to your Windows session
  (env vars or `%USERPROFILE%\.aws\credentials`) — the same ones Terraform uses. STS rotates
  ~4h; refresh before a run.
- An existing **EC2 key pair** in the account (Learner Lab default is usually `vockey`).
- `python3` in WSL (used by the scripts to parse JSON / write `.last_run.json`).

### Credentials: automatic forwarding (recommended)
When you run **`python scripts/runs.py cfn-run`**, it automatically forwards the AWS
credentials from your current PowerShell session into WSL (via `WSLENV`) — no copying. It
looks for `AWS_*` env vars first, then the Windows `~/.aws/credentials` default profile.
You'll see `==> forwarding AWS credentials to WSL (source: ...)`. If it prints a "No AWS
credentials found" warning, set them on the Windows side first.

If instead you run `deploy.sh` **directly inside WSL**, that WSL shell needs its own creds
(e.g. `aws sts get-caller-identity` works there), since the forwarding only happens through
the `cfn-run` wrapper.

## Run it

```bash
# 1) edit your values
cp parameters.example.json parameters.json   # or edit the example in place
#    set KeyPairName and a strong DbPassword

# 2) deploy (run in WSL)
PARAMS_FILE=parameters.json wsl bash cloudformation/deploy.sh
#    -> prints  http://<alb-dns>/  when done

# 3) tear down
wsl bash cloudformation/destroy.sh

# 4) redeploy = run deploy.sh again (clean, repeatable round-trip)
```

### Timed runs (records to runs.csv, comparable to the Terraform stacks)
```bash
python scripts/runs.py cfn-run            # deploys via WSL, times it, appends a row
python scripts/runs.py cfn-run --timeout 55   # with a wall-clock watchdog
python scripts/runs.py show               # CloudFormation row appears beside Ansible/Chef
```
`cfn-run` records the same columns as the Terraform `run` command:
- `total_s` — wall-clock of the whole deploy.
- `config_s` — WordPress install time, split from the **builder timing tags**
  (`config_start_ts` / `config_end_ts`) the builder writes during `cfn-init` (the analogue of
  Terraform's `time_static` markers). `deploy.sh` reads them into `.last_run.json`.
- `tf_apply_s` — reused here as the CloudFormation **infra/provisioning** time
  (`total_s − config_s`). The column name is Terraform-era; for this stack read it as
  "infrastructure time".

### Idempotency test
```bash
python scripts/runs.py cfn-idempotency --repeats 5              # change set + drift
python scripts/runs.py cfn-idempotency --repeats 5 --skip-drift # faster, change set only
```
Run it **while both stacks are still up** (right after a successful `cfn-run`, before
`cfn-destroy`). `.last_run.json` is optional: `destroy.sh` deletes it, so when it is absent
the golden AMI id is read back from the live stack's own `GoldenAmiId` parameter. If the
stacks are already destroyed the test exits with the stack status and you must `cfn-run`
again first.
Results append to `idempotency.csv` alongside the Ansible and Chef rows.

CloudFormation cannot re-run its configuration layer: the builder's user-data executes once
at first boot and the service will not re-execute it without replacing the instance. So the
test asks the declarative question instead — re-submit the **identical** templates and
parameters and see whether CloudFormation would act:

| Check | What it does | 0 means |
|-------|--------------|---------|
| Change set | `create-change-set --change-set-type UPDATE` per stack, counted, then **deleted without executing** | The identical declaration is a no-op |
| Drift | `detect-stack-drift` per stack | No live resource diverged from its template |

`changed_count = change-set changes + drifted resources`, so 0 = fully idempotent, the same
convention as Ansible's "changed tasks" and Chef's "resources updated". Nothing is created,
modified, or destroyed by the test.

> **Scope caveat.** This covers the *declarative infrastructure* layer only. The Ansible/Chef
> iterations re-execute the *whole* configuration (packages, EFS mount, WordPress files,
> `wp-config.php`, services) on a live host. The numbers answer different questions — report
> the difference qualitatively, not as a head-to-head metric. See `LIMITATIONS.md` §7.

## Learner Lab constraints honored
- **No IAM creation** — uses the existing `LabInstanceProfile` only (no `AWS::IAM::Role/Policy`).
- **No NAT** — private route table has no default route; the golden AMI lets the private ASG
  serve WordPress without outbound internet.
- **AWS-managed encryption only** — `Encrypted` / `StorageEncrypted: true`, never a `KmsKeyId`.
- **No Secrets Manager** — `DbPassword` is a `NoEcho` parameter threaded into the builder
  user-data (same trust model as the Terraform stacks' `extra_vars`).
- RDS `DeletionPolicy: Delete` so teardown leaves **no final snapshot** behind.

## Notes / known differences vs the Terraform stacks
- **Two stacks + a script** instead of one `terraform apply`: CloudFormation can't bake an AMI
  mid-template, so the AMI step lives in `deploy.sh`. Everything else is pure CloudFormation.
- **Stack 2 completion ≠ healthy**: unlike Terraform's `min_elb_capacity`, CloudFormation
  finishes the ASG once instances launch (golden-AMI instances don't run cfn helpers, so they
  can't `cfn-signal`). `deploy.sh` therefore polls `describe-target-health` until a target is
  healthy, so `total_s` stays comparable to the Terraform timing.
- The builder needs `aws-cfn-bootstrap` (Ubuntu lacks cfn helpers); the user-data installs it
  via `pip` (the builder is in the public subnet with internet).

## Appendix — why not "export" the Terraform deployment?
The original idea was to export an existing Terraform deployment into CloudFormation, delete
it, and redeploy. That was rejected:
- CloudFormation can't read Terraform; the only "export" tools (**CloudFormation IaC
  Generator** `create-generated-template`, or third-party **Former2**) scan the *live account*
  and emit flat, hardcoded templates.
- They capture only AWS resources — **not** the WordPress configuration / golden-AMI layer. A
  generated template would reference the already-baked AMI id; deleting everything (AMI +
  snapshot) and redeploying then points at an AMI that no longer exists → redeploy fails.
- Generated templates are not comparison-grade, and IaC Generator availability inside Learner
  Lab is uncertain.

Hand-authoring the templates here gives a clean, repeatable deploy → delete → redeploy
round-trip and a fair third stack for the thesis comparison.
