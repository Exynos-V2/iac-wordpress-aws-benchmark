#!/usr/bin/env bash
# Tear down the CloudFormation WordPress stack (mirrors `terraform destroy`):
#   delete Stack 2 -> deregister golden AMI + delete its snapshot -> delete Stack 1.
# Run inside WSL.
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
S1="${STACK1_NAME:-skripsi-iac-cfn-1}"
S2="${STACK2_NAME:-skripsi-iac-cfn-2}"

log() { echo "==> $*"; }

# ---------- Stack 2 first (depends on the AMI) ----------
log "Deleting $S2"
aws cloudformation delete-stack --stack-name "$S2" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "$S2" --region "$REGION"

# ---------- Deregister golden AMI + delete its backing snapshot ----------
if [ -f .last_run.json ]; then
  AMI="$(python3 -c "import json;print(json.load(open('.last_run.json')).get('ami_id',''))")"
  if [ -n "${AMI:-}" ] && [ "$AMI" != "None" ]; then
    SNAP="$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
             --query 'Images[0].BlockDeviceMappings[0].Ebs.SnapshotId' --output text 2>/dev/null || echo None)"
    log "Deregistering AMI $AMI"
    aws ec2 deregister-image --region "$REGION" --image-id "$AMI" || true
    if [ -n "${SNAP:-}" ] && [ "$SNAP" != "None" ]; then
      log "Deleting snapshot $SNAP"
      aws ec2 delete-snapshot --region "$REGION" --snapshot-id "$SNAP" || true
    fi
  fi
fi

# ---------- Stack 1 last ----------
log "Deleting $S1"
aws cloudformation delete-stack --stack-name "$S1" --region "$REGION"
aws cloudformation wait stack-delete-complete --stack-name "$S1" --region "$REGION"

rm -f .last_run.json
log "Teardown complete."
