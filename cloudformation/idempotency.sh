#!/usr/bin/env bash
# One idempotency iteration for the CloudFormation stack.
#
# CloudFormation has no "re-converge the same host" command the way
# `ansible-playbook` / `chef-client` do: its configuration layer (user-data on
# the builder) runs exactly once at instance launch. Its equivalent of a re-run
# is re-submitting the IDENTICAL templates + parameters and asking the service
# what it would change. Two independent checks are made here:
#
#   1. CHANGE SET  - re-submit the same template/params against the live stack.
#                    An empty change set means the declared desired state
#                    already matches. Change sets are created but NEVER
#                    executed, and are deleted afterwards, so this is
#                    completely non-destructive.
#   2. DRIFT       - `detect-stack-drift` compares the live resources against
#                    the template. DriftedStackResourceCount > 0 means the real
#                    infrastructure diverged from the declaration.
#
# changed_count reported to the tracker = change-set changes + drifted
# resources across both stacks, so 0 == fully idempotent (same convention as
# Ansible's "changed tasks" and Chef's "resources updated").
#
# Run inside WSL. Requires a completed deploy.sh (both stacks up, .last_run.json).
set -uo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-us-east-1}"
S1="${STACK1_NAME:-skripsi-iac-cfn-1}"
S2="${STACK2_NAME:-skripsi-iac-cfn-2}"
PARAMS_FILE="${PARAMS_FILE:-parameters.example.json}"
ITER="${1:-1}"
SKIP_DRIFT="${SKIP_DRIFT:-0}"
CS_PREFIX="idem-$(date +%Y%m%d-%H%M%S)-iter${ITER}"

log() { echo "==> $*"; }
out() { aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
          --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null; }

[ -f "$PARAMS_FILE" ] || { echo "Missing $PARAMS_FILE"; exit 2; }

for S in "$S1" "$S2"; do
  ST="$(aws cloudformation describe-stacks --stack-name "$S" --region "$REGION" \
        --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo MISSING)"
  case "$ST" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) ;;
    *) echo "Stack $S not in a re-runnable state (status=$ST). Run deploy.sh first."; exit 2 ;;
  esac
done

# ---------- parameter lists (identical to what deploy.sh sent) ----------
mapfile -t P1 < <(python3 -c "
import json
for k, v in json.load(open('$PARAMS_FILE')).items():
    print('ParameterKey=%s,ParameterValue=%s' % (k, v))
")
KEYPAIR="$(python3 -c "import json;print(json.load(open('$PARAMS_FILE')).get('KeyPairName',''))")"
# Golden AMI id: prefer .last_run.json, but destroy.sh removes that file and it is
# absent on a stack deployed from another shell. The authoritative value is the
# parameter the live stack was created with, so fall back to reading it back.
AMI=""
[ -f .last_run.json ] && AMI="$(python3 -c "import json;print(json.load(open('.last_run.json')).get('ami_id',''))" 2>/dev/null || echo '')"
if [ -z "$AMI" ] || [ "$AMI" = "None" ]; then
  AMI="$(aws cloudformation describe-stacks --stack-name "$S2" --region "$REGION"           --query "Stacks[0].Parameters[?ParameterKey=='GoldenAmiId'].ParameterValue"           --output text 2>/dev/null)"
  [ -n "$AMI" ] && [ "$AMI" != "None" ] && log "recovered GoldenAmiId=$AMI from live stack $S2"
fi
[ -n "$AMI" ] && [ "$AMI" != "None" ] || { echo "Could not determine the golden AMI id (no .last_run.json and $S2 has no GoldenAmiId parameter)."; exit 2; }

P2=(
  "ParameterKey=GoldenAmiId,ParameterValue=$AMI"
  "ParameterKey=VpcId,ParameterValue=$(out "$S1" VpcId)"
  "ParameterKey=PublicSubnetAId,ParameterValue=$(out "$S1" PublicSubnetAId)"
  "ParameterKey=PublicSubnetBId,ParameterValue=$(out "$S1" PublicSubnetBId)"
  "ParameterKey=PrivateAppSubnetAId,ParameterValue=$(out "$S1" PrivateAppSubnetAId)"
  "ParameterKey=PrivateAppSubnetBId,ParameterValue=$(out "$S1" PrivateAppSubnetBId)"
  "ParameterKey=SgAlbId,ParameterValue=$(out "$S1" SgAlbId)"
  "ParameterKey=SgAppId,ParameterValue=$(out "$S1" SgAppId)"
  "ParameterKey=KeyPairName,ParameterValue=$KEYPAIR"
)

# ---------- 1. change set (created, inspected, deleted - never executed) ----------
# Prints the number of resource changes; 0 means the re-submitted template is a no-op.
changeset_count() {
  local stack="$1" tmpl="$2"; shift 2
  local name="${CS_PREFIX}-${stack##*-}"
  local arn status reason count rc

  arn="$(aws cloudformation create-change-set \
          --stack-name "$stack" --change-set-name "$name" \
          --change-set-type UPDATE --template-body "file://$tmpl" \
          --parameters "$@" --region "$REGION" \
          --query Id --output text 2>&1)"
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "create-change-set failed for $stack: $arn" >&2
    echo "ERR"; return
  fi

  # Poll: CREATE_COMPLETE = real changes; FAILED + "didn't contain changes" = no-op.
  status=PENDING
  for _ in $(seq 1 40); do
    status="$(aws cloudformation describe-change-set --change-set-name "$arn" \
               --region "$REGION" --query Status --output text 2>/dev/null || echo PENDING)"
    [ "$status" = "CREATE_COMPLETE" ] && break
    [ "$status" = "FAILED" ] && break
    sleep 3
  done

  reason="$(aws cloudformation describe-change-set --change-set-name "$arn" \
             --region "$REGION" --query StatusReason --output text 2>/dev/null || echo '')"
  count="$(aws cloudformation describe-change-set --change-set-name "$arn" \
            --region "$REGION" --query 'length(Changes)' --output text 2>/dev/null || echo 0)"

  if [ "$status" = "FAILED" ]; then
    case "$reason" in
      *"contain changes"*|*"No updates are to be performed"*) count=0 ;;
      *) echo "change set $name FAILED: $reason" >&2; count="ERR" ;;
    esac
  elif [ "$status" = "CREATE_COMPLETE" ] && [ "${count:-0}" != "0" ]; then
    # Log which resources CloudFormation would touch - that is the evidence of
    # non-idempotency, so it belongs in the run log, not just the count.
    echo "--- $stack would change: ---" >&2
    aws cloudformation describe-change-set --change-set-name "$arn" --region "$REGION" \
      --query 'Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]' \
      --output text >&2 2>/dev/null || true
  fi

  aws cloudformation delete-change-set --change-set-name "$arn" --region "$REGION" >/dev/null 2>&1 || true
  echo "$count"
}

