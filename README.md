# NL → SQL Business Q&A Service

Ask plain-English business questions about `sparkline_demo.db` (customers,
products, sales, employees) and get back the generated SQL, the tables it
touched, the result, and a plain-English answer.

**One-line note:** the NL→SQL engine is **Gemini 2.5 Flash** (via its
OpenAI-compatible endpoint) — this is the path described below and the one
that answers every question once a `GEMINI_API_KEY` is set. A deterministic,
schema-valid-by-construction **rule-based engine** also exists purely as an
automatic safety net for if Gemini is ever unreachable or hallucinates (see
"Architecture"); it isn't part of the normal run path and doesn't need any
setup of its own.

---

## Setup (Gemini path)

```bash
cd LLM_KanishShah
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd code
export GEMINI_API_KEY=your-key        # free at https://aistudio.google.com/apikey
uvicorn app.main:app --reload --port 8000
```

That's it — no other configuration is required to run on Gemini.
`DB_PATH` (the SQLite file every generated query actually executes against)
already defaults to `sparkline_demo.db` at the repo root, so it only needs
to be set explicitly if you move the database somewhere else.

<details>
<summary><strong>Windows / VS Code step-by-step</strong> (click to expand)</summary>

Two terminals are needed: one to run the server, one to send test
requests. In VS Code, open a terminal with `` Ctrl+` `` and use
`+` in the terminal panel to open additional ones.

**Terminal 1 — run the server:**
```powershell
cd LLM_KanishShah
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd code
$env:GEMINI_API_KEY="your-key"        # free at https://aistudio.google.com/apikey
uvicorn app.main:app --reload --port 8000
```
Leave this running — don't type anything else into this terminal once you
see `Uvicorn running on http://127.0.0.1:8000`.

If `Activate.ps1` is blocked by an execution-policy error, run this once
(per machine, not per project), then retry:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Terminal 2 — test it**, after activating the same venv again (every new
terminal needs its own activation):
```powershell
cd LLM_KanishShah
.\venv\Scripts\Activate.ps1
Invoke-RestMethod -Method Post -Uri http://localhost:8000/ask `
  -Headers @{ "X-API-Key" = "sparkline-demo-key-123" } `
  -ContentType "application/json" `
  -Body '{"question": "Who are our top 5 customers by revenue?"}'
```

Or skip Terminal 2 entirely and just open `http://localhost:8000/docs` in
a browser for the interactive Swagger UI instead.

</details>

**Confirm Gemini is actually the active engine** before sending a real
question:
```bash
curl http://localhost:8000/health
```
A healthy Gemini setup returns `"engine": "CompositeEngine"` and
`"model": "gemini-2.5-flash"`. (`CompositeEngine` is the Gemini-primary /
stand-in-fallback wrapper described in "Architecture" — it's what runs
whenever a `GEMINI_API_KEY` is set, regardless of whether the fallback ever
actually triggers.)

**Auth:** every `/ask` call needs `X-API-Key: sparkline-demo-key-123`
(override with `export API_KEY=...`).

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" -H "X-API-Key: sparkline-demo-key-123" \
  -d '{"question": "Who are our top 5 customers by revenue?"}'
```

Or open `http://localhost:8000/docs` for the interactive Swagger UI —
expand `POST /ask`, click "Try it out," paste the API key into the
`X-API-Key` field, and send a question directly from the browser.

**Tests:** `pytest tests/ -v` (44 tests, no network/API key required —
the LLM-fallback paths are tested with fakes, see `tests/test_fallback.py`).

**All configuration is via environment variables** (none hard-coded, none
committed to this repo):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` (or `LLM_API_KEY`) | *(unset)* | Credential for the Gemini call. **Required to run on Gemini** — see Setup above |
| `API_KEY` | `sparkline-demo-key-123` | Required value of the `X-API-Key` header |
| `DB_PATH` | `sparkline_demo.db` at repo root | Path to the SQLite file. Only needs to be set if the DB is moved |
| `LLM_MODEL` | `gemini-2.5-flash` | Model name passed to the Gemini endpoint |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint | Swap to point at OpenAI / Groq / a local Ollama / vLLM server instead |
| `LLM_TIMEOUT_SECONDS` | `20` | HTTP timeout before falling back to the rule-based engine |
| `LLM_PROVIDER` | `auto` | `auto` = Gemini if a key is set, else rule-based · `stand-in` = force rule-based, ignore any key · `openai`/`gemini` = force the LLM path, error loudly if no key |
| `MAX_ROWS` | `200` | Hard cap injected into every query's `LIMIT` |
| `QUERY_TIMEOUT_SECONDS` | `5` | SQLite execution watchdog |

---


## Architecture

```
question
   │
   ▼
