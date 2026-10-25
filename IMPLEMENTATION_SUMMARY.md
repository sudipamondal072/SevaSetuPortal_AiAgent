# Sewa Setu agent integration - implementation and handoff

## What is implemented

- Two separate streamable-HTTP MCP servers are available at
  `http://localhost:8001/mcp` (citizen) and `http://localhost:8002/mcp` (officer).
  The citizen server exposes scheme information, application/draft status,
  create-or-resume and save draft, age-proof attachment and validation, and
  confirmed final submission. The officer server exposes the enabled-block
  queue, case review, approve/reject decisions, and reasoned age-proof review.
- Both servers accept only portal-issued bearer tokens. Each verifies the
  signature, issuer, exact audience, role and scope. A citizen token cannot use
  officer tools, and neither MCP tool accepts a caller-selected mobile or actor.
  Resource metadata advertises the same canonical issuer as portal OAuth
  discovery; verification remains compatible with tokens issued before the
  issuer-format change.
- The portal exposes the tools' API routes in [app/agent_api.py](./app/agent_api.py).
  Drafts and random-named proof files are private, resume by authenticated mobile
  and opaque draft reference, accept either MCP base64 uploads or authenticated
  multipart uploads for a chat frontend, and expire after a configurable
  inactivity period (30 days by default). New drafts report `PENDING_UPLOAD`;
  stored files report `PENDING_VALIDATION`. PDF and image parsers check
  readability, but this never establishes age or authenticity: an authorized
  block officer must inspect the current document ID and explicitly record
  `VALID`, `INVALID`, or `REVIEW_REQUIRED` with a reason before submission.
  Submissions use the portal's `validate_application()` and `create_application()`
  logic. A transaction advisory lock in [app/app.py](./app/app.py) serializes
  one-application-per-mobile checks across web and MCP paths. A withdrawn
  application frees the verified mobile for a new application; rejected and
  other non-withdrawn applications continue to block replacements.
- Bank account numbers remain strings, preserve leading zeros, remove accidental
  whitespace, use configured `BANK_ACCOUNT_LENGTHS`, and are masked in MCP
  responses and summaries. The portal does not claim ownership from format checks.
  Unavailable village mappings do not reject an application.
- Officer decisions require a reason and write the authenticated officer and
  reason to the portal's application row and audit log. The portal remains the
  system of record, so web and MCP channels use the same database.
- Citizen applications and supported officer MCP operations use every block
  in the portal's supported-block catalog. `MCP_ENABLED_BLOCKS` is deprecated
  and ignored, so an old Sonari-only environment setting cannot restrict
  submissions, queues, lookups, reviews, or decisions. Village-to-block
  relationship validation remains deferred because no authoritative registry
  is present; historical applications are not used as a substitute.
- [dashboard/eval_dashboard.py](./dashboard/eval_dashboard.py) serves
  `/eval-dashboard`; [evals/personas.json](./evals/personas.json) defines three
  specific synthetic citizens. [evals/run_evals.py](./evals/run_evals.py) uses
  actual MCP streamable-HTTP client sessions and verifies application, file,
  duplicate, status, queue, decision and audit state directly in Postgres.

## Design note

1. **Separate citizen and officer MCP endpoints.** This keeps officer tools out
   of a citizen assistant's tool list and makes audience/role validation
   explicit.
2. **Reuse the portal's validator and record-creation path.** MCP submissions
   cannot bypass age, required fields, block scope, scheme window, proof,
   IFSC or active-application checks. The portal's shared active check now also
   locks by verified mobile so concurrent web/MCP requests cannot both pass.
3. **Keep decisions attributable and bounded.** Officer decisions require a
   reason, persist the token subject as the officer, and are limited to valid
   blocks in the portal's supported-block catalog. Citizen and officer MCP
   endpoints remain separate and role-authorized.

No trusted OCR or authenticity service is configured. The implementation
therefore keeps the application in draft until an authorized officer reviews the
actual stored document; a future approved verification service could automate
some checks without weakening that gate.

