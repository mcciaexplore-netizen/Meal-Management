#!/bin/sh
# Build the image, push it to the stack's ECR repository, and roll both ECS
# services onto it. Usage: sh deploy/aws/push-image.sh [stack-name] [tag]
set -eu

stack=${1:-meal-management}
tag=${2:-$(git rev-parse --short HEAD)}
region=${AWS_REGION:-$(aws configure get region)}
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

output() {
    aws cloudformation describe-stacks --region "$region" --stack-name "$stack" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

repository=$(output RepositoryUri)
cluster=$(output ClusterName)
registry=${repository%%/*}

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
docker build --platform linux/amd64 -t "$repository:$tag" "$project_root"
docker push "$repository:$tag"

# Point the task definitions at the new tag through the stack, so CloudFormation
# stays the source of truth.
aws cloudformation update-stack --region "$region" --stack-name "$stack" \
    --use-previous-template --capabilities CAPABILITY_IAM \
    --parameters \
        ParameterKey=ImageTag,ParameterValue="$tag" \
        $(for key in VpcId LoadBalancerSubnetIds TaskSubnetIds AssignPublicIp CertificateArn AdminHostname ScannerHostname AppSecretArn DbHost DbPort DbName EmailSender EmailSendEnabled ScanAppEnabled AdminDesiredCount ScannerDesiredCount; do
            printf 'ParameterKey=%s,UsePreviousValue=true ' "$key"
        done)
aws cloudformation wait stack-update-complete --region "$region" --stack-name "$stack"
aws ecs wait services-stable --region "$region" --cluster "$cluster" --services admin scanner
printf 'Deployed %s:%s\n' "$repository" "$tag"
