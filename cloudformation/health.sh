#!/usr/bin/env bash
# Diagnose why the ASG instances aren't passing ALB health checks. Run in WSL.
set -uo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
S2="${STACK2_NAME:-skripsi-iac-cfn-2}"

out() { aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
          --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null; }

ST="$(aws cloudformation describe-stacks --stack-name "$S2" --region "$REGION" \
        --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo NOT_FOUND)"
echo "Stack $S2 status: $ST"
if [ "$ST" = "NOT_FOUND" ]; then
  echo "Compute stack not found — deploy it first (python scripts/runs.py cfn-run)."
  exit 0
fi

TG="$(out "$S2" TargetGroupArn)"
ALB="$(out "$S2" AlbDnsName)"
ASG="$(out "$S2" AsgName)"
echo "TargetGroup: ${TG:-<none>}"
echo "ALB:         ${ALB:-<none>}"
echo "ASG:         ${ASG:-<none>}"

if [ -n "${ASG:-}" ]; then
  echo
  echo "=== ASG instances ==="
  aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" --region "$REGION" \
    --query "AutoScalingGroups[0].Instances[].[InstanceId,LifecycleState,HealthStatus,AvailabilityZone]" --output text
fi

if [ -n "${TG:-}" ]; then
  echo
  echo "=== ALB target health (Id / Port / State / Reason / Description) ==="
  aws elbv2 describe-target-health --target-group-arn "$TG" --region "$REGION" \
    --query "TargetHealthDescriptions[].[Target.Id,Target.Port,TargetHealth.State,TargetHealth.Reason,TargetHealth.Description]" \
    --output text
fi

if [ -n "${ALB:-}" ]; then
  echo
  echo "=== ALB HTTP response (from WSL over the internet) ==="
  curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 "http://$ALB/" || echo "curl failed/timed out"
fi

# Serial console of the first InService instance — shows boot / systemd / mount / nginx / php errors.
if [ -n "${ASG:-}" ]; then
  IID="$(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG" --region "$REGION" \
          --query "AutoScalingGroups[0].Instances[?LifecycleState=='InService']|[0].InstanceId" --output text 2>/dev/null)"
  if [ -n "${IID:-}" ] && [ "$IID" != "None" ]; then
    echo
    echo "=== serial console of $IID (filtered: error/fail/mount/efs/nginx/php/fpm/wordpress) ==="
    aws ec2 get-console-output --instance-id "$IID" --latest --region "$REGION" --output text 2>/dev/null \
      | grep -iE "error|fail|denied|timed out|mount|efs|nfs|nginx|php|fpm|wordpress|cloud-init|wp-config" \
      | tail -40
    echo "(if blank, console not populated yet — wait ~3-4 min after launch and re-run)"
  fi
fi
