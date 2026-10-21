# Threat model

## Assets
1. **Listing data:** 11,000+ businesses: names, phones, addresses, hours, photos
2. **Member accounts:** email addresses and password hashes
3. **Admin account:** full control of the content
4. **Cloud credentials and API keys:** AWS role, Bedrock, Google Places, SES
5. **Spend:** pay-per-use AI and API services
6. **Availability** of a public site that businesses rely on

## Trust boundaries
```
Internet ─► CloudFront ─► ALB ─► app instances ─► RDS / S3 / Secrets Manager
                                   │
                                   ├─► Amazon Bedrock (Claude, Stability)
                                   ├─► Google Places API
                                   └─► third-party job boards and blogs (untrusted content)
```
Content scraped from job boards and blogs is untrusted. It reaches the AI models as input data, and their output reaches public pages.

## STRIDE

| Category | Threat | Mitigation | Status |
|---|---|---|---|
| Spoofing | Password guessing on member or admin login | Lockout after 5 failures; email-code sign-up; one-time admin setup link | Partly mitigated; admin MFA open (OF-5) |
| Spoofing | Forged cross-site posts to legacy endpoints | `Origin`/`Referer` must match the site; Django CSRF tokens everywhere else | Mitigated |
| Tampering | Wrong or malicious AI edits to listings | Confidence threshold **and** external verification before auto-apply; review queue; history and undo | Mitigated |
| Tampering | Prompt injection from scraped pages | Schema-only JSON output; the models can't call tools or trigger actions; low-confidence changes go to review | Mitigated by design |
| Tampering | Malicious image upload targeting decoder bugs | Pillow restricted to JPEG/PNG/GIF/WebP decoders; re-encoded as JPEG on save | Mitigated (F-2) |
| Repudiation | Unattributed data changes | Every change stored with its source (admin, AI job, import) and before/after values | Mitigated |
| Information disclosure | Credential leak from code or history | Secrets Manager; instance role; gitleaks in CI; history scan | Mitigated |
| Information disclosure | SSRF to internal addresses via admin link import | Host allow-list checked on the first request and every redirect; IMDSv1 disabled | Mitigated (F-1) |
| Information disclosure | Database exposure | Not publicly accessible; encrypted; reachable from the app security group only | Mitigated |
| Denial of service | Request floods against search or login | CloudFront absorbs volume; origin only reachable through CloudFront | Partly mitigated; WAF and rate limits open (OF-4) |
| Denial of wallet | Runaway AI or API usage | In-app monthly ceiling; Budgets alerts; Budgets deny action | Partly mitigated; deny action open (OF-1) |
| Elevation of privilege | XSS through user content (ads, reviews, jobs) | Escape-first formatters; links only for `http(s)` and digit-only `tel:` | Mitigated; CSP open (OF-3) |
