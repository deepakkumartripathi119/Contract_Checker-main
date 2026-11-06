# ClauseIntel dashboard — implementation plan

This file is the roadmap for the dashboard only. It does not replace `PROJECT_PLAN.md`. That parent plan was not edited for this document.

No application code has been changed for this roadmap. Implementation waits until this plan is approved.

Last updated: 6 November 2026.

## Scope

Replace the look and the flow of `apps/dashboard/index.html` so a signed-in company can:

- see its own vendors in a sidebar and open one vendor workspace
- upload a PDF or `.docx` into that vendor, or submit one clause, but not both
- watch the check using the status the API already returns
- sign in and register with the fields the API already accepts

The compliance engine, the worker, the job table, Redis, the bucket, and Streamlit stay. Streamlit remains the developer screen.

Out of this dashboard until a later decision:

- Kafka
- a paid cloud account
- a working Google or LinkedIn login (the buttons are drawn and do not sign anyone in)
- Upgrade to Pro (the card is omitted; it stays on the future list)
- a follow-up question after a report (no route stores a question or an answer)
- a menu of several companies on one login
- pages behind Settings, Help, and the bell (those controls are drawn and do nothing)
- renaming the finding text. That text keeps the six labels from `core/report_view.py`. The count cards use the titles in the results section of this file.

## Pictures

Three files in `data/` are the design reference for colour, type, spacing, corner radius, the navy sidebar, and the light workspace:

- `data/ClauseIntel Contract Analysis Dashboard.png`
- `data/ClauseIntel Responsive UI Mockups.png`
- `data/ClauseIntel Contract Review Dashboard.png`

This plan is the record of what we decided. A label, a button, a count, or a flow in those files is not a requirement when this plan says otherwise. Names such as Acme Corporation, Globex Inc, and TechSolutions are picture text. They are not written into the page.

Visual match is one page at a time. The picture decides the look of that page. This plan decides what the page is allowed to do.

- [x] Sign in. Done 6 November 2026. Welcome Back card, measured waves (`#f7f6fe`, `#e4e4fd`, `#dadafd`), button `#5653fa`, 46-pixel fields, 16-pixel field icons. The eye toggles the password and switches between the open eye and the crossed eye. Google is the four-color G at 20 pixels. LinkedIn is the blue mark at 20 pixels. Those buttons do not sign anyone in.
- [x] Register Your Company. Done 6 November 2026. Same card, waves, and 46-pixel fields as sign-in. Icons are 16 pixels. The picture has no password field. Password stays, with the same eye toggle, because sign-in needs it. Industry is typed because the picture lists no industries. Company size is a custom menu of the six employee bands, not the browser list.
- [ ] Company policy. This is the page after sign-in. The pictures do not draw a separate full page. The only Company Policy art is the card on the analyzing screen in `data/ClauseIntel Contract Review Dashboard.png`: shield, the title, a green Uploaded mark, and Download Policy. The page uses that card once a file exists, and adds the file name, Preview, and the upload. Preview shows a PDF on the page. A Word file says to download it. When a file exists, the upload says that a new policy deletes the current one. An empty company does not show Uploaded.
- [x] Sidebar. Fixed to the screen. The slide control sits in the sidebar header. Collapsing leaves that control and the hexagon logo in a narrow rail, and hides the word. The mark is the purple hexagon with white scales. The word is Clause in white and Intel in `#8b8ef2`. Vendor rows use an office-building icon; the selected row puts that icon on a `#6a5cff` tile. The Vendors funnel opens a name search, and choosing a match opens that vendor. Footer labels are Company policy, Settings, and Help & Support. Upgrade to Pro stays off.
- [ ] Vendor workspace. Seen only after a vendor is created and that vendor is opened. There is no Vendors tab. An opened vendor with no report uses the welcome picture in `data/ClauseIntel Contract Analysis Dashboard.png`: the illustration, “Welcome to ClauseIntel!”, Upload Contract, Add Single Clause, and Type Clause. The picture’s line “Add a vendor from the sidebar to get started with your contract analysis.” is not shown. Upload Contract picks a file for that vendor. Type Clause opens the heading and clause dialog. It does not open Add Vendor.
- [ ] Analyzing.
- [ ] Results.