The repository does not contain an embedded chat frontend. Its host must either
pass actual attachment bytes to the MCP `attach_age_proof` tool or call the
authenticated multipart draft-upload API directly. A rendered image alone is
never treated as an uploaded file.

## Personas and evals

| Citizen | Scenario | Required portal result |
|---|---|---|
| Kamla Devi, 70, Sonari Basti | Speaks simply and asks her grandson to help; seeks the pension. | One pending Sonari application for her verified mobile, age proof and submit audit; duplicate attempt denied; status follows the same row. |
| Shanti Devi, 59, Sonari Basti | Asks to apply before her 60th birthday. | Portal age rule rejects her; no application is created. |
| Rahim Ansari, 68, Rajapara Bazaar | Gives details politely and asks whether his nearby block is covered. | One pending Rajapara application is saved and verified in Postgres; the officer MCP queue can review and decide it. |

The officer scenarios check queue visibility for both supported blocks,
case/audit review, decisions with a synthetic reason, officer attribution in
the database, and the citizen's updated status. The evaluator uses new
synthetic identities and must run only against an isolated deployment whose
portal and evaluator share the same disposable database. On Windows, run
`.\evals\run_disposable_eval.ps1` to create and clean up that isolated database.
For a separately managed isolated deployment, set `EVAL_DATABASE_URL`,
`EVAL_CITIZEN_TOKENS` (persona ID to fresh token), and `EVAL_OFFICER_TOKEN`,
then run `python -m evals.run_evals`.

**Eval result: passed (3/3).** The isolated evaluation completed MCP draft,
document upload and review, eligibility validation, Rajapara submission,
PostgreSQL persistence, duplicate checks, officer queue/case review and
decisions. The production database was not used or reset; see
[evals/results/results.json](./evals/results/results.json) for the recorded
synthetic evaluation details.

## Verification completed

- Full pytest suite: **139 passed**.
- Changed Python modules compiled successfully.
- Primary and isolated evaluation Docker Compose configurations validated with
  temporary test-only environment values.
- `.\evals\run_disposable_eval.ps1` completed all **3/3** synthetic personas
  against a dedicated PostgreSQL database; its isolated containers and volumes
  were removed afterward. No production database records were changed.

## Local walkthrough

1. Copy `.env.example` to `.env` and set unique values for the required
   `ADMIN_PASSWORD`, `DB_PASSWORD`, `SECRET_KEY`, and `SMS_GATEWAY_TOKEN`, then
   run `docker compose up --build -d`.
2. Open the portal at `http://localhost:8000/` and the evaluation page at
   `http://localhost:8000/eval-dashboard`. Interactive form URLs in
   [PORTAL_URLS.txt](./PORTAL_URLS.txt) require the expected browser session.
3. Connect a citizen assistant to `http://localhost:8001/mcp` and an officer
   assistant separately to `http://localhost:8002/mcp`. Complete the portal's
   OAuth sign-in and consent for the matching MCP resource; do not reuse tokens.
4. To run the written evals, use a cloned/isolated deployment and synthetic
   identities, set the three `EVAL_*` environment variables from
   [README.md](./README.md), then run the evaluator. Review database assertions
   both in the results file and at `/eval-dashboard`.
5. If the deployment changes or the live assistant is updated, rerun the
   isolated evals and record the actual pass rate and any new lessons.

## Remaining external handoff

- No public VM/deployment URL was provided; the current reachable endpoints are
  local defaults. Set the exact public HTTPS values for `PUBLIC_BASE_URL`,
  `CITIZEN_RESOURCE_URL` and `OFFICER_RESOURCE_URL` before deployment.
- A Loom recording was not created or uploaded. Record a face-and-screen video
  of no more than three minutes after running the real evals; cover the two MCP
  designs, personas, eval outcomes and changes learned from those outcomes.
- No private GitHub repository, collaborator invitation or `.git` zip was
  created. Those brief submission steps require the target account/repository
  and external access.

For the complete local and deployment URL checklist, see [PORTAL_URLS.txt](./PORTAL_URLS.txt).
Operational setup, tool descriptions and eval instructions are in [README.md](./README.md).
