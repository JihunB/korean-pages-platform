# Guardrails for AI-maintained content

Listings are kept current by background jobs. They combine Google Places data with Claude models on Amazon Bedrock to find new businesses, check existing ones (hours, closures, renamed or moved), write descriptions and pick photos. These jobs publish to a public site, so they are treated as an untrusted writer.

## 1. Proposals, not writes
Jobs never write a listing directly. They call `services.submit_proposal(...)` ([app/directory/services.py](../app/directory/services.py)), which applies the change immediately only when **both** are true:

- the model's confidence is at or above `AI_AUTO_APPLY_CONFIDENCE`, and
- the fact is verified against Google Places.

Closing a business is stricter still: it applies only when Google reports `CLOSED_PERMANENTLY`. Anything else waits in the admin's review queue, with a preview of how the page would look.

## 2. One write path, full history
Every listing change, whether from the admin, an AI job, an import or a script, goes through the same service layer. That layer:
- records who or what made the change and the before/after values
- keeps category counts consistent
- makes any change reversible with one click

## 3. Structured output only
The models answer in JSON that must match a fixed schema (`store_writer.StoreWriter`). They have no tools and can't trigger actions. Text from scraped pages can change what a model *says*, but not what the system *does*, and a low-confidence answer goes to review.

## 4. Spend ceiling
`store_writer.check_budget()` adds up each month's model and image costs. It stops all AI work before a call would exceed `CLAUDE_MONTHLY_BUDGET_USD`. Stopped runs record where they were and resume when the month resets or the limit is raised. AWS Budgets adds email alerts and an IAM deny action as a second, independent layer.

## 5. Respect for sources
Importers obey `robots.txt` and skip members-only boards. One requested source was left out entirely because its `robots.txt` forbids crawling.

## 6. Human-written text stays human
For ads, the admin can choose *keep the text, only make the picture*. The writer then produces only an image prompt, and the advertiser's wording is published unchanged.
