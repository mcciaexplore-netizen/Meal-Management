# AWS ECS deployment

The admin and scanner applications run as two ECS Fargate services built from one Docker image. One HTTPS Application Load Balancer routes by hostname: `AdminHostname` goes to the admin service and `ScannerHostname` to the scanner. The database stays on the existing Aiven MySQL instance. Photos go to a private S3 bucket and email goes through Gmail.

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | Builds the frontend in a Node stage and runs the backend on Python 3.12 as a non-root user. `MEAL_APPLICATION=admin` or `scanner` selects the process. |
| `deploy/docker-entrypoint.sh` | Writes `DB_SSL_CA_PEM` to a private file and sets `DB_SSL_CA`. Starts uvicorn trusting `X-Forwarded-*` headers only from `FORWARDED_ALLOW_IPS` (the stack sets this to the VPC range). That way the load balancer's `X-Forwarded-Proto: https` satisfies the production HTTPS check, and clients can't fake their IP to get around the login and scan rate limits. Never set it to `*`. Any arguments run as a one-off command instead. |
| `.dockerignore` | Keeps `.env*`, `*.pem`, `.venv`, tests, docs and deploy tooling out of the image. |
| `docker-compose.yml` | Runs both services locally from the same image, reading settings from `.env.docker`. |
| `deploy/aws/ecs-stack.yaml` | CloudFormation for the ECR repository, S3 bucket, ECS cluster, load balancer, IAM roles, log group, task definitions and both services. |
| `deploy/aws/app-secret.example.json` | The keys the Secrets Manager secret must contain. |
| `deploy/aws/push-image.sh` | Uploads a snapshot of the working tree to S3, builds the image in CodeBuild (no local Docker needed), then updates the stack to the new tag and waits for both services to stabilise. |

## What the stack creates

- **Image build.** A CodeBuild project and a private S3 bucket for source snapshots, which are deleted after 14 days. Base images come from ECR Public, which avoids Docker Hub's pull limits.
- **ECR repository.** Named after the stack. Images are scanned on push, and only the last 20 are kept.
- **S3 bucket for photos.**
  - Private, with all public access blocked.
  - SSE-S3 encryption and versioning on.
  - Rejects requests that don't use TLS.
  - Kept if the stack is deleted.
- **ECS cluster.** Container Insights is on, and both services log to the CloudWatch group `/ecs/<stack>` with 30-day retention.
- **IAM roles.**
  - The execution role pulls images, writes logs, and reads the one app secret.
  - The task role can read, write and delete photos in the bucket, and allows ECS Exec.
- **Security groups.** The load balancer accepts ports 80 and 443 from anywhere. The tasks accept port 8000 only from the load balancer.
- **Load balancer.**
  - HTTP redirects to HTTPS. HTTPS uses TLS 1.2/1.3 with your ACM certificate.
  - Two host-header rules send traffic to the right service; any other hostname gets a 404.
- **Two Fargate services, `admin` and `scanner`.**
  - Each task has 0.5 vCPU, 1 GB of memory, and runs 2 uvicorn workers.
  - Deployments roll back automatically if the new tasks fail (deployment circuit breaker).
  - ECS Exec is on, so you can run commands inside a running task.
  - Each container checks `/health/live` itself.

## One-time setup

1. **Certificate.** In ACM, in the deployment region, request a certificate covering both hostnames and validate it through DNS.
2. **Secret.** Create one Secrets Manager secret with the keys from `deploy/aws/app-secret.example.json`. Use the **same** `QR_ENCRYPTION_KEYS`, `APP_CSRF_SECRET`, `LOGIN_RATE_SECRET` and `SCANNER_ACTIVATION_SECRET` values the current deployment uses:
   - Different QR keys make existing QR codes unreadable.
   - A different CSRF secret logs out every paired scanner device.

   Put the Aiven CA certificate text in `DB_SSL_CA_PEM`. Then create the secret:

   ```sh
   aws secretsmanager create-secret --name meal-management/app --secret-string file://app-secret.json
   ```

   Create `app-secret.json` outside the repository and delete it afterwards.