Delete vendor removes the vendor row in that request. The page says “Deleted.” and the name is gone. The same request writes a `vendor.deleted` job and marks that vendor’s jobs `deleting`, so a check cannot read them while the file erase is still queued. Delete report does the same with `report.deleted` and leaves the vendor. Those database writes commit together. A crash before the commit leaves the vendor and its jobs unchanged. A contract upload commits the file row, the job, and the vendor link together. A finished replacement commits the report and the vendor move together. A policy publish commits the new index and the policy version together. A crash before any of those commits leaves the previous rows in place. The worker erases the files. If that worker dies after taking the delete, the row stays `running` and the next claim, once the lease has passed, runs the erase again. A missing file is ignored, so the second run can finish. A later Kafka consumer can read those same kind names.

Drawn in the files and not built, because this plan says so:

- Upgrade to Pro
- a working Google or LinkedIn login
- six processing stages marked complete
- bars, an overall verdict, a compliance rate, and "No Major Issues Found"
- Clause Details, Suggestions, and Ask a follow-up question
- All Contracts, a search box, chat counts, and Upload New Contract as the primary action
- a company menu, a notification, and pages for Settings or Help
- Ask for a job

## What the repository is

One Python 3.12 application. There is no Node app, no React tree, and no component library.

| Piece | Where it lives | What it does |
| --- | --- | --- |
| Client page | `apps/dashboard/index.html` | One HTML file. The API serves it at `GET /`. |
| HTTP API | `services/api.py` | FastAPI. Company id comes from the bearer token. |
| Login | `services/company_login.py` | One username and one password on the company row. PBKDF2. HS256 JWT, one hour, `org_id` and session version. |
| Company row | `tenant_configs` | `org_id`, `name`, `business_email`, `industry`, `company_size`, `headquarters`, `website`, `login_name`, `password_hash`, `session_version`, policy version, silence rule. |
| Vendors | `core/vendors.py` | `vendor_id`, `org_id`, `name`, `job_id`, `created_at`. Created on first use, not by Alembic. |
| Jobs | `core/jobs.py` | Queue and status. Kinds `policy.uploaded` and `contract.submitted`. Statuses `queued`, `running`, `succeeded`, `failed`. |
| Worker | `scripts/worker.py` | Claims one job, calls `run_job`, marks succeeded or failed. No HTTP port. No stage column. |
| Contract check | `services/contract_check.py` | Stores the file, writes the job, or runs one pasted clause in the request. |
| Checker | `pipeline/graph.py`, `pipeline/subgraph.py` | LangGraph. Contract path can extract clauses, check them, then look for missing requirements. One clause uses the subgraph only. |
| Report words | `core/report_view.py` | Matches the policy, Does not match the policy, Not in the contract, Only partly in the contract, Conflicts with the policy, Needs a person to review. |
| Cache | Redis in Docker, SQLite on a laptop | Clause verdicts, policy hash, saved report. Not a job-progress channel. |
| Files | LocalStack bucket `contract-checker` in Docker | API and workers. 25 MB body limit on nginx. The API checks the extension, not the file bytes. |
| Edge | nginx on port 8080 | Forwards to the API copies. Polls are ordinary `GET` requests. |

Alembic revision `001_policy_and_tenant.py` creates policy chunks and tenant columns. `jobs` and `vendors` are created by the Python that first uses them.

## Routes that exist

