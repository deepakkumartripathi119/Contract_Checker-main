# ClauseIntel dashboard — progress

This is the progress record for the dashboard roadmap in `DASHBOARD_IMPLEMENTATION_PLAN.md`.

A task is Verified only after the behavior has been checked, not because a file was written.

`PROJECT_PLAN.md` was not changed.

Last updated: 6 November 2026.

## Status key

- Not started
- In progress
- Implemented
- Tested
- Verified
- Blocked

## Repair pass — 6 November 2026

Checked against the code, then fixed one item at a time. This list is the place to resume.

| Item | Status |
| --- | --- |
| Preview shows a stored PDF on the company policy page, with its own scroll, page number, search, and zoom. A Word file still says to download it. | Verified. A three-page PDF scrolled inside the card. Search for a word on page 2 moved there and marked it. |
| The timed card shows a plain sentence. An error says “We encountered an error.” The policy index and document ids are not shown. The card is a light tint. | Verified in the browser |
| The same contract after a policy change must be checked again. | Tested |
| A refresh returns to the open page. A queued or running check is polled again. | Verified. After a reload, Refresh Check showed “Analyzing your contract” while its job was still queued. |
| A live policy PDF upload. | Verified. A numbered policy PDF finished indexing. The page said “The company policy is ready.” and the stored file was company-policy.pdf. |
| Widths 1920×1080, 1440×900, 1024×768, 768×1024, and 390×844. | Verified. No horizontal overflow. The policy heading stayed visible. At 1024 and 390 the rail kept the logo. |
| Keyboard pass of the drawer, vendor list, file input, clause dialog, and both forms. | Verified. Each visible control on sign-in, register, the sidebar, the vendor page, the policy page, Add Vendor, and the clause dialog accepted focus. The open dialog kept focus on its own fields. |
| Tests for the mode lock: the other choice is rejected before a report is stored, and only one overlapping first claim is stored. | Tested |
| Sign in with a company that is already in the database. | Verified. Repair Check signed in again and the page opened on Company policy. |
| Delete removes the vendor or report immediately and erases the files from a job-table event. A row waiting to be erased cannot be read or published again. A crash during the click rolls the database writes back. A crashed worker claims the same delete again and finishes the erase. A new upload, a finished replacement, and a policy version commit with their related rows, and a crash rolls those rows back together. | Tested |

## Left

- The policy-change cache flaw is fixed and covered by `tests.test_jobs`. A finished report is reused only for the current policy version.
- 1920, 1440, 1024, 768, and 390 were measured on 6 November 2026. None overflowed sideways.
- A numbered policy PDF finished indexing on 6 November 2026. The page said “The company policy is ready.”
- After a reload, a queued contract showed “Analyzing your contract.”
- The visible controls on sign-in, register, the sidebar, the vendor page, the policy page, and both dialogs accepted keyboard focus.
- Sign-in with Repair Check, a company already stored, opened Company policy.
- `apps/web` is the React project. It is not what port 8080 serves. `npm run dev` in `apps/web` starts it and proxies `/v1` to 8080.
- Phase tables below this status note are older than the code. Treat this "Left" list and the "Where we are" paragraph as the current tracker.

## Where we are

On 6 November 2026 success and error text is a card at the top right for 6 seconds. The result page has no analyzed heading. Check another is hidden on the upload screen. A closed sidebar keeps the ClauseIntel logo icon and hides the word. The saved-report row has a document icon before the file name. The report filter icon sits at the right, with a clear gap under the count cards. Its menu is smaller and translucent, with no checkboxes and no Total row. Chosen filters collect on the left as chips, and × removes one. Log out is a red-tinted icon. On 6 November 2026 dark mode uses light status tints on findings, a rounded tint on Check another, Google, and LinkedIn, and a larger sign-in logo. Browser autofill on a dark field keeps the dark fill. The company profile is a building mark, a short line, registered-detail tiles, and the sign-in form. On 6 November 2026 the theme control switches the page between light and dark and changes its icon. The avatar opens the company profile, where the sign-in email and password can change and the registration details stay fixed. Log out and Check another are tinted buttons; Check another has a refresh icon. The bell still does nothing. On 6 November 2026 the welcome line “Add a vendor from the sidebar to get started with your contract analysis.” is removed. The report keeps the five stored counts, now coloured by count, and the findings are one continuous list with a symbol for each stored label. Download and delete sit beside Check another as icons. A finished check shows a moving green Completed circle, fades, and then shows the report. The company-policy header uses the two-document picture, Welcome to ClauseIntel uses the logo colours, dialogs have an X, a stored report can be deleted, a vendor can be hidden from the sidebar, and an expired token returns to sign-in. `apps/web` is the separate React project. The three PNG files in `data/` are colour and layout reference. `DASHBOARD_IMPLEMENTATION_PLAN.md` is the record of the decisions. Visual match is one page at a time, in this order: sign-in (done), register (done), company policy (the page after sign-in), the opened-vendor welcome, sidebar, analyzing, and results. The sidebar is fixed to the screen and does not scroll. The header button still slides it closed and open. The clause dialog is centered in the main area while that panel is open. A running contract job reports `extracting`, then `analysing`, then `matching`, then `completed`. The analyzing page shows that step as a line of marks and arrows, not as separate boxes. The same contract uploaded after a policy change is checked again. See the repair pass above. There is no Vendors tab. Creating a vendor opens that vendor. Its empty page is the welcome picture: Upload Contract sends a file for that vendor, and Type Clause opens the heading and clause dialog (255 and 10,000). Company policy shows one current file, or the upload when none is stored. Delete vendor is a soft delete in the database (`vendors.deleted_at`). The page says “Delete”, not that the row is hidden or that files stay. Delete report removes only the report that vendor shows. An expired login token returns the person to the sign-in page. The company policy header uses the two-document illustration. Welcome to ClauseIntel uses the logo colors. Dialogs have an X. A numbered policy PDF finished indexing, a reload resumed a queued contract, and the visible controls accepted keyboard focus. The five widths were measured. The shell was restyled toward the reference: navy sidebar, lavender page, rounded cards, and a spinner while a check runs. On 6 November 2026 a company registered in the browser at `http://127.0.0.1:8766/`, landed on Company policy, and created a vendor named North Supply. The header showed the company name. The first login had been opening the empty-vendor welcome instead of Company policy. That jump is removed. The five viewport widths were measured. A numbered policy PDF finished indexing. The company name in the header is an icon and the name, with no white chip. On a vendor page the name is a back arrow and the name, also with no white chip. Delete controls are red icons. The hint, the replacement sentence, and the stored file sit on the tinted page rather than in stacked white cards.

