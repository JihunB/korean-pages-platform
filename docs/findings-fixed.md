# Findings fixed during the pre-publication review

Before publishing this repository I ran the same checks the CI now runs: a secret scan, bandit and pip-audit. I then reviewed every hit by hand. Two real issues came out of it, and both were fixed and deployed to production the same day.

## F-1: SSRF in the admin "add stores from Google Maps links" importer
**Severity:** Low (staff-only feature; IMDSv1 already disabled)
**Found by:** bandit B310 (`urlopen` with a caller-supplied URL), confirmed by manual review

**Problem**
- The admin form accepted any line that *contained* `google.com/maps`, so `https://attacker.example/google.com/maps` passed.
- The server then fetched the URL and followed every redirect.
- A malicious or mistaken paste could make the server request arbitrary hosts, including internal addresses.

**Fix:** [app/directory/store_update.py](../app/directory/store_update.py), `is_maps_link()` and `_fetch_maps_url()`
- the URL's actual hostname must be a Google or `goo.gl` host
- the scheme must be `http` or `https`
- a custom redirect handler checks the same rule on **every hop** and aborts otherwise

**Verified**

| URL | Result |
|---|---|
| `https://maps.app.goo.gl/…` | allowed |
| `https://www.google.com/maps/place/…` | allowed |
| `https://www.google.com.au/maps?q=…` | allowed |
| `https://attacker.example/google.com/maps` | rejected |
| `https://google.com.evil.io/maps` | rejected |
| `http://169.254.169.254/latest/meta-data/` | rejected |
| `file:///etc/passwd` | rejected |

## F-2: Image uploads reached every Pillow decoder
**Severity:** Medium (member-facing upload; Pillow 12.3 has 8 open CVEs in its TIFF, EPS, GIF and PPM decoders)
**Found by:** pip-audit, then tracing where uploads are decoded

**Problem**
- Uploads were checked by **file extension** only.
- Pillow picks its decoder from the file's **content**, so a TIFF or EPS file renamed to `.jpg` went straight to the vulnerable decoders.

**Fix**
- every `Image.open()` now passes `formats=SAFE_FORMATS` (`JPEG`, `PNG`, `GIF`, `WEBP`), defined once in [app/directory/storage.py](../app/directory/storage.py)
- applies to member uploads, admin uploads, thumbnails, brand logos and Google photos

**Verified:** genuine JPEG, PNG, WebP and GIF uploads still save. TIFF and BMP content renamed to `.jpg` is rejected with the normal "can't read this photo" message.

**Follow-up:** upgrading Pillow to 13.x is tracked as OF-8 in [open-findings.md](open-findings.md).

## Secret scan
I scanned the full history of the production repository for AWS keys, Google API keys, private keys, Slack and GitHub tokens and hard-coded passwords. Nothing was found. No database dumps, `.env` files or the original credentials document were ever committed.