auth: X-API-Key header                              [app/auth.py]
   │
   ▼
NL2SQLEngine.generate_sql()                          [app/nl2sql/]
   │  Gemini 2.5 Flash (primary)
   │     │  network/timeout/bad-response → automatic fallback
   │     ▼
   │  rule-based stand-in engine
   │
   │  (either path: an explicit "can't answer" → graceful response, no retry)
   ▼
sql_validator.validate_and_prepare()                 [app/sql_validator.py]
   │  single-statement + comment + keyword-denylist checks
   │  sqlglot AST parse → must be SELECT
   │  table allow-list (vs. live schema)
   │  column allow-list (vs. live schema, alias-resolved)
   │  LIMIT injected/clamped to MAX_ROWS
   │
   │  validation failure AND Gemini produced the SQL → retry once with
   │  the stand-in (its templates are schema-valid by construction)
   ▼
db.execute_query()                                   [app/db.py]
   │  read-only connection (mode=ro + PRAGMA query_only) + watchdog timeout
   ▼
answer.build_answer()  →  AskResponse JSON            [app/answer.py]
```
Full flow + retry policy: `app/pipeline.py`. `tables_used` is parsed from
the validated SQL's AST — never taken from the LLM's own claim, per the
assignment's explicit requirement. `pipeline.py` also tracks which engine
(Gemini vs. the stand-in) actually produced each answer and logs it on
every request — this stays in the logs rather than the `AskResponse` JSON,
since the assignment's response format doesn't call for it and keeping the
API surface minimal matters more than exposing internal routing detail.

**A real bug found via manual testing, and the fix:** `answer.build_answer()`
originally assumed the *first* result row was always the highest-value one
when phrasing a "leads with" sentence — true for the stand-in's templates
(they always include `ORDER BY ... DESC`), but not guaranteed for
Gemini-generated SQL. A live Gemini run on "What is the total sales amount
by region?" produced syntactically valid, schema-valid SQL with no
`ORDER BY` at all; the rows came back in an order where the true maximum
(West, 899,900) wasn't first, so the old logic named the wrong region as
the leader — correct data, incorrect sentence. The fix computes the actual
maximum across all returned rows instead of trusting row order, so the
"leads with" claim is correct regardless of how the SQL ordered (or didn't
order) its results; see `tests/test_answer.py` for a regression test built
directly from this real, reproduced failure.

**Why Gemini, with a fallback rather than Gemini-only:** the assignment
asks for a real LLM; a single external dependency on the only path that
can answer any question is also a real reliability risk for an internal
tool a non-technical user depends on. `app/nl2sql/llm_provider.py` is a
generic OpenAI-Chat-Completions-compatible client — the same code works
for OpenAI, Groq, or a local Ollama/vLLM server by changing `LLM_BASE_URL`
and `LLM_MODEL`; Gemini 2.5 Flash was selected because it provides strong
structured reasoning, low latency, and a generous free tier while
supporting an OpenAI-compatible API interface.

**Authentication approach:** a static API key in a custom `X-API-Key`
header (`app/auth.py`), checked against `API_KEY` on every `/ask` call —
missing or wrong key → `401`. This is deliberately the simplest mechanism
that satisfies the assignment ("a simple mechanism"): no session state,
trivially testable with `curl`/Swagger, and easy to reason about. A
multi-user production deployment would need per-client keys (or
OAuth/short-lived tokens) plus rate limiting per key — see Limitations.

---

## Prompt design

Sent to Gemini with the live schema interpolated in (`app/db.get_schema_context()`),
so it never drifts from the actual database:
```
You are a careful SQLite query-writing assistant for a small business database.
You translate a plain-English business question into exactly one read-only
SQL SELECT statement that SQLite can execute.

Rules you must always follow:
1. Use ONLY the tables and columns given in the schema below. Never invent
table or column names.
2. Output ONE SQL statement only: a single SELECT. Never use INSERT, UPDATE,
DELETE, DROP, ALTER, PRAGMA, ATTACH, or multiple statements separated by ';'.
3. Do not include comments, markdown fences, or any explanation -- output
raw SQL only, nothing else.
4. Prefer explicit column lists and meaningful aliases (e.g. SUM(amount)
AS revenue) over SELECT *.
5. If the question cannot be answered with the given schema -- because it is
ambiguous, refers to data that doesn't exist here, or asks for a write/delete
operation -- respond with exactly: NO_QUERY: <short reason>
   Do not guess in that case.