## Phase 0 — Audit and roadmap

| Task | Status |
| --- | --- |
| Read the API, login, vendors, jobs, worker, and both graphs | Verified |
| Map each reference screen to a route that exists | Verified |
| Write `DASHBOARD_IMPLEMENTATION_PLAN.md` | Verified |
| Write `DASHBOARD_ARCHITECTURE_DECISIONS.md` | Verified |
| Write this progress file | Verified |
| Eight answers recorded | Verified |
| One current policy file, rename in the bucket, preview placeholder, 255 and 10,000 limits | Verified |
| Roadmap approved to start Phase 1 | Verified |

What was done: the repository was read. The client is `apps/dashboard/index.html`, served by FastAPI. Vendors, jobs, and the clause check were traced. The pictures were compared with those routes.

Files created: `DASHBOARD_IMPLEMENTATION_PLAN.md`, `DASHBOARD_PROGRESS.md`, `DASHBOARD_ARCHITECTURE_DECISIONS.md`.

Files modified: none in the application. `PROJECT_PLAN.md` was not modified.

Backend changes: none.

APIs added or modified: none.

Tests executed: none in this session. The audit did not change code.

Known limitations: the open questions in the implementation plan are unanswered.

Remaining work: approval, then Phase 1.

Next task: wait for approval of the roadmap and the open questions.

## Phase 1 — Shell

| Task | Status |
| --- | --- |
| Sidebar, top bar, welcome, and responsive shell | Tested |
| Company policy in the sidebar; Ask for a job omitted | Tested |
| Update `tests.test_api` labels if the markup changes | Tested |
| Browser check of the shell at the five widths | In progress |

## Phase 2 — Vendors

| Task | Status |
| --- | --- |
| Add Vendor calls `POST /v1/vendors` | Verified |
| Sidebar lists names from `GET /v1/vendors` | Verified |
| Selecting a vendor opens its workspace | Verified |
| Empty company and empty vendor states | Not started |
| Refresh keeps the selected vendor | Not started |

## Phase 3 — Contract or one clause

| Task | Status |
| --- | --- |
| `vendors.input_mode` and a stored clause report | Not started |
| Transaction so two first submissions cannot both win | Not started |
| `POST /v1/vendors/{vendor_id}/clause` stores the result on a job row | Not started |
| Check Another keeps the current job until the new result is stored | Not started |
| Clause dialog with heading (255) and text (10,000) | Not started |
| Tests for both modes, replacement, and overlap | Not started |
| Regenerate `services/openapi.json` | Not started |

## Phase 4 — Progress

| Task | Status |
| --- | --- |
| Poll `GET /v1/jobs/{job_id}` for a contract | Not started |
| One analyzing state for `queued` and `running` | Not started |
| Report on `succeeded`, error on `failed` | Not started |
| Refresh resumes a running contract | Not started |
| Clause wait and stored report after refresh | Not started |
| Do not draw the six picture stages | Not started |
| Detailed section shows the stored finding text. No bars | Not started |
| Time card from `created_at` and `updated_at` on the job response | Not started |
| Five count cards from the stored summary | Not started |
| Overall verdict, rate, and "No Major Issues Found" | Verified — omitted. The result has no whole-file conclusion |
| Download Report as a PDF from the stored result | Not started |

## Phase 5 — Sign in and register

| Task | Status |
| --- | --- |
| Sign in with `login_name` and password | Tested |
| Register with `org_id`, `name`, `login_name`, and password | Tested |
| Google and LinkedIn buttons, no login | Tested |
| Header shows the stored company name | Verified |
| Existing login tests still pass | Not started |

## Phase 6 — Fit and polish

| Task | Status |
| --- | --- |
| 1920×1080, 1440×900, 1024×768, 768×1024, 390×844 | Not started |
| Keyboard use of the shell, list, upload, and forms | Not started |
| Empty, error, and loading states | Not started |

## Phase 7 — Verification

| Task | Status |
| --- | --- |
| Unit tests for the page, login, vendors, and the mode lock | Not started |
| Browser pass of the flows | Not started |
| API image rebuild so port 8080 serves the new page | Not started |
| Stop and report. Do not start the parent roadmap | Not started |

## Session log

### 6 November 2026

Audit and the three dashboard documents. The eight answers, then the policy-file rules, were written into the plan. No application code.
