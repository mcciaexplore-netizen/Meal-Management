#!/bin/sh
# Build the image in AWS CodeBuild, push it to the stack's ECR repository, and
# roll both ECS services onto it. No local Docker is needed.
# Usage: sh deploy/aws/push-image.sh [stack-name]
set -eu

stack=${1:-meal-management}
region=${AWS_REGION:-$(aws configure get region)}
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project_root"

tag=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then
    tag="$tag-dirty-$(date +%Y%m%d%H%M%S)"
fi

output() {
    aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

bucket=$(output SourceBucketName)
project=$(output ImageBuildProject)
cluster=$(output ClusterName)

# Snapshot tracked and new (non-ignored) files, so uncommitted work is built too.
archive=$(mktemp -d)/source.zip
git ls-files -z --cached --others --exclude-standard | python3 -I -c '
import sys, zipfile, os
names = [n for n in sys.stdin.buffer.read().decode().split("\0") if n and os.path.isfile(n)]
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for n in names:
        z.write(n)
' "$archive"
aws s3 cp --region "$region" --only-show-errors "$archive" "s3://$bucket/source.zip"
rm -f "$archive"

build=$(aws codebuild start-build --region "$region" --project-name "$project" \
    --environment-variables-override name=IMAGE_TAG,value="$tag",type=PLAINTEXT \
    --query build.id --output text)
printf 'Building image %s (%s)...\n' "$tag" "$build"
while :; do
    status=$(aws codebuild batch-get-builds --region "$region" --ids "$build" --query 'builds[0].buildStatus' --output text)
    case "$status" in
        IN_PROGRESS) sleep 15 ;;
        SUCCEEDED) break ;;
        *)
            printf 'Image build %s. Logs: CloudWatch group /aws/codebuild/%s\n' "$status" "$project" >&2
            exit 1
            ;;
    esac
done

# Point the task definitions at the new tag through the stack, so CloudFormation
# stays the source of truth.
aws cloudformation update-stack --region "$region" --stack-name "$stack" \
    --use-previous-template --capabilities CAPABILITY_IAM \
    --parameters \
        ParameterKey=ImageTag,ParameterValue="$tag" \
        $(for key in VpcId VpcCidr LoadBalancerSubnetIds TaskSubnetIds AssignPublicIp CertificateArn AdminHostname ScannerHostname AppSecretArn DbHost DbPort DbName EmailSender EmailSendEnabled ScanAppEnabled AdminDesiredCount ScannerDesiredCount; do
            printf 'ParameterKey=%s,UsePreviousValue=true ' "$key"
        done) >/dev/null
aws cloudformation wait stack-update-complete --region "$region" --stack-name "$stack"
aws ecs wait services-stable --region "$region" --cluster "$cluster" --services admin scanner
printf 'Deployed image tag %s\n' "$tag"
