# Patient Registration and Department Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add patient self-registration and secure department-scoped case submission so a patient-confirmed primary department receives the case while unrelated doctors cannot read it.

**Architecture:** Keep the existing Flask + SQLite + `case_access` authorization boundary. Persist users in a new SQLite table, keep environment accounts as idempotent seeds, bind doctors to standard department codes, and create access grants only for active doctors in the patient-confirmed primary department. Store routing/submission metadata inside `payload.routing` so the existing analysis state machine remains unchanged; extend MDT screening to grant additional access only when a lead doctor explicitly recommends it.

**Tech Stack:** Flask, Python standard library `sqlite3`, PBKDF2-SHA256 already provided by `services.auth`, existing `services.department_domain`, unittest, Node 18+ frontend verifier.

**Spec:** `docs/superpowers/specs/2026-09-29-患者注册与科室分流设计.md`

## Global Constraints

- 用户名规范为 3–64 个 ASCII 字符（字母、数字、`.`、`_`、`-`），密码长度为 8–128 个字符。
- 注册接口只创建 `patient` 角色；客户端不能注册医生，注册请求中的 `role` 字段必须拒绝。
- 医生账号只能由环境变量配置或部署初始化写入用户表，每个医生账号必须绑定标准 `department_code`。
- 患者确认主责科室后，默认只授权该科室的 active 医生；模型协同科室意见不等于访问授权。
- 医生队列的科室过滤不能替代后端权限校验；病例详情必须继续由 `case_access` 硬授权保护。
- 患者端不得返回 `reviewer_id`、医生用户名、密码或密码哈希。
- `APP_AUTO_ASSIGN_REVIEWER=false` 继续作为默认安全配置；显式开启时也必须按病例主责科室匹配医生。
- 旧的 `APP_DOCTOR_USERNAME` / `APP_DOCTOR_PASSWORD`、内存 `AuthService(accounts=...)` 测试夹具、旧病例和游客链路必须继续可用。
- 不新增破坏现有病例状态机的状态；注册提交状态放在 `payload.routing`。
- 所有后端新行为先写失败测试，再写最小实现；每个任务完成后运行该任务的聚焦测试并单独提交。

---

### Task 1: Persist users and expose doctor department identity

**Files:**
- Modify: `backend/services/store.py:64-155` schema initialization and new user methods
- Modify: `backend/services/auth.py` session, configured-account loading, login lookup, registration helpers
- Modify: `backend/app.py:92-138` store/auth initialization order
- Modify: `.env.example` doctor account configuration comments
- Create: `backend/tests/test_user_store.py`
- Modify: `backend/tests/test_auth.py`

**Interfaces:**
- `Store.create_user(username: str, password_hash: str, *, role: str, label: str, department_code: str = "", status: str = "active") -> bool`: insert a user only when the username is unused; return `False` for duplicates.
- `Store.load_user(username: str) -> Optional[Dict[str, Any]]`: return the internal account record including `password_hash` for `AuthService`; API responses must never expose this dict directly.
- `Store.list_users(*, role: Optional[str] = None, department_code: Optional[str] = None, status: str = "active") -> List[Dict[str, Any]]`: return matching persisted users, ordered by username.
- `AuthService.__init__(..., store: Optional[Any] = None)`: keep the current `accounts=` constructor path for tests and add an optional Store-backed provider for the application.
- `AuthService.register_patient(username: str, password: str, *, label: str = "") -> Session`: create a patient account in the backing store (or the in-memory accounts map when no store is supplied) and return an authenticated patient session.
- `AuthService.list_reviewer_accounts(department_code: str) -> List[Dict[str, str]]`: return only active doctor accounts whose standard department code matches; each result contains `username`, `label`, and `department_code`.
- `Session` gains optional `department_code` and `department_name`; `to_dict()` includes them only when present, so existing patient responses remain stable.

- [ ] **Step 1: Write failing Store tests for the user table and department lookup.**

  Add tests with `Store(":memory:")` that assert `create_user()` inserts a PBKDF2 hash, duplicate usernames return `False`, `load_user()` returns the role/label/department, and `list_users(role="doctor", department_code="ophthalmology")` excludes inactive or other-department users.

- [ ] **Step 2: Run the new Store tests and confirm the schema/methods fail.**

  Run from `CHAIS/backend`:

  ```powershell
  python -m unittest tests.test_user_store -v
  ```

  Expected: import or missing-method failures for the new user API.

- [ ] **Step 3: Add the `users` table and Store methods.**

  Extend `_init_schema()` with `users`, indexes on `(role, status)` and `(role, department_code, status)`, and use `INSERT` guarded by the primary-key constraint. Convert rows to dictionaries without changing the existing case row conversion. Keep all writes under the existing `RLock` and commit each mutation.

