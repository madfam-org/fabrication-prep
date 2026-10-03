# Operator setup — the steps only the owner can take

> **Boundary note (public-safe).** These are the public steps and their order. Hostnames of internal services,
> store paths, account identifiers, client identifiers and every credential stay in the private operations
> repository; placeholders in `<angle brackets>` stand for them. Never paste a secret into a chat, an issue, a PR
> or this file.

Nothing below has been done yet. Order: 1 → 2 → 3 → 4 → 5 → 6 → 7; step 8 is the go-live decision.

## 1. Publish the repository

`madfam-org/fabrication-prep` exists, is public and empty. From the local clone that holds `main` (one skeleton
commit) and `feat/service-v1`:

```bash
git push -u origin main
git push -u origin feat/service-v1
gh pr create --repo madfam-org/fabrication-prep --base main --head feat/service-v1 --draft \
  --title "feat: fabrication-prep v1 — slicing service (OrcaSlicer CLI, versioned profiles, signed artifacts)" \
  --body-file <PR body file>
gh pr checks --repo madfam-org/fabrication-prep feat/service-v1 --watch
```

The first CI run is also the first build of the three images and the Linux proof of the slicer contract
(`selftest-slice` for both targets in the worker image). Recommended: protect `main` (require the CI checks and a
review).

## 2. Janua — audience, scope, one client (pravara → fabrication-prep)

A Janua PR (platform-admin territory). In `apps/api/app/core/reserved_oauth_boundaries.py`:

```diff
 ECOSYSTEM_SERVICE_AUDIENCES: frozenset[str] = frozenset(
-    {"karafiel-api", "dhanam-api", "yantra4d-api", "pravara-api", "asset-shells-api"}
+    {"karafiel-api", "dhanam-api", "yantra4d-api", "pravara-api", "asset-shells-api", "fabrication-prep-api"}
 )
+
+#: fabrication-prep (slicing) scope (audience ``fabrication-prep-api``). Jobs belong to the calling
+#: client (``sub``); slicing carries no tenant data, so the client is platform-admin.
+FABRICATION_PREP_SCOPES: frozenset[str] = frozenset({"fabrication-prep:slice"})
```

Add `FABRICATION_PREP_SCOPES` to `RESERVED_SCOPES` the same way the asset-shells scopes are added. Also add a row
in `docs/service-tokens.md`, the pinned members in `tests/unit/core/test_reserved_oauth_boundaries.py`, and one
entry in `SERVICE_CLIENTS` of `apps/api/scripts/seed_service_clients.py`:

```python
# one more element of the SERVICE_CLIENTS list
{
    "name": "pravara-fabrication-prep-slicer",
    "description": (
        "Pravara MES dispatcher -> fabrication-prep: submits slice jobs for render bundles and reads their "
        "results (signed artifact URLs). Platform-admin: slice jobs carry no tenant."
    ),
    "audience": "fabrication-prep-api",
    "redirect_uris": [],
    "allowed_scopes": ["fabrication-prep:slice"],
    "grant_types": ["client_credentials"],
    "is_confidential": True,
    "organization_id": None,
}
```

After that PR is merged and Janua is promoted, as a platform admin:

```bash
cd apps/api && python scripts/seed_service_clients.py   # DATABASE_URL per the private Janua recipe
```

The `client_secret` is printed once. Hand the pair to pravara-mes's runtime through the secret intake, never
through chat:

```bash
enclii secrets intake submit <pravara-mes target> --reason "fabrication-prep slice client"
# keys: FABRICATION_PREP_CLIENT_ID, FABRICATION_PREP_CLIENT_SECRET (masked prompt)
```

pravara-mes also needs `FABRICATION_PREP_API_URL=https://fabrication-prep-api.madfam.io`, which is not a secret.
Client ids are never committed to a public repository.

## 3. Object storage — a private bucket

Artifacts (G-code, sliced 3MF, slicer-variables.json) live in an S3-compatible bucket; Cloudflare R2 is the
expected backend. In the Cloudflare dashboard (or the private IaC):

1. Create bucket `<artifact bucket>`. Give it **no public access, no custom domain and no r2.dev URL**: bytes
   leave only through the API's signed URLs (ADR-014).
2. Create an R2 API token with **Object Read & Write on that bucket only**. Note the access key id, the secret and
   the account's S3 endpoint (`https://<account id>.r2.cloudflarestorage.com`). They go into step 4's secrets
   file, never into a repository.
3. Retention is an owner decision. Artifacts are content-addressed and referenced by jobs, so a lifecycle rule
   that expires objects after N days is safe once pravara has copied what the passport needs. v1 ships no
   garbage collector.

## 4. Enclii — project, database, secrets, domains

```bash
enclii onboard --repo madfam-org/fabrication-prep --project fabrication-prep \
  --manifest-path infra/k8s/production --preflight --dry-run
```

Prepare the passwords, the URL-signing key and the secrets file in a private, short-lived location:

