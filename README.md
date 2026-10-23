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
`config_s` (configuration time), `total_s`, `status`, `error_category`, `log_path`. Gaps in
`run_id` and the `Fail` row are explained in `LIMITATIONS.md`.

In the logs, the database password, the AWS account ID, and public IP addresses are replaced with
`<REDACTED_DB_PASSWORD>`, `<AWS_ACCOUNT_ID>`, and `<PUBLIC_IP>`.