- [ ] **Step 4: Run the Store tests and confirm they pass.**

  ```powershell
  python -m unittest tests.test_user_store -v
  ```

- [ ] **Step 5: Write failing AuthService tests for persisted login, seeded doctors, and registration.**

  Extend `tests.test_auth` with a Store-backed `AuthService` fixture. Assert a configured patient and two configured doctors are seeded once, a doctor session contains the expected `department_code`/`department_name`, `list_reviewer_accounts()` returns only the matching department, and `register_patient()` stores a PBKDF2 hash and returns a patient session. Keep the existing in-memory account tests unchanged.

- [ ] **Step 6: Implement Store-backed AuthService lookup and idempotent seed parsing.**

  Add `APP_DOCTOR_DEPARTMENT` and optional `APP_DOCTOR_ACCOUNTS_JSON` parsing. Preserve the legacy single doctor environment variables as one seed entry. For each configured account, hash a raw password only when the account is first inserted; never overwrite an existing row on startup. Login should consult explicit in-memory `accounts` first when provided by tests, then the Store-backed users. `is_reviewer_account()` and `list_reviewer_accounts()` must check role and active status.

  Initialize the application Store before `AuthService(store=_shared_store)` so persistent registrations survive process restarts. Do not import `Store` from `auth.py`; use the optional provider type to avoid a circular import.

- [ ] **Step 7: Run auth regression and new user tests.**

  ```powershell
  python -m unittest tests.test_auth tests.test_user_store -v
  ```

- [ ] **Step 8: Commit the account foundation.**

  ```powershell
  git add backend/services/store.py backend/services/auth.py backend/app.py backend/tests/test_user_store.py backend/tests/test_auth.py .env.example
  git commit -m "feat: persist patient and department doctor accounts"
  ```

### Task 2: Add patient registration API with rate limiting

**Files:**
- Modify: `backend/app.py:92-138,234-320` auth constants and routes
- Create: `backend/tests/test_registration.py`
- Modify: `.env.example` registration limit documentation

**Interfaces:**
- `POST /api/register` accepts `{username, password, password_confirm?, label?}` and returns `201` with `status`, a patient-safe session, and patient scopes.
- `POST /api/register` returns `400` with `code=invalid_request` for invalid fields or a non-empty `role` field, `409` with `code=username_taken` for duplicates, and `429` with `code=rate_limited` after `APP_REGISTRATION_MAX_PER_HOUR` attempts from one `request.remote_addr`.
- The route calls `auth_service.register_patient()` and never writes account records directly.

- [ ] **Step 1: Write failing HTTP tests for registration.**

  Use an in-memory Store and `AuthService(accounts={...}, store=store)` in `setUp`, replace `app_module.auth_service`, and clear the registration counter in `tearDown`. Cover successful registration/auto-login, duplicate username, invalid username, short password, role escalation, and rate-limit behavior. Assert the response and persisted row never contain `password` or `password_hash`.

- [ ] **Step 2: Run the registration tests and confirm the route is missing.**

  ```powershell
  python -m unittest tests.test_registration -v
  ```

  Expected: `404` for `/api/register` and failing assertions.

- [ ] **Step 3: Implement registration validation and source-address throttling.**

  Add `APP_REGISTRATION_MAX_PER_HOUR` with default `20` using `read_int_env`. Track timestamps in a bounded `defaultdict(deque)` keyed by `request.remote_addr`, prune entries older than one hour, and return JSON `429` without exposing account details. Parse the request with `_json_object()` and `string_field()`, reject any supplied `role`, require matching `password_confirm` when supplied, and map `AuthError` duplicate failures to `409 username_taken`.

- [ ] **Step 4: Run the registration tests and the existing auth/security tests.**

  ```powershell
  python -m unittest tests.test_registration tests.test_auth tests.test_security_access -v
  ```

- [ ] **Step 5: Commit the registration endpoint.**

  ```powershell
  git add backend/app.py backend/tests/test_registration.py .env.example
  git commit -m "feat: add patient self-registration"
  ```

### Task 3: Add department catalog and patient case submission routing

**Files:**
- Modify: `backend/services/store.py:270-305` idempotent case-access grant behavior
- Modify: `backend/services/case_flow.py:1211-1305` new serialized case submission method
- Modify: `backend/app.py:322-620` department and submit-review routes plus creation auto-assignment
- Create: `backend/tests/test_department_routing.py`

