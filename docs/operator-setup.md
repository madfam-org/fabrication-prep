# Operator setup — the steps only the owner can take

> **Boundary note (public-safe).** These are the public steps and their order. Hostnames of internal services,
> store paths, account identifiers, client identifiers and every credential stay in the private operations
> repository; placeholders in `<angle brackets>` stand for them. Never paste a secret into a chat, an issue, a PR
> or this file.

Every credential in this service is generated and filed by the platform: the database owner password, the
runtime role's password, the URL-signing key, the bucket token and the Janua client secret. No step below has
anyone generate, type, copy or store a credential, and none prints one. Order: 1 → 7; section 8 holds the
operations notes. Run each command on its own; never paste a dry run and its real run as one block.

## 1. Publish the repository — done

`madfam-org/fabrication-prep` is public; `main` holds the skeleton and draft PR #1 carries `feat/service-v1`.
Its CI is the first build of the three images and the Linux proof of the slicer contract (`selftest-slice` for
both targets in the worker image). Recommended: protect `main` (require the CI checks and a review).

## 2. Janua — the pravara → fabrication-prep client — done

Janua reserves the audience `fabrication-prep-api` and the scope `fabrication-prep:slice`, and the confidential
`client_credentials` client `pravara-fabrication-prep` exists (platform-admin: slice jobs carry no tenant). The
platform provisioner created it and filed its id and secret into pravara-mes's runtime secrets; nobody saw the
secret. To re-run it (idempotent; it converges and never prints the secret):

```bash
enclii secrets provision oidc --profile admin --platform pravara-fabrication-prep
```

pravara-mes also needs `FABRICATION_PREP_API_URL=https://fabrication-prep-api.madfam.io`, which is not a secret
and belongs in its manifests. Client ids are never committed to a public repository.

## 3. Enclii — project, database, roles, generated keys, domains

One command. Switchyard creates everything and generates every value server-side:

```bash
enclii onboard --repo madfam-org/fabrication-prep --project fabrication-prep \
  --manifest-path infra/k8s/production --preflight \
  --db-name fabrication_prep --generate-db-password \
  --db-connection-limit 2 \
  --app-role fabrication_prep_app --app-role-connection-limit 8 \
  --generate-secret ARTIFACT_URL_KEYS
```

This creates:
- the namespace (default-deny);
- the database `fabrication_prep` and its owner role with a generated password, the pooler entry, and the
  owner URL in `fabrication-prep-credentials` under `DATABASE_URL` (read only by the API's migrate init
  container);
- the runtime role `fabrication_prep_app`: no superuser, no `BYPASSRLS`, `NOINHERIT`, `CONNECTION LIMIT 8`,
  `CONNECT` on the database, a generated password, its pooler line, and its URL under `APP_DATABASE_URL`
  (read by the API and the worker). The migration grants it exactly what it needs on first deploy;
- `ARTIFACT_URL_KEYS`: 32 random bytes, unpadded base64url. The service derives the key id from the key;
- from `enclii.yaml`, the tunnel routes and DNS records for `fabrication-prep.madfam.io` (landing) and
  `fabrication-prep-api.madfam.io` (API). The worker gets no route.

To preview first, run the same line with `--dry-run` instead of `--preflight` **as a separate step**: it lists
the roles and key names that would be generated, never values. Re-runs and repairs go through
`enclii onboard ensure` with the same flags; it keeps every existing role and value, and only a `--rotate-*`
flag replaces one. The command exits non-zero when any step did not complete.

`CONNECTION LIMIT 8` covers the API pool (3), the worker pool (2) and headroom for a second worker replica. The
owner role is used only by the one-connection migrate init container, so `--db-connection-limit 2` caps it.
Raise it deliberately with the replica count (`enclii onboard ensure … --app-role fabrication_prep_app
--app-role-connection-limit <n> --rotate-app-role-password`): the shared Postgres budget is 100 connections.

## 4. Object storage — a private bucket

```bash
enclii buckets create fabrication-prep-artifacts --project fabrication-prep
```

This creates the bucket and mints a Cloudflare token scoped to that one bucket (Object Read & Write), and writes
`R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` into
`fabrication-prep-credentials`. Nothing is printed. The Deployments map those keys onto the service's own `S3_*`
settings. Keep the bucket **private: no public access, no custom domain, no r2.dev URL**; bytes leave only
through the API's signed URLs (ADR-014).

### Artifact retention: 30 days

`enclii buckets` has no lifecycle rule, so the worker enforces retention itself (`fabrication_prep/retention.py`):
between jobs, at most once an hour, it deletes the bytes of artifacts that no job has produced for
`ARTIFACT_RETENTION_DAYS` (30, set in the worker Deployment; 0 keeps everything) and marks their rows expired. Rows
and digests stay. A job whose artifacts expired shows `artifacts_expired_at` and no URLs; the download route answers
410. pravara copies the digests it needs when the slice succeeds and fetches the bytes only at dispatch, within
hours, so 30 days is safe for the passport. The bucket token's Object Read & Write permission covers the delete;
a refused delete is logged and retried, never silently skipped.

## 5. Non-secret configuration

`INPUT_ALLOWED_HOSTS` (the hosts render bundles are fetched from: yantra4d's API) is plain configuration in both
Deployments, not a Secret. Changing it is a manifest change in this repository. An empty list refuses every
input.

## 6. Build and deploy

`.github/workflows/build-deploy.yml` calls Enclii's reusable workflow (pinned by commit SHA) for the three images,
signs them and commits the digest pins to `infra/k8s/production/kustomization.yaml`; the GitOps sync then rolls
them out. It runs **only when dispatched** while `autoDeploy` stays `false` (promotion not yet ruled):

```bash
gh workflow run build-deploy.yml --repo madfam-org/fabrication-prep
```

The worker image is about 0.5 GB larger than the API image (OrcaSlicer). Push-on-main is a separate pull request
once promotion is ruled.

## 7. Smoke after the first deploy

```bash
curl -fsS https://fabrication-prep.madfam.io/health
curl -fsS https://fabrication-prep-api.madfam.io/health
curl -fsS https://fabrication-prep-api.madfam.io/ready            # 503 until steps 3-4 are complete and the first migration ran
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

- **Signing-key rotation:** `enclii onboard ensure --repo madfam-org/fabrication-prep --project fabrication-prep
  --manifest-path infra/k8s/production --rotate-secret ARTIFACT_URL_KEYS` replaces the key server-side; the API
  restarts on the Secret change. URLs signed with the old key stop verifying at once, so a client holding one
  (valid for at most `ARTIFACT_URL_TTL_SECONDS`, 900 s) asks for the job again to get a fresh URL. The variable
  also accepts a `kid:base64` list (first signs, all verify) for a staged rotation.
- **Dead letters:** a job in `dead_lettered` exhausted its retries on transient errors (input host down, slicer
  killed, lease lost). Its `error_code` and `error_message` say why. Resubmit with a new `Idempotency-Key`.
- **Slicer upgrade:**
  1. Bump `ORCASLICER_VERSION` and its sha256 in `Dockerfile.worker`.
  2. Rebuild the profiles from that release with `scripts/build_profiles.py`.
  3. Bump the version of every profile whose content changed.
  4. Run the real-slicer tests.

  The worker refuses a CLI whose version differs from `catalog.json`.
