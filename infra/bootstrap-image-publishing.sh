#!/usr/bin/env bash
# Run only in the explicitly approved AWS account after policy review.
set -euo pipefail
export AWS_PAGER=''
region=ap-south-1
account=521595303406
role=market-intel-image-publisher
root=$(cd "$(dirname "$0")/.." && pwd)
test "$(aws sts get-caller-identity --query Account --output text)" = "$account"
for service in dashboard market news; do
  repo="market-intel/$service"
  if ! aws ecr describe-repositories --region "$region" --repository-names "$repo" >/dev/null 2>&1; then
    aws ecr create-repository --region "$region" --repository-name "$repo" \
      --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
      --tags Key=Project,Value=market-intel >/dev/null
  fi
done
provider="arn:aws:iam::$account:oidc-provider/token.actions.githubusercontent.com"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --tags Key=Project,Value=market-intel >/dev/null
fi
if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
  owner=$(aws iam list-role-tags --role-name "$role" --query "Tags[?Key=='Project'].Value | [0]" --output text)
  test "$owner" = market-intel || { echo 'Existing role is not owned by this project; stop for review'; exit 1; }
else
  aws iam create-role --role-name "$role" \
    --assume-role-policy-document "file://$root/infra/github-image-trust.json" \
    --tags Key=Project,Value=market-intel >/dev/null
fi
aws iam put-role-policy --role-name "$role" --policy-name market-intel-ecr-publish \
  --policy-document "file://$root/infra/github-image-permissions.json"
printf 'Image role ready: arn:aws:iam::%s:role/%s\n' "$account" "$role"