**Interfaces:**
- `GET /api/departments` returns `{departments: [{code, name, group}]}` for `department_domain.PRIMARY_ELIGIBLE_NAMES` only.
- `CaseFlow.submit_case_for_review(case_id: str, *, department_code: str, actor: str, reviewer_ids: Sequence[str]) -> Dict[str, Any]` validates and persists `payload.routing`, writes the routing audit event, and returns a patient-safe routing object. It returns an idempotent success for the same already-submitted department and an error for a different department.
- `POST /api/v2/case/<case_id>/submit-review` is patient-owner-only. Its body may contain `department_code`; when omitted, derive the code from `payload.triage.primary.department`. It returns `409 no_reviewer_available` before changing routing when no active doctor matches.
- `Store.grant_case_access()` returns `True` only when a new grant is inserted; repeated grants return `False` without rewriting `created_at` or adding another grant audit.

- [ ] **Step 1: Write failing routing tests.**

  Build three Store-backed doctor accounts (`endocrinology`, `ophthalmology`) and a patient case whose payload has `triage.primary.department = "内分泌科"`. Assert the department catalog excludes auxiliary departments, default submission grants only the endocrine doctor, selecting ophthalmology grants only the ophthalmology doctor and records `patient_adjusted`, different patients receive `404`, a missing department doctor returns `409` with no routing mutation, and a repeated same-department submission is idempotent. Assert patient-safe case/report JSON contains routing status but no reviewer username.

- [ ] **Step 2: Run the routing tests and confirm they fail.**

  ```powershell
  python -m unittest tests.test_department_routing -v
  ```

- [ ] **Step 3: Make case-access grants idempotent.**

  In `Store.grant_case_access()`, query the `(case_id, reviewer_id)` row under the existing lock. Return `False` immediately when it exists; otherwise insert, commit, write `case_access_granted`, and return `True`. Preserve the current revoke behavior and security tests.

- [ ] **Step 4: Implement `CaseFlow.submit_case_for_review()`.**

  Resolve a code through `department_domain.to_name()` or a standard/alias name through `resolve()` + `to_code()`, reject OOV and auxiliary departments with an explicit error, and construct:

  ```python
  routing = {
      "status": "submitted",
      "triage_primary_department": {"code": triage_code, "name": triage_name},
      "requested_department": {"code": department_code, "name": department_name},
      "patient_adjusted": department_code != triage_code,
      "submitted_at": now,
  }
  ```

  Use `mutate_case()` under the existing per-case lock, preserve all existing payload keys, set routing only after `reviewer_ids` is non-empty, and call `grant_case_access()` for each reviewer. Add one `case_submitted_for_review` audit event containing the department code and count, never the patient’s raw medical text or password.

- [ ] **Step 5: Add the catalog and submit-review routes.**

  Add a small helper in `app.py` to normalize a requested code/name and derive the triage code. The route must call `_guard_case(case_id, 'case:own')`, obtain active reviewers through `auth_service.list_reviewer_accounts()`, and pass only usernames to the service. Return `{ok, routing, changed, reviewer_count}` without reviewer identities. Keep `/api/v2/case/<id>/access` for controlled compatibility only.

- [ ] **Step 6: Make optional auto-assignment department-aware.**

  In the existing `create_case()` closure, when `APP_AUTO_ASSIGN_REVIEWER=true`, derive the triage primary department, list matching active reviewers, and call `submit_case_for_review()` only when at least one matches. Do not fall back to granting the configured doctor every case.

- [ ] **Step 7: Run routing, security, and triage regressions.**

  ```powershell
  python -m unittest tests.test_department_routing tests.test_security_access tests.test_triage tests.test_mdt_screening -v
  ```

- [ ] **Step 8: Commit department-scoped patient submission.**

  ```powershell
  git add backend/services/store.py backend/services/case_flow.py backend/app.py backend/tests/test_department_routing.py
  git commit -m "feat: route submitted cases by primary department"
  ```

### Task 4: Grant explicit MDT collaborator access

**Files:**
- Modify: `backend/services/case_flow.py:1213-1305` MDT screening service
- Modify: `backend/app.py:680-710` MDT screening route
- Modify: `backend/tests/test_mdt_screening.py`
- Modify: `backend/tests/test_department_routing.py`

**Interfaces:**
- Extend `CaseFlow.submit_mdt_screening(..., department_reviewer_map: Optional[Mapping[str, Sequence[str]]] = None) -> Dict[str, Any]` without changing existing callers that omit the map.
- The result adds `authorized_departments: List[str]`, `authorized_reviewer_count: int`, and `pending_departments: List[str]`; existing `mdt_screening` and patient-safe fields remain unchanged.

