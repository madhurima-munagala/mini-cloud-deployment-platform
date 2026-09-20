# Database Setup

Covers only the database layer (7 tables from `DATABASE_DESIGN.md v1.0`). No API endpoints exist yet.

## 1. Required environment variables

Copy `.env.example` to `.env` and fill in real values:

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Main PostgreSQL connection string, e.g. `postgresql+psycopg2://user:pass@localhost:5432/mini_cloud_platform` |
| `TEST_DATABASE_URL` | no (falls back to `DATABASE_URL`) | Separate DB so `pytest` never touches real data |
| `ENCRYPTION_KEY` | yes | Fernet key for `environment_variables.value_encrypted`. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SQL_ECHO` | no (default `false`) | Set `true` to print raw SQL locally for debugging |

`.env` is git-ignored. `.env.example` contains placeholders only.

## 2. Start PostgreSQL

Any local PostgreSQL 13+ instance works. Example with Docker:

```bash
docker run --name mini-cloud-db -e POSTGRES_USER=devuser -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=mini_cloud_platform -p 5432:5432 -d postgres:16
```

Then set `DATABASE_URL=postgresql+psycopg2://devuser:devpass@localhost:5432/mini_cloud_platform` in `.env`.
Create a second database (e.g. `mini_cloud_platform_test`) if you want `TEST_DATABASE_URL` to point somewhere separate.

## 3. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Run migrations

```bash
alembic upgrade head
```

This creates all 7 tables, foreign keys, CHECK constraints, and the partial unique indexes described in `DATABASE_DESIGN.md`.

## 5. Verify the schema

```bash
psql "$DATABASE_URL" -c "\dt"                       # lists all 7 tables
psql "$DATABASE_URL" -c "\d deployments"             # inspect columns/constraints/indexes on one table
```

You should see `uq_deployments_active_per_repository` and the two `uq_env_vars_*` indexes listed under `deployments` / `environment_variables` respectively — those are the partial unique indexes enforcing the concurrency and env-var-uniqueness rules.

## 6. Run the database tests

```bash
pytest
```

The suite connects to `TEST_DATABASE_URL` (or `DATABASE_URL`), creates all tables, runs each test inside a transaction that's rolled back afterward, and drops the tables at the end. If no PostgreSQL instance is reachable, the suite skips with a clear message instead of failing — SQLite is deliberately not used as a substitute, since the partial unique indexes this schema depends on are PostgreSQL-specific.
