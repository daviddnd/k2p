# Frontend

The TCSP-facing UI for **K2P (Kairos Pinax)** on the execution plane (MVP A): sign-in, the portfolio
dashboard, the compliance calendar, the company review screen (extracted
prior-filing proposals, confirmed or rejected; the company's officer and member
records; and its drafts with the gated generation form), the draft review
surface (a draft's resolved values, provenance highlighting, QC flags, filing
metadata and allowed actions), the change-event lifecycle screens (the
cross-company review queue, a company's event list with manual entry, and the
event detail with its lineage and allowed actions) and the calendar-feed panel
— consuming `GET /api/v1/dashboard`, `GET /api/v1/compliance/calendar`,
`GET /api/v1/companies/{id}(/master-data|/import-batches|/change-events|/officers|/shareholdings|/drafts)`,
`POST|PATCH|DELETE /api/v1/officers|/shareholdings`,
`GET|POST /api/v1/drafts(/{id}|/{id}/transitions)`,
`GET|POST /api/v1/change-events(/{id}/transitions|/{id}/assessments)` and
`/api/v1/calendar/feed-credentials` (scope §2.1/§2.3/§2.4/§2.7/§2.8/§2.9;
contract §5.1).

Requires Node 22+. Backend setup and the full command list are in the root
`README.md`.

```bash
npm install
npm run dev        # dev server on :5173, proxying /api to http://localhost:8000
npm run build      # tsc -b + vite build
npm run lint       # oxlint
npm run verify     # SSR render + client checks against the fixtures
```

## Layout

```
src/api/          the HTTP seam: types.ts (wire shapes), client.ts (fetch,
                  bearer token, 401 handling, the 204-as-undefined rule),
                  endpoints.ts
src/session.ts    the session store (sessionStorage) and its XSS caveat
src/route.ts      hand-rolled hash routing (#/dashboard, #/calendar,
                  #/client-actions, #/change-events, #/companies/{id},
                  #/companies/{id}/change-events/{eventId},
                  #/companies/{id}/drafts/{draftId})
src/filters.ts    the dashboard's client-side filter rule (§2.3 search by
                  name, BRN or CR number)
src/coverage.ts   which companies the calendar feed serves, derived from the
                  dashboard's own gate flags
src/renewals.ts   the renewal panel's translations and the shared refusal
                  vocabulary every screen's refusals are read through
src/change_events.ts
                  the change-event lifecycle's translations and the typed
                  payload grammar the entry/edit form is built from
src/officers.ts   the officer and shareholding records' translations, and the
                  form state their panels' add/edit forms build bodies from
src/drafts.ts     the draft lifecycle's translations and the readers the draft
                  screens render resolved_state, field_provenance, qc_flags
                  and filing_metadata through
src/views/        *Route.tsx fetch and own the resource; *View.tsx are pure
                  presentational components (which is what the SSR
                  verification renders)
src/components/   shared pieces (deadline table, badges, error panel, the
                  ingestion review table, the change-event form, the person
                  and address field groups, the officer and member panels,
                  the company's drafts section, the filing-metadata form)
verify/           the SSR verification entry and its fixtures
```

## Notes

- **No new dependencies.** `fetch` for transport, plain CSS for styling, no
  router, no query or UI library. `sessionStorage` holds the bearer token;
  moving to an httpOnly cookie needs CSRF handling the backend does not have
  yet (see the comment in `src/session.ts`).
- **One place handles 401.** `src/api/client.ts` drops the dead token and the
  app returns to sign-in; a 401 while signing in is an ordinary credential
  error. 403, network failures and other statuses have visible states with a
  retry affordance. It also resolves a 204 as `undefined` — the delete routes
  answer that, and "it worked" is the whole payload.
- **The review screen shows the backend's own evidence** — the proposal's
  target and value, the extractor's confidence, the run's §2.7 routing and
  overall score, the provenance from the assessment's `signal_log`, and the
  effective date. Confirming writes master data, rejecting writes nothing, and
  the effective date the API requires is collected rather than invented: the
  confirm form pre-fills the filing's own date when it stated one and sends
  nothing when the reviewer leaves it empty, so the API's own 422 is what
  explains a missing one.
- **The calendar-feed panel is Administrator-only** (its routes are), lives on
  the dashboard, and shows the subscription URL exactly once with a copy
  affordance. Below Standard it explains the gate's refusal instead of letting
  the reviewer meet it — the coverage state is derived from the dashboard's
  service-gap flags, which are the same gate's decisions.
- **The views render only what the backend reports.** Widgets the backend
  deliberately omits (pending client confirmations, drafts awaiting approval or
  signature, the activity feed) are not faked; the dashboard says so in one
  footnote. The extraction *trigger* (uploading a prior filing and running the
  gated extraction) is still an API call, and the company screen says so where
  a reviewer would look for it.
- **The officer and member panels keep two boundaries.** The add forms create
  the person inline (there is no grant-scoped person search), the §1
  protected-class fields are never controls — the public-page partial
  identifiers are, and the API refuses `hkid_full`, `hkid_check_digit`,
  `passport_full` and `usual_residential_address` by name — and a person's
  identity is corrected on the Administrator-only persons surface: an edit
  states the appointment's or holding's own fields, and a removal is for a row
  entered by mistake while a cessation is a window correction. Money and the
  percentage cross as strings, and the API's own named refusals
  (`address_invalid`, `capacity_role_mismatch`, `shareholding_invalid: <field>`,
  …) are shown verbatim.
- **The draft screens hold no §4.2 rule of their own.** The detail renders
  action buttons strictly from `GET /api/v1/drafts/{id}/transitions` (the sole
  allowed-actions authority, so unwired actions are never offered), shows the
  resolved values and the generation's QC flags as the backend wrote them,
  marks the `changed` provenance entries with their source trace (scope §2.9
  step 8 on screen), and lets `edit` state the **filing metadata only** — a
  resolved value comes from master data or a member record and is picked up by
  regenerating, which the screen says. Generation is the gated automation: a
  denial is the API's own `403 automation_denied: <reason>`, explained in
  place, and it writes nothing.
- **The change-event screens hold no lifecycle rule of their own.** The event
  detail renders action buttons strictly from
  `GET /api/v1/change-events/{id}/transitions` (the sole allowed-actions
  authority, so unwired actions are never offered), shows the API's own
  409/422 refusal codes and words when a stale screen submits one, and the
  entry/edit form mirrors the domain's typed-payload grammar per event type —
  the §1 protected-class fields (`hkid_full`, `hkid_check_digit`,
  `passport_full`, `usual_residential_address`) are not controls of it, and
  `company_name_change` renders disabled with its Wave-2 reason. `edit` sends
  only the fields that changed, carrying the payload parts the form does not
  offer through untouched; the assessments section is truthful-empty until the
  parser exists.
- **Verification without a test framework.** `npm run verify` renders the
  views with React's SSR renderer and asserts on the HTML, calls the filter
  rule directly (typing is not machine-checkable through markup), and checks
  the client's request shapes and failure modes against a stubbed `fetch`. To
  check the fixtures against the backend's own response models:
  `backend/.venv/bin/python verify/check-fixtures.py` (run `npm run verify`
  first — it writes `dist-ssr/fixtures.json`).
- **What SSR cannot check, a human must:** appearance in a real browser, and
  everything a click reveals (a record's confirm/reject form, the revoke
  confirmation, the clipboard copy).