log "iteration $ITER: change set on $S1"
C1="$(changeset_count "$S1" 01-infra-builder.yaml "${P1[@]}")"
log "iteration $ITER: change set on $S2"
C2="$(changeset_count "$S2" 02-compute.yaml "${P2[@]}")"

# ---------- 2. drift detection ----------
drift_count() {
  local stack="$1" did status n
  did="$(aws cloudformation detect-stack-drift --stack-name "$stack" --region "$REGION" \
          --query StackDriftDetectionId --output text 2>/dev/null)" || { echo "ERR"; return; }
  [ -n "$did" ] || { echo "ERR"; return; }
  status=DETECTION_IN_PROGRESS
  for _ in $(seq 1 60); do
    status="$(aws cloudformation describe-stack-drift-detection-status \
               --stack-drift-detection-id "$did" --region "$REGION" \
               --query DetectionStatus --output text 2>/dev/null || echo DETECTION_IN_PROGRESS)"
    [ "$status" = "DETECTION_IN_PROGRESS" ] || break
    sleep 5
  done
  if [ "$status" != "DETECTION_COMPLETE" ]; then
    echo "drift detection on $stack ended as $status" >&2
    echo "ERR"; return
  fi
  n="$(aws cloudformation describe-stack-drift-detection-status \
        --stack-drift-detection-id "$did" --region "$REGION" \
        --query DriftedStackResourceCount --output text 2>/dev/null || echo ERR)"
  if [ "${n:-0}" != "0" ] && [ "$n" != "ERR" ]; then
    echo "--- $stack drifted resources: ---" >&2
    aws cloudformation describe-stack-resource-drifts --stack-name "$stack" --region "$REGION" \
      --stack-resource-drift-status-filters MODIFIED DELETED \
      --query 'StackResourceDrifts[].[LogicalResourceId,ResourceType,StackResourceDriftStatus]' \
      --output text >&2 2>/dev/null || true
  fi
  echo "$n"
}

if [ "$SKIP_DRIFT" = "1" ]; then
  D1=0; D2=0
  log "iteration $ITER: drift detection skipped (SKIP_DRIFT=1)"
else
  log "iteration $ITER: drift detection on $S1"
  D1="$(drift_count "$S1")"
  log "iteration $ITER: drift detection on $S2"
  D2="$(drift_count "$S2")"
fi

TOTAL="$(python3 -c "
import sys
vals = sys.argv[1:]
print('ERR' if 'ERR' in vals else sum(int(v) for v in vals))
" "$C1" "$C2" "$D1" "$D2")"

echo "CHANGESET_S1=$C1"
echo "CHANGESET_S2=$C2"
echo "DRIFT_S1=$D1"
echo "DRIFT_S2=$D2"
echo "TOTAL_CHANGES=$TOTAL"
[ "$TOTAL" = "ERR" ] && exit 1
exit 0