- [ ] **Step 1: Add failing tests for MDT access grants.**

  Add a Store-backed test with endocrine and ophthalmology doctors. Submit `recommend_mdt` with both departments and a reviewer map; assert both doctors can read the case, the result reports both departments, and an unmapped department is returned in `pending_departments`. Assert `single_specialty` and `urgent_care` create no new collaborator grants.

- [ ] **Step 2: Run the focused MDT tests and confirm the new assertions fail.**

  ```powershell
  python -m unittest tests.test_mdt_screening tests.test_department_routing -v
  ```

- [ ] **Step 3: Implement explicit collaborator grants after valid screening persistence.**

  After the existing validation and payload save, when `decision == "recommend_mdt"`, map each normalized department name to its reviewer IDs, grant access through `Store.grant_case_access()`, and audit `mdt_access_granted` or `mdt_access_pending` per department. Do not grant for other decisions. Keep `patient_safe_mdt_screening()` unchanged so reviewer identities never reach the patient.

- [ ] **Step 4: Update the HTTP route to provide the reviewer map.**

  Normalize the submitted department names exactly as the service does, call `auth_service.list_reviewer_accounts()` for each, and pass `{department_name: [usernames...]}`. The route still relies on `_guard_doctor_case()` before entering the service.

- [ ] **Step 5: Run all MDT/security tests and commit.**

  ```powershell
  python -m unittest tests.test_mdt_screening tests.test_department_routing tests.test_security_access -v
  git add backend/services/case_flow.py backend/app.py backend/tests/test_mdt_screening.py backend/tests/test_department_routing.py
  git commit -m "feat: authorize MDT collaborator departments explicitly"
  ```

### Task 5: Add patient registration and department confirmation UI

**Files:**
- Modify: `backend/templates/patient.html:70-135` login card and inline auth script
- Modify: `backend/templates/patient.html:317-337,1340-1560` report-stage routing card and report rendering
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`

**Interfaces:**
- Add `patient-register-form`, `register-user`, `register-pass`, `register-pass-confirm`, and `register-label` DOM fields; `registerForm()` posts to `/api/register` and calls the existing `afterLogin()` on success.
- Add `S.departments` and `S.routing`; `loadDepartments()` fetches `/api/departments`; `loadCaseRouting()` fetches the patient-safe `/api/v2/case/<case_id>` view.
- `renderReviewSubmission()` returns one primary-action card with the triage recommendation, a standard-department `<select>`, consent copy, and submitted status. `submitCaseForReview()` posts `{department_code}` to `/api/v2/case/<case_id>/submit-review`, then re-renders the card without exposing reviewer IDs.

- [ ] **Step 1: Add failing static and Node assertions.**

  Assert the patient template contains the registration form, password confirmation, registration error region, department selector, submit-review action, and copy stating only the selected department can see the case. Extend the Node stub export with `renderReviewSubmission`, seed `S.triage` and `S.routing`, and assert the unsubmitted and submitted HTML states. Assert registration success uses `/api/register` and patient-safe output does not contain `reviewer_id`.

- [ ] **Step 2: Run the frontend checks and confirm the new assertions fail.**

  ```powershell
  cd E:\aic_competition\CHAIS\backend
  python -m unittest tests.test_frontend_structure -v
  node verify_frontend_logic.mjs
  ```

- [ ] **Step 3: Implement the patient auth toggle and registration handler.**

  Keep the existing login form and add a keyboard-accessible button/link that toggles a hidden registration form. Validate matching passwords client-side, show errors in a `role="alert"` live region, post JSON, and reuse `afterLogin()` so session storage and flow initialization remain single-sourced.

- [ ] **Step 4: Implement report-stage department confirmation and submission.**

  Render the routing card in the summary view only so the report has one clear primary submission action. Populate options from `/api/departments`, select the system primary by default, show a warning when the patient changes it, disable the button while submitting, and render “已提交至 X 科，等待医生审核” after success. Keep the existing report accordion and print output unchanged.

- [ ] **Step 5: Run the frontend checks and commit.**

  ```powershell
  python -m unittest tests.test_frontend_structure tests.test_responsive_layout -v
  node verify_frontend_logic.mjs
  git add backend/templates/patient.html backend/tests/test_frontend_structure.py backend/verify_frontend_logic.mjs
  git commit -m "feat: let patients register and submit to a department"
  ```

### Task 6: Show doctor department identity and queue scope

**Files:**
- Modify: `backend/templates/doctor.html:150-210,320-375,460-500` identity and queue empty state
- Modify: `backend/tests/test_frontend_structure.py`
- Modify: `backend/verify_frontend_logic.mjs`

**Interfaces:**
- `S.me.department_code` and `S.me.department_name` come from the enriched login session.
- `enterApp()` renders `#ident` with the doctor label and department and fills `#doctor-dept-note` with `当前科室：X`.