3. **Database network access.** If the Aiven service restricts client IPs, the tasks need a fixed outbound address:
   - Run them in private subnets behind a NAT gateway with an Elastic IP (`AssignPublicIp=DISABLED`).
   - Add that Elastic IP to Aiven's allowed IP addresses.

   Public subnets with `AssignPublicIp=ENABLED` work only if Aiven accepts any address, because task IPs change on every deployment.
4. **Create the stack** with zero tasks, because the repository is still empty. Use a lowercase stack name; it becomes the ECR repository name.

   ```sh
   aws cloudformation create-stack --stack-name meal-management \
     --template-body file://deploy/aws/ecs-stack.yaml --capabilities CAPABILITY_IAM \
     --parameters \
       ParameterKey=VpcId,ParameterValue=vpc-xxxx \
       ParameterKey=LoadBalancerSubnetIds,ParameterValue='subnet-a\,subnet-b' \
       ParameterKey=TaskSubnetIds,ParameterValue='subnet-a\,subnet-b' \
       ParameterKey=CertificateArn,ParameterValue=arn:aws:acm:... \
       ParameterKey=AdminHostname,ParameterValue=meals.example.com \
       ParameterKey=ScannerHostname,ParameterValue=scan.example.com \
       ParameterKey=AppSecretArn,ParameterValue=arn:aws:secretsmanager:... \
       ParameterKey=DbHost,ParameterValue=xxxx.aivencloud.com \
       ParameterKey=DbPort,ParameterValue=12345 \
       ParameterKey=EmailSender,ParameterValue=sender@example.com
   aws cloudformation wait stack-create-complete --stack-name meal-management
   ```
5. **Push the first image.** Run `sh deploy/aws/push-image.sh meal-management`. It needs only the AWS CLI and git; CodeBuild does the Docker build.
6. **Start the services.** Update the stack with `AdminDesiredCount=1` and `ScannerDesiredCount=1`, reusing previous values for the other parameters.
7. **DNS.** Point both hostnames at the `LoadBalancerDnsName` stack output, using a CNAME or a Route 53 alias.

## Releases

```sh
sh deploy/aws/push-image.sh meal-management
```

The image is tagged with the current git commit, plus `-dirty-<timestamp>` when there are uncommitted changes. The script updates the stack to the new tag and waits for both services to stabilise. If the new tasks fail health checks, ECS rolls back to the previous task definition.

## Migrations and the first administrator

Migrations never run on startup. These commands prompt for passwords, so run them interactively inside a running admin task with ECS Exec. This needs the Session Manager plugin for the AWS CLI.

```sh
task=$(aws ecs list-tasks --cluster <ClusterName> --service-name admin --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster <ClusterName> --task "$task" --container app --interactive --command "/bin/sh"
# inside the container:
python manage.py migrate --database-user meal_migrator --allow-database-changes
python manage.py bootstrap-admin --allow-database-access
```

Stop the old admin deployment before applying migrations, as [deployment.md](deployment.md) describes.

## Health checks

The load balancer's health check uses plain HTTP and sends the task IP as the host, so production mode answers `400 HTTPS_REQUIRED`. The target groups therefore accept `200,400`, which proves only that the process is responding. The container health check sends the real hostname and `X-Forwarded-Proto: https` to `/health/live`, and ECS replaces the task if it fails. `/health/ready` also checks MySQL and the migration ledger; check it through the public hostname after a release.

## Not done yet

- The image has never been built. The first CodeBuild run will be its first build.
- The template passes `cfn-lint` but has never been deployed.
- Automatic email sending (`EMAIL_AUTO_SEND_ENABLED`) stays off. Real sending stays off until `EmailSendEnabled=true`.
- Nothing is set up yet for autoscaling, a WAF, alarms, or limiting the admin site to office IPs.
