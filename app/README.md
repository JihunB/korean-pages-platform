# Selected modules

These files are copied from the production code with account-specific identifiers replaced. They show the design but aren't a runnable app on their own: the rest of the Django project, and the original CMS's page templates, stay in the private repository.

| File | What to look at |
|---|---|
| [directory/services.py](directory/services.py) | The single write path for listings: `submit_proposal()` (confidence + verification gate), history, undo, backup-before-restore, photo upload checks |
| [directory/store_update.py](directory/store_update.py) | The background jobs that keep listings current; `is_maps_link()` / `_fetch_maps_url()` (SSRF fix F-1) |
| [directory/store_writer.py](directory/store_writer.py) | Bedrock calls with schema-constrained JSON output; `check_budget()` monthly spend ceiling |
| [directory/google_places.py](directory/google_places.py) | Places API client with retry/backoff and per-call cost tracking |
| [directory/storage.py](directory/storage.py) | Storage backends; `SAFE_FORMATS` (Pillow decoder allow-list, fix F-2) |
| [community/images.py](community/images.py) | Upload handling: size limit, decoder allow-list, re-encode to JPEG |
| [community/ad_writer.py](community/ad_writer.py) | AI ad writer, including the keep-the-advertiser's-text mode |
| [community/templatetags/community_tags.py](community/templatetags/community_tags.py) | Escape-first formatters for user text (`ad_text`, `story_html`) |
| [legacy/members.py](legacy/members.py) | Login lockout, email-code sign-up, password reset, `same_origin()` check for legacy endpoints |
| [config/settings.py](config/settings.py) | Secrets Manager loading, secure cookies, proxy TLS header |