| Route | Auth | What it does |
| --- | --- | --- |
| `GET /` | no | The HTML page. |
| `GET /health` | no | `{"status":"ok"}`. |
| `POST /v1/register` | no | Stores the company profile on `tenant_configs`. Password is required. Company name and business email can stand in for `org_id` and `login_name`. Returns `token`, `org_id`, and `name`. |
| `POST /v1/login` | no | Body: `login_name` (username or business email) and `password`. Returns `token`, `org_id`, and `name`. |
| `GET /v1/company` | company | The stored name, email, industry, size, headquarters, and website. No password hash. |
| `PATCH /v1/company` | company | Body: current password, and a new email or a new password. A new password returns a new token. Registration fields are not accepted. |
| `POST /v1/policies` | company, and the Redis budget | Stores PDF or `.docx` files and queues `policy.uploaded`. |
| `POST /v1/checks` | company, and the budget | Stores a contract and queues a job that is not attached to a vendor. |
| `POST /v1/vendors` | company | Body: `name`. Creates an empty slot. |
| `GET /v1/vendors` | company | Newest first. Each item has `vendor_id`, `name`, `job_id`, `filename`, `status`, `created_at`. |
| `POST /v1/vendors/{vendor_id}/contract` | company, and the budget | Puts a PDF or `.docx` in that slot. |
| `GET /v1/jobs` | company | Every `contract.submitted` job for the company. Not used by the sidebar. |
| `GET /v1/jobs/{job_id}` | company | Status, file name, error, and `result` when finished. No stage field. `created_at` is not in this payload. |
| `POST /v1/checks/clause` | company, and the budget | Body: `clause_text` and `clause_type`, both required. Runs in the request. Returns the report. Does not write a job and does not touch a vendor. |

There is no logout route. The page drops the token from `sessionStorage`. The previous token dies when someone logs in again, because login increments `session_version`.

There is no WebSocket, no Server-Sent Events, and no Kafka consumer. The page polls `GET /v1/jobs/{job_id}` about every 2 seconds, up to about five minutes.

`GET /v1/vendors` and `GET /v1/jobs/{job_id}` do not spend the company work budget. Uploads and the pasted clause do. The budget runs only when `CACHE_BACKEND=redis`.

## How a vendor works today

A vendor is a name plus one current `job_id`.

- The same file bytes uploaded again into that vendor, while the current job is not `failed`, return that job with `existing: true`. No second row.
- The same bytes uploaded into a different vendor create a second job. The worker can reuse the saved report. The second vendor does not open the first vendor's job.
- A different file replaces `job_id` on that vendor. The older job remains in `jobs`. The slot shows the new file.
- A `failed` current job can be uploaded again.
- The sidebar must not show the file name. The API still returns `filename` and `status`. The workspace can use them. The sidebar shows `name` only.

Nothing on the vendor row records whether the slot is a contract or a pasted clause. A pasted clause cannot be locked to a vendor with the current table.

## What the pictures show, mapped to this code

The sample names Acme Corporation, Globex Inc, and TechSolutions are picture text. They are not data. The sidebar lists vendors returned by `GET /v1/vendors`.

| Picture | Already true | Not true, and not invented here |
| --- | --- | --- |
| Welcome when no vendor is selected | The page can do this with the vendor list it already loads. | |
| Add Vendor | `POST /v1/vendors` with a name. | |
| Vendor names in the sidebar, no file names, no chat counts | The list has `name`. The page can hide `filename`. | |
| Upload PDF or `.docx` | Extension check exists. Drag-and-drop does not. | The API does not inspect the file magic. Nginx allows 25 MB. |
| Add a single clause | `POST /v1/checks/clause` checks one clause and does not look for missing requirements. | It requires `clause_type` as well as `clause_text`. It is not stored on the vendor. |
| One mode per vendor, enforced by the server | | No column and no transaction do this. Hiding a button is not enough. |
| Analyzing, with six stages | A contract job is `queued`, then `running`, then `succeeded` or `failed`. | The worker does not write those six stages. A cache hit finishes inside `running` with no parse. |
| Refresh and see the same check | `GET /v1/jobs/{job_id}` and the vendor's `job_id` survive a refresh. | A clause check does not, because it is not stored. |
| Sign in with username or email | `POST /v1/login` matches `login_name`. A new company stores the business email there. | |
| Continue with Google, Continue with LinkedIn | | No OAuth code, no client id, no callback. |
| Register: company name, business email, industry, size, headquarters, website | Those values are columns on `tenant_configs`. The business email is the login. `org_id` is derived from the name. A password is also required. | The picture does not list industry choices, so industry is typed. Company size is one of the six employee bands. |
| Upgrade to Pro | | No product and no route. An earlier decision was to leave it off. |
| Theme | The browser stores `theme` as `light` or `dark`. The sun icon switches to the moon icon. No server column. | The bell still has no notification. |
| Avatar | Opens the company profile. `GET /v1/company` reads the stored row. `PATCH /v1/company` changes the sign-in email and the password after the current password matches. Name, industry, size, headquarters, and website stay as registered. | A wrong current password does not change the row. |
| Company menu | The header shows `name` from login and register. | One token is one company. There is no menu of other companies. |
| Policy upload | `POST /v1/policies` stores PDF or `.docx` files and queues `policy.uploaded`. The worker indexes them. | No route lists the current policy file or returns its bytes. Preview and download need that route. See the decisions and the remaining questions. |

