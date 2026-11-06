# ClauseIntel dashboard — architecture decisions

`DASHBOARD_IMPLEMENTATION_PLAN.md` is the record of the dashboard decisions. The three PNG files in `data/` supply colour and layout reference. Where a picture and that plan disagree, the plan is the one that is built. `PROJECT_PLAN.md` was not changed.

Last updated: 6 November 2026.

## Facts

### The client is one HTML file

`apps/dashboard/index.html` is the only client. `GET /` returns that file. There is no React, Vue, or Node project in this repository. Tests in `tests/test_api.py` read the HTML as text.

### Login is one company account

`tenant_configs` holds `org_id`, `name`, `login_name`, `password_hash`, and `session_version`. The token is HS256 and carries `org_id` and the session version. It lasts one hour. A newer login makes the previous token fail. There is no user table, no email column, no email verification, and no OAuth callback.

`POST /v1/register` requires `org_id`, `login_name`, and a password of at least 8 characters. `name` is optional. `POST /v1/login` accepts `login_name` and `password` and returns `token` and `org_id`.

### A vendor is a slot, not a file

`vendors` stores `vendor_id`, `org_id`, `name`, `job_id`, and `created_at`. The current contract is the job id. Two vendors in one company may hold the same file bytes as two jobs. The worker can reuse the saved report. The same bytes in the same vendor reopen that vendor's current non-failed job. A different file replaces the job id on that slot.

### A contract check is a job. A clause check is not

`POST /v1/vendors/{vendor_id}/contract` stores the file and inserts `contract.submitted`. `scripts/worker.py` claims the row and calls `run_job`. The row is `queued`, `running`, `succeeded`, or `failed`. Nothing writes a finer stage.

`POST /v1/checks/clause` runs `check_clause` in the API process and returns the report. It requires `clause_text` and `clause_type`. It does not insert a job, does not set `vendors.job_id`, and does not look for missing requirements. That last point is what `check_clause` already does.

### Progress transport that exists

The page polls `GET /v1/jobs/{job_id}`. Nginx allows a proxied request 300 seconds and a body of 25 MB. Redis stores verdicts, the policy hash, the saved report, and the company work budget. It does not store job stages. Kafka is not running. There is no WebSocket and no Server-Sent Events endpoint.

### Report text

`core/report_view.py` is what both the page and Streamlit show. The labels are Matches the policy, Does not match the policy, Not in the contract, Only partly in the contract, Conflicts with the policy, and Needs a person to review. This dashboard does not rename them.

### Tables are not all in Alembic

Policy chunks and tenant columns go through `core/pg_schema.py` and Alembic `001_policy_and_tenant.py`. `jobs` and `vendors` are created with `CREATE TABLE IF NOT EXISTS` in Python. A new vendor column follows that same path so a laptop SQLite file and Docker Postgres stay in step.

## Proposals, not yet approved

### Keep the static page

The new layout stays in `apps/dashboard/index.html`, with a style block and inline SVG. No UI framework is added.

Why: the API already serves that file, the tests read it, and the repo has no Node toolchain.

Alternative: a React app and a build step. Rejected unless you ask for it. It is a new deploy path for the same API.

### Poll the job. Do not add a progress channel

The analyzing screen uses `queued`, `running`, `succeeded`, and `failed`. It does not draw Extracting Clauses, Checking Compliance, and Generating Results as completed steps.

Why: the worker performs parse, the graph, and the missing-clause check inside one `running` claim, and it does not record each of those. A report cache hit returns during that claim without parsing. Marking those stages complete would be false.

Alternative: a `stage` column written at real boundaries (file stored, parse started, graph started, result saved). That is a worker change. It is not required for an honest screen, and it is not part of this proposal.

Alternative: WebSockets, Server-Sent Events, or Kafka. Rejected. One browser polling one job does not need them. Redis is already the cache and the budget, not a second queue.

### Lock the vendor mode in the database

Add `input_mode` (`contract` or `clause`) on `vendors`. The first successful choice sets it only while it is empty, in one transaction. The other choice then returns 409.

A clause result is stored on that vendor because the clause path has no job. `POST /v1/vendors/{vendor_id}/clause` is the route that can see the vendor id. `POST /v1/checks/clause` stays as it is, with no vendor.

Inside contract mode, the current file rules stay: same bytes reopen the current check, a different file replaces the slot, a failed job can be uploaded again.

Why: two requests can pass a disabled button. The row has to be the lock.

Alternative: hide the second card and do nothing on the server. Rejected. The request says the lock is server-side.

Not proposed: a reset that clears the mode. That deletes the current choice. It waits for a rule from you.