```bash
umask 077
OWNER_PW=$(openssl rand -hex 24); APP_PW=$(openssl rand -hex 24)
URL_KEY="k$(date +%Y%m):$(openssl rand -base64 32)"   # kid:base64 of 32 random bytes
printf '%s\n' \
  "DATABASE_URL=postgresql://<owner role>:${OWNER_PW}@<pooler host>:<port>/fabrication_prep" \
  "APP_DATABASE_URL=postgresql://fabrication_prep_app:${APP_PW}@<pooler host>:<port>/fabrication_prep" \
  "ARTIFACT_URL_KEYS=${URL_KEY}" \
  "INPUT_ALLOWED_HOSTS=<yantra4d API host>" \
  "S3_ENDPOINT_URL=https://<account id>.r2.cloudflarestorage.com" \
  "S3_BUCKET=<artifact bucket>" \
  "S3_ACCESS_KEY_ID=<R2 access key id>" \
  "S3_SECRET_ACCESS_KEY=<R2 secret>" > ./fabrication-prep.env
enclii onboard --repo madfam-org/fabrication-prep --project fabrication-prep \
  --manifest-path infra/k8s/production --db-name fabrication_prep --db-password "$OWNER_PW" \
  --secrets-file ./fabrication-prep.env --preflight
rm -f ./fabrication-prep.env; unset URL_KEY
```

This creates:
- the namespace (default-deny);
- the database and its owner role, and the pooler entry;
- the Secret `fabrication-prep-credentials`, holding the eight keys above under the names the Deployments read;
- from `enclii.yaml`, the tunnel routes and DNS records for `fabrication-prep.madfam.io` (landing) and
  `fabrication-prep-api.madfam.io` (API). The worker gets no route.

Onboarding is not idempotent; repairs go through `enclii onboard ensure`.

`INPUT_ALLOWED_HOSTS` is a comma-separated list of the hosts that render bundles are fetched from: the yantra4d
API host, which serves `/static/…` renders and their `.variables.json` sidecars. An empty list refuses every input.

## 5. The runtime database role

The API and the worker connect as a NON-owner role that row-level security applies to. The migration grants it
exactly what it needs and fails visibly if the role does not exist. Through the platform's database
administration path, connected to `fabrication_prep` as an administrator, with `APP_PW` from step 4:

```bash
# psql interpolates :'app_pw' only in script input (stdin or -f), not in -c commands.
psql "<admin connection to fabrication_prep>" -v ON_ERROR_STOP=1 -v app_pw="$APP_PW" <<'SQL'
CREATE ROLE fabrication_prep_app LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE
  CONNECTION LIMIT 8 PASSWORD :'app_pw';
GRANT CONNECT ON DATABASE fabrication_prep TO fabrication_prep_app;
SQL
unset OWNER_PW APP_PW
```

Then add `fabrication_prep_app` to the connection pooler's user list. This is a platform step; until it is done,
`/ready` answers 503, which is the expected signal, not a bug.

`CONNECTION LIMIT 8` covers the API pool (3), the worker pool (2) and headroom for a second worker replica. Raise
it deliberately with the replica count: the shared Postgres budget is 100 connections.

## 6. Build and deploy wiring (owner decision)

This repository has no build-and-deploy workflow, on purpose. To enable deploys, add
`.github/workflows/build-deploy.yml` calling the shared Enclii reusable workflow at a pinned tag, with three
services:

```yaml
services: |
  [
    {"name":"fabrication-prep-api", "dockerfile":"Dockerfile", "paths":"fabrication_prep pyproject.toml Dockerfile"},
    {"name":"fabrication-prep-worker", "dockerfile":"Dockerfile.worker", "paths":"fabrication_prep pyproject.toml Dockerfile.worker"},
    {"name":"fabrication-prep-web", "dockerfile":"web/Dockerfile", "paths":"web"}
  ]
```

The workflow builds, signs and pins the digests in `infra/k8s/production/kustomization.yaml`, replacing the
all-zero placeholders; ArgoCD then syncs. The worker image is large, because OrcaSlicer is roughly 0.5 GB
extracted. Flip `autoDeploy` in `enclii.yaml` only when promotion is ruled.

## 7. Smoke after the first deploy

```bash
curl -fsS https://fabrication-prep.madfam.io/health
curl -fsS https://fabrication-prep-api.madfam.io/health
curl -fsS https://fabrication-prep-api.madfam.io/ready            # 503 until steps 4-5 are complete
```

The worker pod becomes Ready when its heartbeat is fresh. It refuses to start, and logs why, when the CLI version
differs from the profiles' pinned version.

For an end-to-end job, use a token for the pravara client (Janua client_credentials, audience
`fabrication-prep-api`, scope `fabrication-prep:slice`):

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" https://fabrication-prep-api.madfam.io/v1/profiles | jq '.profiles | length'
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "Idempotency-Key: smoke-$(date +%s)" --data @<job json with a real render URL and its sha256> \
  https://fabrication-prep-api.madfam.io/v1/slice-jobs
```

Then poll `GET /v1/slice-jobs/<id>` until it reports `succeeded`. Download `output.url` and check that its
`sha256sum` equals `output.sha256`.

Nothing here prints or moves a machine; dispatching the output to a printer is pravara's step (MES-1 §6).

## 8. Operations notes

- **Signing-key rotation:**
  1. Prepend a new `kid:key` to `ARTIFACT_URL_KEYS`. The list is comma-separated and the first key signs.
  2. Roll the API.
  3. Wait at least `ARTIFACT_URL_TTL_SECONDS` (900 s), then remove the old key.
- **Dead letters:** a job in `dead_lettered` exhausted its retries on transient errors (input host down, slicer
  killed, lease lost). Its `error_code` and `error_message` say why. Resubmit with a new `Idempotency-Key`.
- **Slicer upgrade:**
  1. Bump `ORCASLICER_VERSION` and its sha256 in `Dockerfile.worker`.
  2. Rebuild the profiles from that release with `scripts/build_profiles.py`.
  3. Bump the version of every profile whose content changed.
  4. Run the real-slicer tests.

  The worker refuses a CLI whose version differs from `catalog.json`.