## Phase breakdown

The order below is the one in the request, with two changes.

Phase 4 does not add a new progress channel. The contract check already has a durable job. The page will poll it. Stage checkmarks are not drawn until the worker records a real boundary.

Phase 5 does not add the picture's extra register fields or a working social login. Those are absent from the company row.

### Phase 0 — Audit. Written. Waiting for approval

This document, `DASHBOARD_PROGRESS.md`, and `DASHBOARD_ARCHITECTURE_DECISIONS.md`.

Acceptance: the routes and tables above match the code. No application file has been edited for the dashboard.

### Phase 1 — Shell

`apps/dashboard/index.html` only, still served by `GET /`.

- Navy sidebar, light workspace, the ClauseIntel name, Add Vendor, a vendor list region, Company policy, Settings, and Help.
- Upgrade to Pro is not drawn.
- Top bar: company name, a theme control, a bell, an avatar, and Log out. Settings, Help, and the bell do not open a page. The bell has no red dot, because no notification exists. The theme control switches the page between light and dark and changes its own icon. The choice is stored in the browser.
- The avatar shows the first letter of the company name. It opens the company profile. The profile heading sits beside the building mark. Registered details are tiles. Only the sign-in email and password are fields. Log out is a red-tinted icon. Check another is a rounded tinted button with a refresh icon. The filter menu is a small translucent panel. In dark mode, finding quotes use a light tint of their status colour, sign-in autofill stays on the dark field, and Google and LinkedIn use that same tint.
- After login the first page is Company policy, not the vendor welcome.
- There is no Vendors tab. The welcome picture is the opened vendor, before that vendor has a report.
- Desktop, laptop, tablet, and phone layout for the shell. A header button slides the sidebar closed and open. Under 1024 px the same button opens it as a drawer.
- No new package. Icons are inline SVG.
- Ask for a job is not on the page. `GET /v1/jobs/{job_id}` stays for logs and the inspector. `tests.test_api` today asserts the label `Ask for a job`; that assertion changes when the label is removed.

Acceptance: a signed-out visitor sees the new sign-in and register shells. A signed-in company sees the shell. `tests.test_api` is updated for the labels that moved. No new API in this phase.

### Phase 2 — Vendors

- Add Vendor calls `POST /v1/vendors`.
- The sidebar loads `GET /v1/vendors` and shows names only.
- Selecting a vendor opens its workspace.
- Empty company: the welcome text tells them to add a vendor.
- Empty vendor: no file and no clause yet.
- Another company's vendors are not returned. This is already true of `GET /v1/vendors`. The test stays.

Acceptance: create, list, and select. No file name in the sidebar. Refresh keeps the selection when `vendor_id` is stored in the page.

### Company policy page

This is the page shown immediately after login. Sidebar label: Company policy. One company has one current policy file.

- The page uploads one PDF or one `.docx` through `POST /v1/policies`.
- It lists that one current file: the name, Preview, and Download.
- When a policy is already stored, the upload area says that uploading a new policy deletes the current one.
- Preview shows each page of a PDF inside the policy card. The card scrolls. Its bar has the page number, previous and next page, search with the match count, and zoom. `GET /v1/policies/preview` returns the page count and each word’s place on the page. `GET /v1/policies/preview/{page}` returns that page as a PNG. A Word file cannot be shown there, and the page says to download it.
- Download returns the current file bytes under the current file name.
- While a replacement is queued or running, the page shows that work and keeps the current file until the job succeeds. A failed job leaves the current file in place.

Backend, which the page needs:

