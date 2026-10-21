# Runbook: deploying a release

## Before
1. Run the release gate:
   ```bash
   python tools/admin_smoke_test.py      # 61 checks: admin actions, data changes, backup/restore round trip
   ```
2. Check desktop and mobile pages in a browser (`?mobile=on`).
3. Commit. Every release is built from a commit.

## Build and upload
```bash
python tools/build_bundle.py                                   # → dist/koreanpages-<timestamp>.zip
aws s3 cp dist/<version>.zip s3://<eb-bucket>/koreanpages/<version>.zip
aws elasticbeanstalk create-application-version \
    --application-name koreanpages --version-label <version> \
    --source-bundle S3Bucket=<eb-bucket>,S3Key=koreanpages/<version>.zip --process
```
Wait for the version status to reach `PROCESSED`.

## Deploy
```bash
aws elasticbeanstalk update-environment \
    --environment-name <env> --version-label <version>
```
- The deployment policy is **rolling with an additional batch**: a new instance comes up before an old one is removed, so capacity never drops.
- Migrations run once, on the leader instance (`.ebextensions/01_django.config`).

## Verify
- Wait for `Ready` / `Green`. A short `Red`/`Degraded` right after a deploy is normal while the old instance is removed or the second instance starts.
- Load the changed pages on the live domain.

## Roll back
Redeploy the previous version label with the same `update-environment` command. Versions stay in the application until deleted.
