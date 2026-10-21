# Legacy platform risk assessment

Done before the migration, to decide what had to change and what must not be carried over.

The old site was a commercial PHP CMS on shared hosting, deployed over FTP. The host was being retired.

| # | Finding | Impact | Decision |
|---|---|---|---|
| L-1 | Member passwords hashed with an MD5-based scheme | A database leak would expose most passwords within hours | **No legacy accounts or hashes migrated.** The new site launched with zero member accounts and Django's salted PBKDF2. |
| L-2 | jQuery 1.3.2 (2009) loaded by the CMS core on every page | Known XSS issues; no upstream fixes | Retired with the CMS |
| L-3 | Diagnostic and one-off PHP scripts in the public web root | Unauthenticated code reachable by anyone who guessed a name | Not carried over. The new deployment has no file-drop path; only built bundles are deployed. |
| L-4 | FTP deployment | Credentials and code sent unencrypted | Replaced with signed API calls (IAM) to upload bundles and create versions |
| L-5 | Handoff document containing admin, database and FTP passwords | Exposure if it's shared, synced or committed | Kept out of version control; a redacted copy is kept instead. A history scan confirmed the original was never committed. |
| L-6 | Database dumps containing member personal data and password hashes | Privacy breach | `*.sql`, `.env` and local databases are git-ignored. A history scan confirmed none were ever committed. |
| L-7 | Page scripts post forms by XHR without CSRF tokens | Cross-site request forgery on login, sign-up and reviews | Those endpoints keep working for the original scripts, but reject any POST whose `Origin`/`Referer` isn't the site |
| L-8 | Single shared host for web, files and database | One compromise exposes everything | Split into CDN, load balancer, app instances and a private database, each with its own security group |

## What was deliberately kept

Page markup, wording and behaviour were reproduced exactly: users and listed businesses saw no change. Keeping the look identical let the migration be judged purely on whether anything broke.