- A company-scoped read of the one current policy file, and a company-scoped download of its bytes. Another company cannot read it.
- Different bytes follow the worker that already exists: embed, bump `active_policy_version`, and clear that company's verdicts and reports. The previous policy object is then deleted, so the bucket and the list hold the new file only.
- The same bytes do not embed, do not bump the version, and do not clear caches. The indexed sections take the new file name. The new object, which already uses that name, is the one kept. The previous policy object is deleted. Download then returns those same bytes under the new name.
- Today the same-bytes path does the opposite with the bucket: it deletes the new object and leaves the old key. That delete is reversed. The index rename stays.
- Streamlit can still send more than one policy PDF. That developer screen is unchanged. The company page sends one file.

There is no list or download route today. `stored_documents` keeps every upload until this discard rule runs. The object key is `{org_id}/policies/{document_id}/{filename}`. The bucket has no rename call. Keeping the new object and deleting the old one is the replace.

### Phase 3 — Contract or one clause

This phase needs the backend lock described below. It is not a button-only change.

Frontend:

- Until a mode is saved, the opened vendor shows the welcome picture. Upload Contract opens the file picker for that vendor. A file can also be dropped on that card. Type Clause opens the dialog. Neither button opens Add Vendor.
- The dialog title is Add Single Clause. It asks for a heading and the clause text, then sends them. The heading is one line, 255 characters. The clause text is 10,000 characters. Both show a counter, and the counter is marked when the limit is reached. The API rejects a value past that limit. There is no published legal standard for these lengths. 255 is the usual maximum for a single-line name. 10,000 is a usual ceiling for one text field that is a clause rather than a whole contract. The whole contract stays on the file upload.
- The first successful result locks the mode. Check Another, below, is how a later contract or clause replaces it.
- A failed first contract can be uploaded again. A first clause that fails before a report is stored clears the mode.
- Errors from the API are shown. A 429 from the company budget is shown.

Backend, proposed:

- Add `input_mode` on `vendors`: empty, `contract`, or `clause`.
- Set it in the same database transaction that accepts the first submission, and only if it is still empty. Two overlapping requests cannot both win.
- `POST /v1/vendors/{vendor_id}/contract` keeps today's file rules inside `contract` mode: same bytes reopen the current non-failed job; a different file replaces the slot; a failed job can be uploaded again.
- A new `POST /v1/vendors/{vendor_id}/clause` takes `clause_text` and `clause_type` and calls the existing `check_clause`. The report is stored on a `jobs` row, the same place a contract report is stored. `jobs.result` is that JSON. `job_id` is the id used to open it again.
- `GET /v1/vendors` gains `input_mode`, `job_id`, and `pending_job_id`. It does not gain another company's rows.

The clause check stays inside the HTTP request, which is how `/v1/checks/clause` works today. Nginx allows that request 300 seconds. The report is stored so a refresh can show it.

The first result stays the vendor's report until a replacement succeeds. Check Another is that replacement. It is not a second vendor.

Acceptance: tests cover two vendors with the same file, a repeat upload, a replacement file, a clause rejected after a contract, a contract rejected after a clause, and two overlapping first submissions where only one mode is stored.

### Phase 4 — Progress that the backend actually has

Contract:

- After upload, the workspace polls `GET /v1/jobs/{job_id}`.
- `queued` and `running` show the analyzing page. A contract job writes `step` as the work starts: `extracting` while the file is read, `analysing` when clause checks start, `matching` when the missing-requirement check starts, and `completed` when the job succeeds. `GET /v1/jobs/{job_id}` returns that `step`. The page marks a step done only after a later step has been received. A saved-report hit does not write extracting, analysing, or matching. The page still says the check can take a few minutes and the tab should stay open.
- `succeeded` shows the report already produced by `core/report_view.py`.
- `failed` shows `error`. Uploading the file again is allowed, because a failed job can already be replaced.
- Refresh reads `job_id` from the vendor and continues the poll.
- The job id is not printed. The page has no Ask for a job box.

The picture's six stages are not shown. The page shows the four steps the contract check writes. A report-cache hit never parses or calls the model, so it does not write extracting, analysing, or matching.

Clause:

