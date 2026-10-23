# IaC WordPress AWS Benchmark

Three Infrastructure-as-Code (IaC) stacks that deploy the same Multi-AZ WordPress site on AWS,
plus the scripts, raw data, and logs used to compare them on deployment time, error rate, and
idempotency.

| Stack | Provisioning | Configuration | Folder |
|-------|--------------|---------------|--------|
| A | Terraform | Ansible | `terraform-ansible/` |
| B | Terraform | Chef (local mode) | `terraform-chef/` |
| C | AWS CloudFormation | EC2 user-data script | `cloudformation/` |

Stacks A and B share identical Terraform code. Only the configuration step differs.

## What gets deployed

- VPC with public, private app, and private data subnets across two Availability Zones
- Application Load Balancer
- Auto Scaling Group running WordPress (nginx + PHP-FPM) from a golden AMI
- RDS MySQL Multi-AZ and EFS for shared `wp-content`
- A temporary builder EC2 that is configured, then baked into the golden AMI

A single command ends with a working WordPress site behind the load balancer. The private subnets
have no NAT Gateway, because WordPress is already installed in the AMI.

## Repository layout

```
terraform-ansible/    Terraform + Ansible playbook
terraform-chef/       Terraform + Chef cookbook
cloudformation/       CloudFormation templates and deploy/destroy scripts
scripts/runs.py       Timed deployments, idempotency checks, and statistics
build_grafik_uji.py   Boxplot chart of the Terraform-Chef vs Terraform-Ansible results
runs.csv              One row per deployment session
idempotency.csv       One row per idempotency check
logs/                 Log of every deployment and idempotency check
LIMITATIONS.md        Measurement notes and caveats
```

## Requirements

- Terraform, AWS CLI, Python 3 (with `scipy` and `matplotlib` for the analysis)
- Ansible for stack A (run from WSL or Linux)
- An AWS account; the experiments ran on AWS Academy Learner Lab in `us-east-1`

## Usage

Deploy and time one session:

```bash
cp terraform-ansible/terraform.tfvars.example terraform-ansible/terraform.tfvars   # then edit it
python scripts/runs.py run --stack Ansible      # or --stack Chef
python scripts/runs.py cfn-run                  # CloudFormation
```

Check idempotency on a live deployment:

```bash
python scripts/runs.py idempotency --stack Chef
python scripts/runs.py cfn-idempotency
```

Recompute the statistics from the recorded data (no AWS needed):

```bash
python scripts/runs.py stats --compare Chef Ansible
python scripts/runs.py table
python scripts/runs.py table --baseline Chef --vs Ansible
python build_grafik_uji.py
```

Destroy everything after each run (`terraform destroy -auto-approve` or
`python scripts/runs.py cfn-destroy`).

## Data

`runs.csv` columns: `run_id`, `date` (WIB, UTC+7), `stack`, `tf_apply_s` (infrastructure time),
`config_s` (configuration time), `total_s`, `status`, `error_category`, `log_path`.

The analysis uses the 30 `Success` rows (10 per stack). Run IDs are not renumbered, so gaps
match the log file names.

### Failed and excluded runs

| Run | Stack | What happened | How it is treated |
|-----|-------|---------------|-------------------|
| 15 | Terraform-Ansible | The host machine went to sleep mid-run, which killed the WSL control node and `ansible-playbook` (no `fatal:` from the playbook). | Discarded as an operator-side interruption and re-run. Removed from `runs.csv`; log kept. |
| 19 | Terraform-Ansible | Stopped by the 55-minute script timeout. The recursive `chmod` over the EFS-mounted `wp-content` stalled on EFS latency. The same task had succeeded in the previous 8 Ansible runs, and the Chef recipe runs the same step. No configuration error message was logged. | Kept in `runs.csv` as `Fail`, recorded as an environmental failure, and excluded from the timing data. |
| 23 (earlier logs) | CloudFormation | `run-023-...-131204`, `-141612`, and `-160424` are template tests made before data collection started (one stack-creation failure, one manual interrupt, one test deployment). | Not part of the sample. The recorded run 23 is `run-023-...-165914`. |
| 27 (earlier log) | CloudFormation | `run-027-...-151826`: the first stack, including builder configuration, completed, but the AWS CLI waiter for the golden AMI gave up (`Waiter ImageAvailable failed: Max attempts exceeded`). No configuration error was logged. | Treated as an environmental failure; the stacks were destroyed and the session was repeated as the recorded run 27 (`-155714`). Before the repeat, `deploy.sh` replaced `aws ec2 wait image-available` (40 polls, about 10 minutes) with its own loop of up to 100 polls. Both poll every 15 seconds, so only the time limit changed, not the measured duration. |
| 33 | CloudFormation | A successful deployment made only to give the idempotency test a live stack. | Not a timing sample; removed from `runs.csv` so CloudFormation stays at n = 10 (runs 23-32). Log kept. |

`LIMITATIONS.md` has the full measurement notes.

In the logs, the database password, the AWS account ID, and public IP addresses are replaced with
`<REDACTED_DB_PASSWORD>`, `<AWS_ACCOUNT_ID>`, and `<PUBLIC_IP>`.
