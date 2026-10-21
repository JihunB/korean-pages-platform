# Architecture

![architecture](images/architecture.svg)

All resources are in **ap-southeast-2 (Sydney)**, except the CloudFront certificate, which CloudFront requires in us-east-1. Everything is tagged `Project=koreanpages` for cost reporting.

## Request path

1. **Route 53** resolves `koreanpages.com.au` and `www` to CloudFront.
2. **CloudFront** terminates TLS with an ACM certificate and redirects `www` to the apex with a CloudFront Function. It sends:
   - photo paths to the **private S3 media bucket** (readable by CloudFront only)
   - everything else to the origin hostname on the load balancer
3. **Application Load Balancer**
   - HTTPS listener only; the default HTTP listener is disabled
   - TLS policy `ELBSecurityPolicy-TLS13-1-2-2021-06`
   - its security group allows TCP 443 **only from the CloudFront origin-facing managed prefix list**, so the origin can't be reached around the CDN
   - health checks hit `/healthz`
4. **Elastic Beanstalk** (Amazon Linux, nginx → gunicorn) on t4g instances, 1–3 behind CPU-based autoscaling.

## Processes (Procfile)

| Process | Job |
|---|---|
| `web` | Django app: public pages, member pages, admin |
| `worker` | Listing maintenance jobs (Google Places + Claude), backups, AI ad writer |
| `importer` | Job-board and blog imports, plus the scheduled runs |

The two background workers are separate processes. A long listing job can't hold up imports, and neither one competes with web requests for gunicorn workers.

## Data

| Store | Settings |
|---|---|
| RDS PostgreSQL | Not publicly accessible; storage encrypted; deletion protection; 7-day automated backups; reachable only from the app's security group |
| S3 media | Private; read by CloudFront through origin access control |
| S3 backups | Private, versioned; app-level backups (download/restore from the admin) |
| Secrets Manager `<app-secret>` | Database password, Django secret key, third-party API keys |

## Identity

- **Instance role:** the instances use an instance role. No access keys exist on the servers.
- **IMDS:** IMDSv1 is disabled (`DisableIMDSv1 = true`), so a server-side request forgery can't read role credentials with a plain GET.
- **Admin account:** created through a one-time setup link printed by a management command. The owner sets the password, and nobody else ever sees it.

## Deployment

See [runbook-deploy.md](runbook-deploy.md). In short:
- a versioned zip goes to S3, an application version is created from it, and a rolling deploy runs with an additional batch, so capacity never drops
- migrations run once, on the leader instance
- managed platform updates (minor and patch) apply weekly in a fixed window
- instance logs stream to CloudWatch with 30-day retention

## Cost controls

| Control | Detail |
|---|---|
| Overall account budget | US$100/month, with email alerts |
| Bedrock-only budget | Email alerts at 80% and 100%. At 120% an AWS Budgets action attaches a deny-Bedrock IAM policy to the instance role (see open finding OF-1). |
| Monthly ceiling in the app | All AI work stops at the limit and resumes in the next month |