### Do not hardcode the picture's companies

Sidebar rows come from `GET /v1/vendors`. Acme Corporation, Globex Inc, and TechSolutions are not written into the page.

### One current policy file

The page after login is Company policy. It uploads one file and lists that one current file.

Different bytes keep today's index path: embed, bump `active_policy_version`, clear that company's caches. The previous policy object is deleted afterward.

The same bytes do not embed and do not bump the version. `process_uploaded_files` already renames the indexed sections. The bucket change is the opposite of `run_job` today: keep the new object, which carries the new file name, and delete the previous policy object. Download returns those bytes under the new name. S3 has no rename. `put` of the new key plus `delete` of the old key is the replace.

Preview shows a PDF on the company policy page. A Word file cannot be drawn there, and the page says to download it.

Streamlit's multi-file policy control is unchanged.

### Results screen is not mapped yet

`jobs.result` already holds the reader report: six counts, a sentence, and each finding's title, label, contract text, quote, review reason, policy location, and suggested revision when those exist. `GET /v1/jobs/{job_id}` returns that object and does not return `created_at` or `updated_at`. The verdict schema has no score.

The detailed section renders those finding fields. The conclusion line is `reason`. When the reader replaced the model text, the line is `review_reason`. It does not draw bars. No score is added.

Count cards use the stored summary. Compliant is `matches_the_policy` only. Not in the contract is `not_in_the_contract`. Review is `needs_a_person_to_review`. Non-compliant is `does_not_match_the_policy` plus `only_partly_in_the_contract` plus `conflicts_with_the_policy`.

The time card is `updated_at` minus `created_at`. `GET /v1/jobs/{job_id}` does not return those columns today. Adding them to that response is the change. The gap includes the wait for a worker. No new column and no new table.

Overall verdict, the compliance rate, and "No Major Issues Found" are omitted. The result has no single conclusion for the whole file.

Download Report writes a PDF when asked. `pymupdf` is already a dependency and can create a document. `reportlab` is not a dependency. It is installed only inside `tests/generate_test_pdfs.py` if someone runs that helper. The PDF uses the counts, the time, and the finding text already on the job. No model call and no new table. The three-dot menu is omitted.

A contract result is headed “Your contract has been analyzed.” A clause result is headed “Your clause has been analyzed.” The Not in the contract card is inactive only when `missing_clause_check` is `not_run`. A contract that found nothing missing still shows 0.

### Header uses the stored company name

Login and register return `name` from `tenant_configs`. The header and the avatar letter use it. No new column.

### Input mode, decided

The first successful result sets `input_mode`. Check Another can start a contract or a clause after that. The other choice does not become the report until its result is stored. A failed first contract can be uploaded again. A first clause that fails before a report is stored clears `input_mode`.

### The report is the job row

`jobs.result` holds the report JSON. `job_id` is the id the page uses to open it. A clause check writes that same kind of row, because a clause has no job today. A new `reports` table is not added.

`vendors.job_id` is the successful report. `vendors.pending_job_id` is the replacement still running. The worker, which already runs off the request, moves `job_id` and deletes the old job only after the new contract result is stored. A clause swap happens when `check_clause` has returned that result. Kafka is not started for this. The parent plan keeps Kafka for later, and that later consumer would perform this same swap.

### Controls that are drawn and do nothing

Google, LinkedIn, Settings, Help, the bell, and the avatar are visible. They do not authenticate, open a page, or invent a notification. The bell has no unread dot. Upgrade to Pro is omitted and stays on the future list. Ask for a job is omitted from the page. `GET /v1/jobs/{job_id}` stays.

### Do not remove working routes

`POST /v1/policies`, `POST /v1/checks`, and `GET /v1/jobs/{job_id}` stay. The page no longer shows Ask for a job.

### Social login and Upgrade to Pro are not implemented as working features

There is no OAuth client and no billing route. Buttons that call neither are the most the pictures can have until those exist. Showing them as if they signed the user in, or as if they upgraded a plan, is rejected.

### Company work budget stays where it is

A contract upload and a clause check already call `require_budget`. Listing vendors and polling a job do not. The dashboard does not add a second limit.

## New dependencies

None proposed.

## Existing pieces the dashboard reuses

- `GET /` and `apps/dashboard/index.html`
- `POST /v1/register`, `POST /v1/login`
- `POST /v1/vendors`, `GET /v1/vendors`, `POST /v1/vendors/{vendor_id}/contract`
- `GET /v1/jobs/{job_id}`
- `check_clause` for the text of one clause
- `for_the_reader_report` for the words on the finished report
- `POST /v1/policies` if the policy form stays on the page