- While `POST /v1/vendors/{vendor_id}/clause` is in flight, the workspace shows one analyzing state.
- When a contract or clause result arrives, the page first shows Completed with a moving green circle, fades that state out, and then shows the report. Opening a report that is already stored skips that animation.
- Refresh shows the stored report.
- If the request fails before a report is stored, the mode is cleared and the error is shown. The vendor can start again.

No Kafka, Redis progress channel, WebSocket, or Server-Sent Events.

Acceptance: a finished job renders the report. A queued job does not render empty findings as a finished report. A refresh during `running` resumes the poll.

### Results screen

The reference is the "Analysis Complete" layout: desktop, a narrower desktop, and a phone. Upgrade to Pro is still drawn there. It stays omitted.

What `GET /v1/jobs/{job_id}` already returns inside `result`, from `core/report_view.py`:

- `document_name`
- `summary.total`
- `summary.matches_the_policy`
- `summary.does_not_match_the_policy`
- `summary.not_in_the_contract`
- `summary.only_partly_in_the_contract`
- `summary.conflicts_with_the_policy`
- `summary.needs_a_person_to_review`
- `summary.sentence` stays on the result. The dashboard does not print it, because the count cards already show those numbers.
- Each item in `present_clauses` and `missing_clauses` has `clause_title`, `reader_label`, `clause_text`, `exact_policy_quote`, `review_reason`, `clauses_internal`, and `suggested_revision` when those exist

The six labels are Matches the policy, Does not match the policy, Not in the contract, Only partly in the contract, Conflicts with the policy, and Needs a person to review. A single clause check has no `missing_clauses`. The same result object is used.

Decided on 6 November 2026:

- The detailed section does not use bars, scores, or the picture's Clause Breakdown, Flagged Clauses, and Summary tabs. Each finding shows the title, `reader_label`, the contract text, the policy quote, the policy location, and a suggested revision when those values are present. The conclusion is the finding's `reason` from the model. When the reader replaced that text, the sentence shown is `review_reason`.
- Count cards, from `summary`, with no new table:
  - Total is `summary.total`.
  - Compliant is `matches_the_policy` only.
  - Not in the contract is `not_in_the_contract`.
  - Review is `needs_a_person_to_review`.
  - Non-compliant is `does_not_match_the_policy` plus `only_partly_in_the_contract` plus `conflicts_with_the_policy`. Those three are already `non_compliant` on the finding, and they are not the "Not in the contract" label.
- The time card is `updated_at` minus `created_at`. Those columns exist on `jobs` and are not in `GET /v1/jobs/{job_id}` today, so that response gains them. The gap includes time waiting for a worker. No new column.
- Overall verdict, the compliance rate, and "No Major Issues Found" are not on the result. Those cards are omitted. The conclusion the person reads is the text on each finding.
- A score is not added. `ClauseComplianceResult` has none.

Download Report builds a PDF from that same result. It does not call the model. `pymupdf` is already installed for reading contracts, and it can write a PDF. The file is made when the person asks for it. It is not stored in a table. On a finished contract or clause result the control is a download icon beside Check another. Hovering it says “Download the report as a PDF.” Delete report is the red trash icon beside that. The three-dot menu in the picture has no items, so it is omitted.

The five count cards keep the titles above. Each card uses its own colour: Total indigo, Compliant green, Not in the contract amber, Review orange, Non-compliant red. The findings are one continuous list. Each row has a coloured symbol and the stored text. They are not separate white cards. The same words are shown: title, `reader_label`, contract text, reason, review reason, policy quote, policy location, and suggested revision, when those values exist.

The page does not print “Your contract has been analyzed.” or “Your clause has been analyzed.” On a clause result, `missing_clause_check` is `not_run`. The Not in the contract card is visible and inactive. A contract whose count is zero still shows 0, because that check did run. The filter icon sits at the right end of the result. Opening it shows Compliant, Not in the contract, Review, and Non-compliant, each with that card’s count. There is no Total row and no checkbox. A chosen name collects on the left as a chip. The chip’s × removes that choice. With none chosen, every finding is shown. The cards themselves do not change. Check another is shown on the result and hidden on the upload screen. Success and error text appears in a card at the top right for 6 seconds.

