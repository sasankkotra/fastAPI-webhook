# ORDR.ai WordPress webhook

A standalone FastAPI receiver for the independent Cloud Run service
`wordpress-webhook` in project `gen-lang-client-0786534718`. No deployment has
been performed by this repository setup. Deployment commands below are for an
operator to run when ready.

The application uses the **existing** `public.wordpress_form_raw` table. It
contains no migrations, DDL, startup schema changes, WordPress PHP code, or
configuration for other services.

## API and authentication

`POST /webhooks/wordpress/forms` accepts `Content-Type: application/json`:

```json
{
  "formId": "101",
  "postId": "5001",
  "formData": {
    "establishment_name": "Sri Udupi Park",
    "city": "Bengaluru"
  }
}
```

`formId` is a nonblank string of at most 255 characters. `postId` is a separate
identifier with the same constraints; it can be omitted or null. Numbers are
not coerced to strings. `formData` must be a JSON object and can contain nested
objects, arrays, strings, numbers, booleans and nulls. Unknown envelope fields,
duplicate JSON keys, nonfinite numbers, invalid Unicode and PostgreSQL-incompatible
null characters are rejected. The default body limit is 1 MiB.

Every request needs these headers:

| Header | Value |
| --- | --- |
| `X-Webhook-Timestamp` | Decimal Unix time in **seconds**, generated for each delivery attempt |
| `X-Webhook-Signature` | `sha256=` followed by the lowercase, 64-character hex digest |

`WEBHOOK_SECRET` must be **512 hexadecimal characters encoding a 256-byte
(2048-bit) randomly generated key**. Hex-decode it into bytes on both systems.
Compute `HMAC-SHA256(hex_decode(secret), timestamp_ascii + b"." + raw_request_body)`.
Do not use the 512-character text itself as the HMAC key. Sign the exact bytes sent, including whitespace;
do not parse or reserialize the body after signing. The timestamp is part of the
signature and must be within 300 seconds of the receiver's clock in either
direction. Signatures use constant-time comparison. Use HTTPS in production and
synchronize the sender's clock. The timestamp limits replay age; event IDs below
prevent duplicate inserts for replays within or beyond that window.

