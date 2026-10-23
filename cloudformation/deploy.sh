#!/usr/bin/env bash
# Deploy the CloudFormation WordPress stack (golden-AMI faithful) end-to-end:
#   Stack 1 (network/data/builder + cfn-init WordPress) -> bake AMI -> Stack 2 (LT/ASG/ALB)
#   -> poll target health -> write .last_run.json.
# Run inside WSL (aws cli lives in WSL). Mirrors a single `terraform apply`.
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
S1="${STACK1_NAME:-skripsi-iac-cfn-1}"
S2="${STACK2_NAME:-skripsi-iac-cfn-2}"
PARAMS_FILE="${PARAMS_FILE:-parameters.example.json}"
HEALTH_TIMEOUT_TRIES="${HEALTH_TIMEOUT_TRIES:-40}"   # x15s = 10 min max wait for ELB health

log() { echo "==> $*"; }
out() { aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
          --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

[ -f "$PARAMS_FILE" ] || { echo "Missing $PARAMS_FILE (copy parameters.example.json and edit)"; exit 2; }

# JSON object -> "Key=Value" list for `aws cloudformation deploy --parameter-overrides`.
mapfile -t OVERRIDES < <(python3 -c "import json;[print(f'{k}={v}') for k,v in json.load(open('$PARAMS_FILE')).items()]")
KEYPAIR="$(python3 -c "import json;print(json.load(open('$PARAMS_FILE')).get('KeyPairName',''))")"

# ---------- Stack 1: foundation + builder (blocks on builder cfn-signal) ----------
log "Deploying $S1 (network + data + builder + WordPress via cfn-init)"
aws cloudformation deploy \
  --template-file 01-infra-builder.yaml \
  --stack-name "$S1" \
  --parameter-overrides "${OVERRIDES[@]}" \
  --region "$REGION"

BUILDER="$(out "$S1" BuilderInstanceId)"
VPC="$(out "$S1" VpcId)"
PUBA="$(out "$S1" PublicSubnetAId)"
PUBB="$(out "$S1" PublicSubnetBId)"
APPA="$(out "$S1" PrivateAppSubnetAId)"
APPB="$(out "$S1" PrivateAppSubnetBId)"
SGALB="$(out "$S1" SgAlbId)"
SGAPP="$(out "$S1" SgAppId)"

# Timing markers the builder reported in its WaitCondition signal Data
# (analogue of Terraform time_static): {"builder":"<start>,<end>"}.
SIGDATA="$(out "$S1" BuilderSignalData)"
export SIGDATA
CSTART="$(python3 -c "import json,os;print(next(iter(json.loads(os.environ['SIGDATA']).values())).split(',')[0])" 2>/dev/null || echo '')"
CEND="$(python3 -c "import json,os;print(next(iter(json.loads(os.environ['SIGDATA']).values())).split(',')[1])" 2>/dev/null || echo '')"

# ---------- Bake the golden AMI (the only non-CloudFormation step) ----------
log "Baking golden AMI from builder $BUILDER"
# Reboot-validate the snapshot (no --no-reboot), matching Terraform's aws_ami_from_instance
# and keeping the bake method consistent across all collected CFN runs. The reboot makes the
# snapshot slower/variable, so we DON'T use `aws ec2 wait image-available` (caps at ~10 min and
# tripped on run 27) — a longer custom poll below tolerates the variable snapshot time.
AMI="$(aws ec2 create-image --region "$REGION" --instance-id "$BUILDER" \
        --name "skripsi-iac-cfn-wp-$(date +%s)" --query ImageId --output text)"
log "Waiting for AMI $AMI to become available (up to ~25 min)"
ISTATE=pending
for _ in $(seq 1 100); do
  ISTATE="$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" --query 'Images[0].State' --output text 2>/dev/null || echo pending)"
  [ "$ISTATE" = "available" ] && break
  [ "$ISTATE" = "failed" ] && { echo "AMI $AMI build failed"; exit 1; }
  sleep 15
done
[ "$ISTATE" = "available" ] || { echo "AMI $AMI not available after timeout (state=$ISTATE)"; exit 1; }

# ---------- Stack 2: compute/serving ----------
log "Deploying $S2 (Launch Template + ASG + ALB) from AMI $AMI"
aws cloudformation deploy \
  --template-file 02-compute.yaml \
  --stack-name "$S2" \
  --parameter-overrides \
      GoldenAmiId="$AMI" VpcId="$VPC" \
      PublicSubnetAId="$PUBA" PublicSubnetBId="$PUBB" \
      PrivateAppSubnetAId="$APPA" PrivateAppSubnetBId="$APPB" \
      SgAlbId="$SGALB" SgAppId="$SGAPP" KeyPairName="$KEYPAIR" \
  --region "$REGION"

ALB="$(out "$S2" AlbDnsName)"
TG="$(out "$S2" TargetGroupArn)"
ASG="$(out "$S2" AsgName)"

# ---------- Block until at least one target is ELB-healthy (parity with Terraform) ----------
log "Waiting for ASG targets to pass ALB health checks"
HEALTHY=0
for _ in $(seq 1 "$HEALTH_TIMEOUT_TRIES"); do
  HEALTHY="$(aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$TG" \
              --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text)"
  [ "${HEALTHY:-0}" -ge 1 ] && break
  sleep 15
done

# ---------- Record run metadata for the Python tracker ----------
python3 - "$BUILDER" "$AMI" "$CSTART" "$CEND" "$ALB" "$ASG" "$HEALTHY" <<'PY'
import json, sys
keys = ["builder_id","ami_id","config_start_ts","config_end_ts","alb_dns","asg_name","healthy_targets"]
json.dump(dict(zip(keys, sys.argv[1:])), open(".last_run.json","w"), indent=2)
PY

log "Done. WordPress URL: http://$ALB/   (healthy targets: $HEALTHY)"
echo "ALB_DNS=$ALB"