Schema:
{schema}
```
Rule 5's `NO_QUERY:` convention gives the model an explicit way to decline
(→ `UnanswerableQuestionError`) instead of confabulating a plausible-but-wrong
query. `temperature=0` for determinism. None of this is trusted as a
*security* boundary — that's what the validator is for, regardless of how
well the model follows instructions (see code comments in `llm_provider.py`).

---

## SQL validation & safety

The layer the assignment calls "most important." Full rationale for each
check is in `app/sql_validator.py`'s docstring; summary:

Prompt instructions are treated as advisory only. Any SQL generated by the
LLM is considered untrusted input and must pass the validation layer before
execution. This prevents prompt-injection attempts from bypassing database
protections.

| # | Check | Catches |
|---|---|---|
| 1 | Single-statement only | stacked-query injection (`...; DROP TABLE...`) |
| 2 | No SQL comments | `--`/`/* */` used to smuggle or truncate statements |
| 3 | Keyword denylist | `DELETE/UPDATE/INSERT/DROP/ALTER/ATTACH/PRAGMA/...` |
| 4 | `sqlglot` AST parse, must be `SELECT` | anything the regex missed; handles `WITH` CTEs correctly |
| 5 | Table allow-list vs. live schema | reaching `sqlite_master`, attached DBs, made-up tables |
| 6 | **Column allow-list**, alias-resolved | a hallucinated column (`SELECT fake_col FROM customers`) |
| 7 | `LIMIT` injected/clamped to `MAX_ROWS` (200) | one query dumping an entire table |
| 8 | Read-only connection: `mode=ro` **+** `PRAGMA query_only` | any write that slipped past 1–6 |
| 9 | Execution watchdog timeout | a runaway/cartesian-join query |

`tests/test_validator.py` proves each of these against real payloads —
including the column check against `SELECT fake_column FROM customers`,
a join that references a real column on the wrong table, and CTE edge
cases (a renamed CTE output column is trusted; a hallucinated column
*inside* a CTE body is still rejected).

---

## Verification

Gemini was run live, end-to-end, with a real `GEMINI_API_KEY`, through the
actual running app (not a test fake). Sample run (a) below is that real
response, unedited apart from formatting: the SQL shows Gemini's own
aliasing choices (`customer_name`, `total_revenue`) rather than the
stand-in's fixed template aliases. The API response itself doesn't expose
which engine answered (kept out of the response on purpose — see
"Architecture" — but logged on every request via `pipeline.py`'s
`logger`); the engine that produced each sample run is called out in the
heading and explained below instead. Everything else was also tested for
real:

- **The fallback architecture itself is verified, not just designed**:
  `tests/test_fallback.py` proves (a) a technical failure in the primary
  engine correctly falls back to the stand-in, (b) an explicit "I can't
  answer this" from the primary is *not* overridden, and (c) SQL that
  fails validation after coming from the LLM path triggers exactly one
  retry against the stand-in. The "primary engine succeeds" path is
  covered by sample run (a) itself — a real, unmodified Gemini response.
- **Sample runs (b) and (c) below use the rule-based engine** so the
  validator's safety checks (column allow-listing, write-attempt refusal)
  are demonstrated deterministically, without depending on network access
  or LLM non-determinism at read time. Both paths go through the exact
  same validation/execution/response-shaping code regardless of which
  engine produced the SQL — see "Architecture".

---

## Sample runs

### (a) Normal question — answered live by Gemini
```json
POST /ask  {"question": "Who are our top 5 customers by revenue?"}
```
```json
{
  "question": "Who are our top 5 customers by revenue?",
  "sql": "SELECT c.name AS customer_name, SUM(s.amount) AS total_revenue FROM customers AS c JOIN sales AS s ON c.id = s.customer_id GROUP BY c.name ORDER BY total_revenue DESC LIMIT 5",
  "tables_used": ["customers", "sales"],
  "result": [
    {"customer_name": "HCL Infosystems", "total_revenue": 488000},
    {"customer_name": "Flipkart Wholesale", "total_revenue": 376000},
    {"customer_name": "Croma Retail", "total_revenue": 334000},
    {"customer_name": "Reliance Digital", "total_revenue": 311500},
    {"customer_name": "Govt IT Department", "total_revenue": 228000}
  ],
  "answer": "HCL Infosystems leads with a total revenue of 488,000. 5 results are shown."
}
```
Note the SQL itself is evidence this came from Gemini, not the stand-in:
the column aliases (`customer_name`, `total_revenue`) are Gemini's own
phrasing — the stand-in's hardcoded template for this exact question
always aliases the columns `name` / `revenue`. (The server log for this
request reads `Answered ... using engine=gemini-2.5-flash`; see
"Architecture" for why that detail is logged rather than returned in the
response body.)

### (b) Grouping / total question — rule-based fallback (deterministic demo)
```json
POST /ask  {"question": "What is the total sales amount by region?"}
```
```json
{
  "question": "What is the total sales amount by region?",
  "sql": "SELECT c.region, SUM(s.amount) AS revenue FROM customers AS c JOIN sales AS s ON s.customer_id = c.id GROUP BY c.region ORDER BY revenue DESC LIMIT 200",
  "tables_used": ["customers", "sales"],
  "result": [
    {"region": "West", "revenue": 899900.0},
    {"region": "South", "revenue": 755100.0},
    {"region": "East", "revenue": 376000.0},
    {"region": "North", "revenue": 228000.0}
  ],
  "answer": "West leads with a revenue of 899,900. 4 results are shown."
}
```

### (c) Correctly refused (write/delete attempt)
```json
POST /ask  {"question": "Delete all sales records for Croma Retail"}
```
```json
{
  "question": "Delete all sales records for Croma Retail",
  "sql": null,
  "tables_used": [],
  "result": [],
  "answer": "I can't answer that: This service only answers read-only questions. Modifying or deleting data is not permitted."
}
```
This is blocked twice over: write-intent is detected before SQL generation
even starts (above), **and** independently, `tests/test_validator.py`
proves a raw `DELETE`/`DROP`/stacked-statement/comment-smuggled SQL string
is rejected by the validator even if it somehow reached that layer directly.

---

## Assumptions

- The four tables are the entire schema in scope; one API key = full access
  (single internal non-technical user, not multi-tenant).
- "Revenue"/"sales amount" = `sales.amount` (already the line-item total).
- `employees` has no FK to the other tables, so employee questions
  (headcount, salary, department) are answered standalone.
- Ambiguous/ungrounded questions are refused with an explanation rather
  than answered with a best-effort guess.

## Limitations & future improvements

1. **Multi-turn/follow-up questions** — every request is stateless today.
2. **Per-client API keys + rate limiting** — one global key currently.
3. **LLM-phrased answers** — `answer.py` is template-based by design (no
   second model call on the critical path); a production version could
   safely use an LLM to phrase the *already-validated* result for more
   natural multi-column summaries, since that step carries no SQL-injection
   risk.
4. **Fuzzy entity matching** in the stand-in (e.g. "Croma" without "Retail").
   Relatedly, the stand-in builds its `WHERE name = '...'` clause by
   substring-matching the question against real values already in the
   database (never raw user input), so it isn't SQL-injectable — but it
   also isn't escaped, so a name containing a literal `'` would currently
   produce invalid SQL (caught gracefully as a normal execution error, not
   a crash or security issue, just a wrong answer). No name in this demo
   database contains one; a production version should still parameterize
   this rather than rely on that being true.
5. **`UNION` queries are rejected outright** by the validator (single
   `SELECT` AST only) — deliberately conservative for this scope.
6. **Column-validation scope is global per-query**, not per-CTE-scope; fine
   for this schema's simple queries, would need real scope resolution for
   deeply nested subqueries.
7. **Query understanding is limited by the capabilities of the underlying
   LLM**; highly complex analytical requests may require prompt refinement
   or additional schema context.

## Tools and resources used

- Built with **Claude** (Anthropic) as a coding assistant for scaffolding,
  the `sqlglot` validator design, and this README — all code was run and
  tested locally before inclusion (see "Verification" and `pytest` output).
- **`sqlglot`** for real SQL parsing (not regex-only) in the validator.
- **FastAPI** + its OpenAPI docs (`/docs`) for the API layer.
- Plain `sqlite3` — no ORM, given the small fixed read-only schema.

**One approach rejected:** an LLM call to phrase the final answer. Skipped
on the critical path for latency/cost/reliability reasons (see Limitation
#3) — it's the one improvement that's safe to add later precisely *because*
it operates on already-validated, trusted data rather than generating SQL.