Check Another does not add a vendor. Add Vendor stays the sidebar button.

The button clears the results view and shows the upload card and the clause card again. The page says that a new contract or a new clause removes this report after the new one is ready, and that the person should download it first. One row remains on that screen: the current report. Opening the row shows that same report again.

`vendors.job_id` keeps pointing at that report. The new work is `vendors.pending_job_id`. The old `jobs` row is deleted only after the new row is `succeeded` and its `result` is stored. A failed new job leaves the old report in place. The same file bytes in that vendor still reopen the current check and do not delete it.

This does not add a report table. `jobs.result` already stores the report JSON, and `job_id` is the id that opens it. A second table would copy that JSON. The new column is `pending_job_id` on `vendors`.

The existing worker does the swap when the new contract job succeeds. Kafka is not added. A later Kafka consumer can perform that same swap. The parent plan still owns Kafka.

### Phase 5 — Sign in and register

Use the current API.

Sign in: username, password, Sign in. Register Now links to the register form. Sign in links back.

Register asks for the company name, business email, industry, company size, headquarters, an optional website, and a password. The picture shows those company fields and does not show a password. The password stays because sign-in needs it. The business email is the login. The company id is made from the company name. Industry is typed, because the picture does not list industries. Company size is one of 1–10, 11–50, 51–200, 201–500, 501–1,000, or 1,000+. Website is optional. These values are stored on the company row.

Continue with Google and Continue with LinkedIn are visible. A click does not sign the user in and does not call a provider. Those logins are later backend work.

There is no email verification and no separate administrator row. The company login is the only account.

Acceptance: register, login, a second login invalidating the first token, and a vendor list that is empty for another company. Existing `tests.test_api` login tests stay green.

### Phase 6 — Fit and polish

Viewports to check after the flows exist: 1920×1080, 1440×900, 1024×768, 768×1024, 390×844.

Keyboard: the drawer, the vendor list, the file input, the clause box, and both forms can be used without a pointer. Status text is not color alone.

Empty, error, and loading states use the same shell. No sample vendor names in the HTML.

### Phase 7 — Verification

- `tests.test_api` for the page and the login.
- `tests.test_jobs` for vendor file rules and the new mode lock.
- A browser pass of welcome, add vendor, upload, clause, processing, failure, refresh, sign in, register, and the five widths.
- Docker image rebuild before the pass on port 8080, because the HTML is copied into the image.

This dashboard stops there. The parent roadmap is not started.

## Backend and database changes

Phase 1 draws controls that do not call new routes. Phase 3, the company name on login, and the policy preview need API or table changes. Preview and download wait on the remaining questions.

| Change | Why | When |
| --- | --- | --- |
| `vendors.input_mode` | One mode, enforced when two requests arrive together. | Phase 3, after approval. |
| Clause report on a `jobs` row | A clause has no job today. `jobs.result` is the report. `job_id` opens it again. No second report table. | Phase 3. |
| `vendors.pending_job_id` | The current `job_id` stays until the new report is `succeeded`. Then the old job is deleted and `job_id` moves. A failure leaves the old report. | Check Another. |
| `POST /v1/vendors/{vendor_id}/clause` | The current clause route has no vendor id, so it cannot lock a slot. | Phase 3. |
| `input_mode` on `GET /v1/vendors` | The page has to know which card to disable after a refresh. | Phase 3. |
| `name` on the login and register responses | The header shows the company name. The value is already on `tenant_configs.name`. No new column. | Phase 5. |
| Read and download the one current policy file | The policy page lists one name and downloads those bytes. Another company cannot read them. | Company policy page. |
| Same-bytes policy upload keeps the new object and deletes the old one | The index is renamed only. Download uses the new file name. | Company policy page. The current worker deletes the new object. |
| Different-bytes policy upload deletes the previous policy object | The version bump and re-index stay. The list shows the new file only. | Company policy page. |
| Heading max 255 and clause text max 10,000 | The dialog and `POST /v1/vendors/{vendor_id}/clause` use the same limits. | Phase 3. |
| `created_at` and `updated_at` on `GET /v1/jobs/{job_id}` | The columns already exist. The time card is their difference, including the wait for a worker. No new column. | Results screen. |
| `GET` a PDF for one finished job | Built with `pymupdf`, which is already installed. Same company only. The bytes are the counts, the time, and the finding text. No model call and no new table. | Results screen. |

