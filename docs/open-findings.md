# Open findings

Known gaps, in priority order. These are tracked openly rather than left out.

| ID | Finding | Severity | Plan |
|---|---|---|---|
| OF-1 | The AWS Budgets action meant to attach a deny-Bedrock policy at 120% of budget fails (`EXECUTION_FAILURE`): its execution role lacks `iam:AttachRolePolicy`. The in-app ceiling still stops spending. | Medium | Grant `iam:AttachRolePolicy`/`DetachRolePolicy`, limited by condition to that one policy and role; then test the action |
| OF-2 | No `Strict-Transport-Security` header | Medium | CloudFront response-headers policy with HSTS (1 year, `includeSubDomains`), then preload |
| OF-3 | No Content-Security-Policy. The legacy page templates rely on inline scripts. | Medium | Ship `Content-Security-Policy-Report-Only` first, inventory inline scripts, move to nonces |
| OF-4 | No AWS WAF in front of CloudFront; no rate limits on login or search | Medium | WAF with the AWS managed core and SQLi rule sets, plus rate-based rules on `/happy_member_login.php` and search |
| OF-5 | Admin login has no second factor | Medium | TOTP MFA for staff accounts |
| OF-6 | App instances run in public subnets with public IPs (no inbound rules on the app security group) | Low | Move instances to private subnets with VPC endpoints for S3, Secrets Manager and Bedrock |
| OF-7 | Settings fall back to development values when a variable is missing: `DJANGO_DEBUG` defaults to on, and `SECRET_KEY` to a development key (production sets both; the key comes from Secrets Manager) | Low | Fail closed: default debug off, and refuse to start without a secret key outside local development |
| OF-8 | Dependency updates: Pillow 12.3 → 13.x (decoder CVEs, already mitigated by F-2); Django 6.0.7 → 6.0.8 (GeoDjango CVEs, not used); cryptography 48 → 50 (PKCS#7 issues, not used) | Low | Upgrade in one release behind the smoke test |
| OF-9 | SES still in sandbox mode | Low (availability) | Request production access |
| OF-10 | No CloudTrail trail: only the default 90-day event history, so there's no durable audit log of API activity | Medium | Multi-region trail to a locked-down S3 bucket with log-file validation |
| OF-11 | GuardDuty, Security Hub and AWS Config are not enabled | Medium | Enable GuardDuty, plus Security Hub with the AWS Foundational Security Best Practices standard |
| OF-12 | The Bedrock budget's service filter doesn't include Stable Image Core, so image spend isn't covered by its alerts (the in-app ceiling does count it) | Low | Add the service to the budget filter |
| OF-13 | The `Project` cost-allocation tag isn't activated, so spend can't be split by tag in Cost Explorer | Low | Activate the tag in Billing |
