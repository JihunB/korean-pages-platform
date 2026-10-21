# Incident notes

Short write-ups of things that went wrong, or nearly did, and what changed afterwards.

## Monthly AI ceiling reached mid-run
**What happened:** Several long jobs (photo selection, review rewrites) stopped when spending reached the monthly ceiling.
**Response**
- Jobs stop cleanly and log their position, so nothing was half-written.
- After the owner approved a higher ceiling, a data migration re-queued exactly the runs that had stopped for that reason. It touched nothing else.

**Lesson:** a ceiling is only useful if hitting it is a non-event. Make "stopped by budget" a state the system can resume from.

## Photo job included 7,000+ listings it should have skipped
**What happened**
- A photo-import run selected franchise listings, which were meant to show brand logos only.
- The queue grew past 7,000 items and would have run for hours at real cost.

**Response**
- Stopped the job.
- Fixed the filter where jobs are queued **and** where each listing is processed.
- Added a separate logo-only job for franchise brands.

**Lesson:** apply scope filters at every stage, not just at selection.

## Template line endings corrupted by a bulk edit
**What happened:** A stream-editor replacement converted Windows (CRLF) line endings to LF in page templates whose output must match the old site byte for byte.
**Response**
- A diff-size check flagged a two-line change that showed up as a 70-line diff.
- Restored the file from the last commit and re-applied the edit with a helper that keeps each file's original line endings.

**Lesson:** check the diff size against the size of the intended change before every commit.

## Third-party API project cancelled
**What happened:** The Google Cloud project behind the Places API key was cancelled.
**Response**
- Confirmed from the code that no public page calls Google. The key is used only by admin-side jobs, and stored photos are self-hosted.
- The site kept serving normally.
- The scheduled discovery and check runs fail harmlessly until a new key is stored in Secrets Manager.

**Lesson:** keep third-party dependencies off the request path where possible, and know which features depend on which credential.