No new table for notifications, people, OAuth, or billing.

`services/openapi.json` is regenerated from the app when a route is added. The path test compares the file with the running app.

## Dependencies

- Phase 1 before Phase 2. The list needs the shell.
- Phase 2 before Phase 3. A workspace needs a vendor id.
- The Phase 3 lock before the Phase 3 page treats a mode as final.
- Phase 3 before Phase 4. Progress reads the job or the stored clause result.
- Phase 5 can be skinned in Phase 1 and wired when the forms are ready. It does not depend on vendors.
- Phase 6 and Phase 7 after the flows they cover.

## React dashboard

`apps/web` is a separate Vite + React project. It is not the page served at `http://127.0.0.1:8080/`. That door still serves `apps/dashboard/index.html`. Run the React app with `npm run dev` inside `apps/web`; Vite proxies `/v1` to port 8080. Components are separate files, and each component has its own CSS file. Shared page CSS is `src/styles/app.css`.

## Known flaw — same contract after a policy change

Recorded 6 November 2026. Fixed the same day, and covered by `tests.test_jobs`.

Policy 1, then contract 1, stores report 1. After the policy changes, uploading contract 1 again is a new check.

- A finished report is reused only when its `checked_policy_version` is the company's current policy version. A report with no stored version is not reused.
- `get_tenant_config` reads the company row on each call, so a version bumped by another process is the version used for the report cache and the clause cache.
- A queued or running check of those same bytes is still the current job. The worker reads the version when it runs.

## Risks

- The HTML is baked into the API image. A local edit is invisible at `http://127.0.0.1:8080/` until the API image is rebuilt.
- `tests.test_api` asserts exact labels in the HTML, including `loadVendors` and `summary.sentence`. A redesign that drops those strings fails the test until the test is updated to the new markup.
- A clause check holds an API worker for the whole model call. Moving it onto the job table would match contract recovery more closely, and it is a larger change. This plan keeps it in the request and stores the result.
- Drawing the picture's six stages would report work the worker did not do, including on a report-cache hit.
- The same-bytes policy path currently deletes the new object. The company page needs the opposite, or download will not match the new file name.

## Decisions from 6 November 2026

1. Company policy is the page after login. One company has one current policy file. A new file replaces it. The page shows that one name, Preview, and Download. When a file is already stored, the upload says that a new policy deletes the current one.
2. Upgrade to Pro is omitted. It stays on the future list, not in this dashboard.
3. Google and LinkedIn buttons are shown. They do not authenticate.
4. Add single clause opens a dialog with a heading and the clause text. Heading 255 characters. Clause text 10,000 characters.
5. The other input mode is rejected. There is no reset. A failed contract can be uploaded again. A clause that fails before a report is stored clears the mode.
6. The header shows `tenant_configs.name`. Login and register return that name. The avatar uses its first letter and opens the company profile.
7. Settings, Help, and the bell are visible and do nothing. The bell has no unread dot. The theme control switches light and dark. The profile can change the sign-in email and the password. The company name, industry, company size, headquarters, and website stay as they were registered.
8. Ask for a job is omitted from the page. The job route stays.
9. The same policy bytes keep the index and only change the file name, in the index and in the bucket. Download returns that file under the new name. Different bytes bump the version as they do now, and the previous file is deleted.
10. Preview shows a PDF on the company policy page. A Word file says to download it.

## Results screen

Decided: the result page has no “Your contract has been analyzed.” or “Your clause has been analyzed.” line. The Not in the contract card is inactive when `missing_clause_check` is `not_run`. The filter uses the five count names and their stored counts.

Check Another clears the results view, warns that a new submission removes this report after the new one succeeds, and suggests the download. It shows the upload and clause cards plus one row for the current report. That row opens the report. The old `jobs` row is deleted only after the new result is stored. No report table is added. `jobs.result` is the stored response.