SHA-256 still produces a **256-bit signature** (64 hexadecimal characters).
The 2048-bit requirement applies to the input key length, not the algorithm's
security strength. HMAC hashes keys longer than its hash block size internally;
this is standard behavior, not 2048-bit cryptographic strength. See
[RFC 2104](https://www.rfc-editor.org/rfc/rfc2104.html).

The parameterized insertion supplies only `form_id`, `post_id` and the complete
`form_data`. PostgreSQL generates the UUID and timestamp through its existing
defaults. JSON nesting and values are retained; JSONB normalizes formatting and
object key order. No uniqueness restriction is imposed on either identifier.
The success response is sent after the transaction commits:

```json
{"id":"8f2eb8be-bf38-496d-8557-3c448dc183ad","created_at":"2026-01-01T00:00:00Z","duplicate":false}
```

| HTTP status | Meaning |
| --- | --- |
| `201` | New row committed |
| `200` | Event already stored; returns its original UUID/time and `duplicate: true` |
| `400` | Malformed or unsupported UTF-8 JSON |
| `401` | Missing/invalid HMAC headers or stale/future timestamp |
| `409` | Event ID reused with different `formData` |
| `413` | Body exceeds configured limit |
| `415` | Content-Type is not application/json |
| `422` | JSON does not match the envelope/field constraints |
| `503` | Database operation could not be confirmed; webhook response includes `Retry-After: 5` |

Authentication is checked before JSON validation. Errors never echo form values,
credentials or SQL. Application logs record event names, generated row IDs and
error classes only. Container access logs are disabled; Cloud Run's own request
logs still apply. `/docs` provides the API schema.

`GET /health` executes `SELECT 1`, returning `200 {"status":"ok"}` or a sanitized
`503` when the database is unavailable. It does not check the table or write data.
Do not use database readiness as a Cloud Run liveness probe.

## Retry protection without schema changes

For reliable retries, the eventual sender should add an optional, unique event
identifier inside its own `formData`, for example:

```json
{"formId":"101","postId":"5001","formData":{"_event_id":"9b339ae1-552b-4657-b444-15077fd91736","city":"Bengaluru"}}
```

The receiver stores this object unchanged. `_event_id` must be a nonblank string
of at most 255 characters; a UUID is recommended. The key is scoped to
`(formId, postId, _event_id)`. A transaction-level PostgreSQL advisory lock
serializes concurrent retries across workers and Cloud Run instances. After
locking, a separate read under READ COMMITTED checks the JSONB field and either
returns the existing row or inserts a new one. Reusing the key with different
data returns `409`; no previous row is overwritten.

Use the **same event ID and data on every retry**, but generate a **new event ID
for each new submission or legitimate update**, even when its identifiers or
contents match an earlier submission. Generate a fresh timestamp and signature
for each attempt. Retry connection errors, timeouts and `5xx` responses with
exponential backoff and jitter. A lost response or failed commit acknowledgement
can leave the commit outcome uncertain; retrying with the same event ID resolves
that uncertainty safely. Correct `4xx` errors before resending.

Without `_event_id`, **every accepted request inserts a row**, including identical
retries. The original payload above remains valid. Identifiers and content hashes
alone cannot distinguish a retry from an intentional repeat. All writers that
need deduplication must use this receiver's locking convention. Existing external
writers, deleted history, or historical duplicate event IDs fall outside that
guarantee. The lookup has no new index and may scan the existing table as it grows;
any future index or separate idempotency storage requires explicit approval.

## Local setup and tests

Requires Python 3.13. Create the environment and edit the placeholder values:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Use an existing database user with `CONNECT` on the database, `USAGE` on `public`,
and `INSERT` and `SELECT` on `public.wordpress_form_raw`. `SELECT` is necessary for
`RETURNING` and retry lookups. UUID generation uses the existing default and needs
no sequence permission. Do not run any schema creation scripts or migrations.
If the existing table is in a different schema, adjust the two SQL queries and
insert in `database.py` before deployment. Row-level security, if enabled, must
allow this user to see its earlier submissions for deduplication to work.

For Cloud SQL locally, authenticate with Application Default Credentials and
run an installed Cloud SQL Auth Proxy against the existing instance:

```bash
gcloud auth application-default login
cloud-sql-proxy --address 127.0.0.1 --port 5432 PROJECT:REGION:INSTANCE
```

Set `.env` to `DB_HOST=127.0.0.1`, `DB_SSLMODE=disable`, the existing database name,
user and password, and a random 2048-bit HMAC key encoded as 512 hexadecimal
characters (generate one with `openssl rand -hex 256`). TLS is disabled only on the local proxy
hop; the proxy authenticates and encrypts the Cloud SQL connection. Private-IP-only
instances also require an existing network route and the proxy's `--private-ip` flag.
No GCP service account key file is needed.

```bash
uvicorn main:app --host 127.0.0.1 --port 8080 --no-access-log
# In another terminal, with the virtual environment active:
curl --fail http://127.0.0.1:8080/health
python scripts/send_sample.py http://127.0.0.1:8080
python -m pytest -q
```

The sample script prompts for the secret, or reads `WEBHOOK_SECRET` from its
environment. It does not load `.env`. Default tests use database doubles and
require no GCP access. They check authentication/tampering, timestamps,
validation, SQL parameters, database failures, health and retry behavior.

The optional integration tests execute actual SQL, concurrent retries and
submission history checks. Set `TEST_DATABASE_DSN` to a **disposable test database
with the same table already provisioned**; never use production. Tests create no
tables. They insert randomly tagged test submissions and delete only those rows
in teardown, so the test user also needs DELETE permission:

```bash
export TEST_DATABASE_DSN='postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/TEST_DATABASE'
python -m pytest -q -m integration
unset TEST_DATABASE_DSN
```

## GCP configuration

Use project `gen-lang-client-0786534718` and a region matching the **existing**
Cloud SQL instance. The instance connection name, database name, user, network
configuration, existing Artifact Registry repository and secret version numbers
must be supplied by the operator. Nothing in the build creates a database,
instance, table, network or registry repository. Reuse an available Docker-format
Artifact Registry repository in the deployment region.

1. Verify Cloud Run, Cloud Build, Artifact Registry, Cloud SQL Admin, Secret Manager
   and Cloud Logging APIs are enabled. An administrator can enable any missing APIs:

   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com logging.googleapis.com --project=gen-lang-client-0786534718
   ```

2. Create a dedicated runtime identity for this new service (once):

   ```bash
   gcloud iam service-accounts create wordpress-webhook --display-name='WordPress webhook runtime' --project=gen-lang-client-0786534718
   gcloud projects add-iam-policy-binding gen-lang-client-0786534718 --member='serviceAccount:wordpress-webhook@gen-lang-client-0786534718.iam.gserviceaccount.com' --role=roles/cloudsql.client
   ```

3. In Secret Manager, create or reuse secrets holding the existing database
   password (`wordpress-webhook-db-password`) and a shared random 2048-bit HMAC key
   (`wordpress-webhook-hmac`, encoded as 512 hexadecimal characters). Store exact values without trailing newlines. Do not
   put secret values in Git, build substitutions, Docker build arguments or plain
   Cloud Run environment settings. Record **numeric version numbers**. Grant the
   runtime identity access to each specific secret:

   ```bash
   gcloud secrets add-iam-policy-binding wordpress-webhook-db-password --project=gen-lang-client-0786534718 --member='serviceAccount:wordpress-webhook@gen-lang-client-0786534718.iam.gserviceaccount.com' --role=roles/secretmanager.secretAccessor
   gcloud secrets add-iam-policy-binding wordpress-webhook-hmac --project=gen-lang-client-0786534718 --member='serviceAccount:wordpress-webhook@gen-lang-client-0786534718.iam.gserviceaccount.com' --role=roles/secretmanager.secretAccessor
   ```

   Cloud Run resolves pinned Secret Manager versions into `DB_PASSWORD` and
   `WEBHOOK_SECRET` at instance startup. No Secret Manager SDK or credentials file
   is needed in the application. To rotate, create new versions, update the build
   substitutions, coordinate the HMAC change with the sender, and deploy a revision.
   Existing running instances retain their old environment values.

   Earlier versions accepted shorter UTF-8 secrets. Those must be replaced with
   a newly generated key in the format above. Coordinate the change with the
   WordPress developer: their signer must hex-decode the stored value before
   calculating HMAC-SHA256. The receiver rejects incorrectly formatted keys at startup.

4. Use a **separate build/deploy service account**, not the runtime account. Give
   it Artifact Registry Writer on the selected repository, Logs Writer on the
   project, Cloud Run Admin for creating the new service and configuring its public
   invoker policy, and Service Account User on the `wordpress-webhook` runtime
   identity. After initial creation, scope Cloud Run access to this service where
   possible. The build account does not need database credentials or Secret Manager
   access. The operator/trigger creator also needs permission to run builds and
   act as the chosen build account. If using a custom account for manual source
   upload, grant it read access to the build's source staging bucket as needed;
   do not grant project-wide Storage Admin by default.

5. The supplied deployment attaches only this new service to the existing Cloud SQL
   instance using `--add-cloudsql-instances`. It connects through
   `/cloudsql/PROJECT:REGION:INSTANCE`, with `DB_SSLMODE=disable` on the local Unix
   socket. The managed Cloud SQL Auth Proxy encrypts/authenticates the remote
   connection. This path requires an instance with public IP connectivity; it
   does not require opening PostgreSQL to the internet or adding authorized networks.
   No existing service or Cloud SQL instance configuration is changed.

   For an instance with **private IP only**, use existing VPC connectivity instead.
   Before deployment, amend only this service's deploy arguments in
   `cloudbuild.yaml`: add `--network=EXISTING_NETWORK`, `--subnet=EXISTING_SUBNET`,
   `--vpc-egress=private-ranges-only`, set `DB_HOST` to the instance's private IP,
   and use `DB_SSLMODE=verify-full` with its trusted CA (`PGSSLROOTCERT` pointing to
   a mounted CA file; use a certificate-matching host name and `PGHOSTADDR` if needed).
   Remove `--add-cloudsql-instances` for direct TCP. Alternatively, use your existing
   approved private Cloud SQL proxy connection pattern. Confirm the existing network
   IAM/firewall routes permit the connection. Do not add public IP, a VPC connector
   or modify instance settings to accommodate this service. The checked-in pipeline
   implements the Unix-socket path and needs these edits for private-only instances.

The default maximum is three instances and five pooled connections each: budget up
to 15 connections, plus temporary overlap during revision rollout. Confirm headroom
in the shared instance before deployment. Concurrency is 20, the pool wait is five
seconds, statements time out after ten seconds, lock waits after five seconds and
Cloud Run requests after 30 seconds. The pool opens lazily, so the container can
start during a database outage and report `503` until connectivity recovers.

## GitHub-triggered deployment

`cloudbuild.yaml` tests, builds the Docker image, pushes it to the selected existing
Artifact Registry repository with `$BUILD_ID` as the image tag, then deploys only
`wordpress-webhook`. The project is checked explicitly before proceeding. It uses
Cloud Logging for build logs with a user-managed build identity. No trigger is
created automatically.

1. Push these files to the new GitHub repository.
2. In Cloud Build, connect that repository using the Cloud Build GitHub app and
   authorize only the intended repository. Create a push trigger for `^main$`,
   select **Cloud Build configuration file**, and use `/cloudbuild.yaml`.
3. Select the dedicated build/deploy service account above. Supply substitutions:

   | Substitution | Value |
   | --- | --- |
   | `_REGION` | Existing Cloud SQL region / registry location |
   | `_AR_REPOSITORY` | Existing Docker-format Artifact Registry repository |
   | `_INSTANCE_CONNECTION_NAME` | `gen-lang-client-0786534718:REGION:EXISTING_INSTANCE` |
   | `_DB_NAME` | Existing database containing `public.wordpress_form_raw` |
   | `_DB_USER` | Existing database login with INSERT and SELECT privileges |
   | `_DB_PASSWORD_SECRET` | `wordpress-webhook-db-password` or existing secret name |
   | `_DB_PASSWORD_VERSION` | Numeric secret version, e.g. `1` |
   | `_WEBHOOK_SECRET_NAME` | `wordpress-webhook-hmac` or existing secret name |
   | `_WEBHOOK_SECRET_VERSION` | Numeric secret version, e.g. `1` |

4. Enable trigger approval if desired; run the first build manually after review,
   then allow subsequent main-branch pushes to deploy. Protect `main` and restrict
   changes to the build configuration because its identity can deploy code.

The deployment allows public invocation so WordPress can reach it without a Google
identity token. **The POST endpoint still requires HMAC authentication.** `/health`
and the API documentation are public. If organization policy forbids public Cloud
Run invocation, agree an authenticated ingress approach before deploying.

For a deliberate one-off build from a local checkout, use the same configuration
after replacing all placeholders; this command **does deploy**:

```bash
gcloud builds submit . \
  --project=gen-lang-client-0786534718 \
  --region=REGION \
  --service-account=projects/gen-lang-client-0786534718/serviceAccounts/BUILD_ACCOUNT_EMAIL \
  --config=cloudbuild.yaml \
  --substitutions=_REGION=REGION,_AR_REPOSITORY=EXISTING_REPOSITORY,_INSTANCE_CONNECTION_NAME=gen-lang-client-0786534718:REGION:EXISTING_INSTANCE,_DB_NAME=EXISTING_DATABASE,_DB_USER=EXISTING_USER,_DB_PASSWORD_VERSION=1,_WEBHOOK_SECRET_VERSION=1
```

## Test the deployed service

Obtain the service URL, check readiness, then send the sample JSON above using the
standard-library signer. It prompts for the same HMAC secret stored in Secret
Manager; never include the secret in the URL or command-line arguments.

```bash
SERVICE_URL="$(gcloud run services describe wordpress-webhook --project=gen-lang-client-0786534718 --region=REGION --format='value(status.url)')"
curl --fail "$SERVICE_URL/health"
python scripts/send_sample.py "$SERVICE_URL"

# Verify retry protection: first response 201, second 200 with the same UUID/time.
EVENT_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
python scripts/send_sample.py "$SERVICE_URL" --event-id "$EVENT_ID"
python scripts/send_sample.py "$SERVICE_URL" --event-id "$EVENT_ID"
```

These are real submissions and remain in the existing table. A request without
the HMAC headers should return `401`. Inspect Cloud Run logs for
`submission_created`, `submission_replayed`, or sanitized database failure events.

## Configuration reference

See `.env.example`. Required: `WEBHOOK_SECRET`, `DB_HOST`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD`. Optional: `DB_PORT` (5432), `DB_SSLMODE` (require),
`DB_POOL_MAX_SIZE` (5), `DB_POOL_TIMEOUT_SECONDS` (5),
`DB_STATEMENT_TIMEOUT_MS` (10000), `SIGNATURE_TOLERANCE_SECONDS` (300),
`MAX_REQUEST_BYTES` (1048576). Uvicorn uses Cloud Run's `PORT` (8080 by default).
Environment variables override `.env`. Production containers exclude `.env`.

Deployment references: [Cloud SQL connectivity](https://docs.cloud.google.com/sql/docs/postgres/connect-run),
[Secret Manager integration](https://docs.cloud.google.com/run/docs/configuring/services/secrets),
[Cloud Build deployment](https://docs.cloud.google.com/build/docs/deploying-builds/deploy-cloud-run),
[repository connections](https://docs.cloud.google.com/run/docs/continuous-deployment).