- [ ] **Step 1: Add failing structure and Node assertions.**

  Assert `doctor.html` contains `doctor-dept-note`, queue scope copy, and uses `department_name` from the session. Add a Node case where `S.me` is an ophthalmology doctor and `enterApp()` produces the department label without changing queue data.

- [ ] **Step 2: Run the focused frontend checks and confirm failure.**

  ```powershell
  cd E:\aic_competition\CHAIS\backend
  python -m unittest tests.test_frontend_structure -v
  node verify_frontend_logic.mjs
  ```

- [ ] **Step 3: Implement the department identity/empty-state copy.**

  Add a small status line above the existing queue toolbar, keep the backend queue response as the source of truth, and change only the empty-state text to include the current department. Do not add a client-side permission filter.

- [ ] **Step 4: Run the focused checks and commit.**

  ```powershell
  python -m unittest tests.test_frontend_structure -v
  node verify_frontend_logic.mjs
  git add backend/templates/doctor.html backend/tests/test_frontend_structure.py backend/verify_frontend_logic.mjs
  git commit -m "feat: show doctor department queue scope"
  ```

### Task 7: Update deployment docs and run end-to-end verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `backend/verify_doctor_http.py`
- Create: `backend/verify_patient_registration.py`
- Modify: `backend/tests/test_frontend_structure.py` only if documentation copy is asserted there

**Interfaces:**
- `.env.example` documents `APP_DOCTOR_DEPARTMENT`, `APP_DOCTOR_ACCOUNTS_JSON`, and `APP_REGISTRATION_MAX_PER_HOUR` with a non-secret example.
- `verify_patient_registration.py` logs in or registers a unique test patient, creates a case, calls `/submit-review`, and checks the returned routing status without printing credentials.
- `verify_doctor_http.py` uses `/submit-review` instead of arbitrary reviewer-id grant when a department-configured doctor is available; it retains the legacy access fallback only for deployments without `APP_DOCTOR_DEPARTMENT` and labels that fallback clearly.

- [ ] **Step 1: Add failing verifier assertions and documentation checks.**

  Add a test that parses `.env.example` for all three keys and a verifier assertion that patient submission returns `routing.status == "submitted"` and no `reviewer_id`.

- [ ] **Step 2: Update docs and repeatable HTTP verifiers.**

  Document the competition flow: patient registration → triage confirmation → department submission → matching doctor queue. Explain that doctor accounts are provisioned, not publicly registered, and that MDT access is explicit.

- [ ] **Step 3: Run the complete focused verification set.**

  ```powershell
  cd E:\aic_competition\CHAIS\backend
  python -m unittest tests.test_auth tests.test_user_store tests.test_registration tests.test_security_access tests.test_department_routing tests.test_mdt_screening tests.test_frontend_structure tests.test_responsive_layout -v
  node verify_frontend_logic.mjs
  python -m compileall -q .
  ```

- [ ] **Step 4: Run the full suite and record baseline differences.**

  ```powershell
  python -m unittest discover -s tests -p 'test_*.py' -v
  ```

  The current branch already has unrelated full-suite failures; compare the failure list with the pre-change baseline and do not claim a green full suite unless every failure is resolved and attributable to this feature.

- [ ] **Step 5: Commit deployment docs and verifiers.**

  ```powershell
  git add .env.example README.md backend/verify_doctor_http.py backend/verify_patient_registration.py
  git commit -m "docs: document patient registration and department routing"
  ```

## Plan Self-Review

- Spec coverage: user persistence and seeded doctors are Task 1; patient registration and rate limiting are Task 2; department catalog, routing, idempotency, and auto-assignment are Task 3; explicit MDT access is Task 4; patient UI is Task 5; doctor UI is Task 6; deployment/verifier coverage is Task 7.
- Permission coverage: every patient mutation uses `_guard_case(..., 'case:own')`; doctor reads continue to use `case_access`; no UI filter is treated as authorization.
- Privacy coverage: registration responses omit hashes, patient responses omit reviewer identities, and audit details contain only routing metadata.
- Compatibility coverage: legacy environment accounts, explicit in-memory test accounts, old access endpoint, old cases, and guest endpoints are explicitly tested.
- Placeholder scan: no `TODO`, `TBD`, or unspecified implementation step is required by this plan.
- Type consistency: Store/auth/routing/MDT method signatures are defined before their consumers, and frontend state names (`S.departments`, `S.routing`) are consistent across Tasks 3 and 5.
