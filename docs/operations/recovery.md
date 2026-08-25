# Security incident and recovery runbook

## Purpose and limits

This runbook covers containment and recovery for the single-deployment,
single-project control plane. It applies to PostgreSQL evidence, authentication
material, provider credentials, worker execution, migrations, container images,
local artifacts, telemetry, and continuous integration.

This repository does not schedule backups, provide encrypted backup storage, or
operate a standby. Consequently it claims neither a recovery point objective
nor a recovery time objective. An operator may define those objectives only
after deploying an external backup system and measuring repeated restore
drills. A successful health check is not proof that evidence was restored
correctly.

Never place credentials, prompts, outputs, SQL, rows, canonical payloads, raw
exceptions, or unredacted scanner findings in an issue, chat, ticket, shell
history, or incident timeline.

## Roles

- **Incident lead:** owns severity, containment decisions, and the event
  timeline.
- **Operations owner:** isolates deployments, rotates credentials, restores
  PostgreSQL, and rebuilds containers.
- **Application owner:** validates migrations, job state, evidence integrity,
  authorization, and safe telemetry.
- **Security reviewer:** assesses exposure, supply-chain indicators, required
  notifications, and safe return to service.

One person may perform multiple roles for a local deployment, but record which
checks were independently reviewed.

## Severity guide

- **Critical:** confirmed credential disclosure, unauthorized evidence access,
  malicious dependency or Action execution, database compromise, or active
  public exposure without authentication.
- **High:** probable disclosure, authorization bypass, untrusted image in
  service, destructive migration, unrecoverable queue corruption, or telemetry
  containing evaluation content.
- **Moderate:** bounded availability loss, worker crash loop, failed migration
  with an intact source database, or dependency finding without evidence of
  exploitation.

When evidence is incomplete, choose the higher severity until containment
establishes the boundary.

## First response

1. Record UTC detection time, affected deployment identity, observed safe error
   codes, image digests, source commit, and who has access. Do not copy sensitive
   payloads into the record.
2. Stop new ingress at the trusted edge. For a local Compose deployment, keep
   the API bound to loopback and stop the API if unauthorized access is
   suspected.
3. Stop workers when execution could leak data, repeat unsafe external effects,
   or consume a compromised dependency. Stopping workers leaves durable jobs in
   PostgreSQL; expired leases can be recovered after the deployment is trusted.
4. Preserve relevant logs using the normal redacted telemetry export. Restrict
   raw database or volume snapshots as sensitive evidence.
5. Identify the isolation unit. Because one deployment represents one project,
   suspected cross-project access means the deployments were incorrectly
   sharing infrastructure and every shared deployment must be contained.
6. Rotate exposed authentication, provider, database, CI, and registry material
   from a trusted workstation. Revoke old material before resuming service.

Do not destroy the only recoverable database or backup while investigating.

## Credential compromise

### API authentication material

1. Disable ingress or the affected principal.
2. Generate a replacement bearer credential using the exact `cpk_` prefix and
   43 URL-safe characters through the deployment secret manager.
3. Replace the configured SHA-256 digest, preserve only the minimum required
   scopes (`control-plane:read`, `control-plane:write`,
   `control-plane:cancel`, or `observability:read`), and recreate API processes
   so they no longer retain the old configuration.
4. Revoke the previous value.
5. Verify unauthenticated, malformed, revoked, wrong-`X-Project-ID`, read-only,
   cancellation, observability, and mutation requests produce the intended safe
   status without echoing credential content or its digest.
6. Review safe audit events for affected operation names and request IDs; do not
   search by placing the credential value in a command.

### PostgreSQL credential

Changing the password file alone does **not** rotate an existing PostgreSQL
role. The database role password and every mounted client reference must change
as one controlled operation.

1. Block API and worker access or place the database behind a temporary network
   deny rule.
2. Rotate the role through an administrator channel that does not expose the new
   value in process arguments, shell history, or logs.
3. Atomically replace the secret-manager version or protected password file.
4. Recreate migration, API, and worker containers; do not merely restart a
   process that may retain an engine pool.
5. Confirm the old credential is rejected and the new connection reports the
   exact expected Alembic revision.
6. If database access may have been unauthorized, restore into an isolated new
   database rather than trusting the original in place.

### Provider credential

1. Revoke the provider key at the provider before resuming workers.
2. Review provider-side usage and idempotency records for repeated or unexpected
   calls.
3. Provision a least-privilege replacement under the secret manager.
4. Recreate only the worker deployments that require it.
5. Mark accuracy, latency, token, or cost evidence produced during the exposure
   window as untrusted until independently reproduced.

## Evaluation-data disclosure

1. Contain API, database, backup, artifact-store, and telemetry access together;
   complete evidence can exist in each.
2. Determine which datasets, jobs, runs, decisions, case identifiers, and time
   ranges were exposed using safe metadata queries.
3. Do not paste the affected content into the incident record. Use immutable
   internal evidence references with restricted access.
4. Rotate credentials that appeared in evaluation content even if the product
   forbids storing them; a policy violation does not make the value safe.
5. Remove disclosed data only under an approved retention and legal process.
   Evidence records are intentionally immutable and should not be edited ad hoc.
6. Re-run sentinel privacy tests before restoring ingress.

## Supply-chain compromise

Treat an unexpected Action, package, build backend, release binary, scanner,
base image, or vulnerability database as potentially hostile.

1. Stop affected workflows and deployments. Preserve commit IDs, full Action
   SHAs, package versions and hashes, image manifest digests, and workflow run
   IDs.
2. Identify every credential and token visible to the affected job. The security
   workflows intentionally receive no repository secrets; CodeQL receives only
   `contents: read` and `security-events: write`. Rotate any exposed token
   anyway when malicious execution is confirmed.
3. Compare the dependency lock, Action source, nested composite Action pins,
   release-archive checksum, and image digest with a trusted upstream advisory.
4. Pin a reviewed safe artifact. Never respond by moving to `latest`.
5. Rebuild from a clean checkout on a clean runner with an empty build cache.
6. Run dependency, static, secret-history, container, CodeQL, migration,
   PostgreSQL integration, and release gates before deployment.
7. Reproduce evidence generated by the compromised component. Do not promote a
   prior run solely because its content digest still validates.

The March 2026 Trivy supply-chain incident is the reason the container gate uses
a full Action commit SHA, a full-SHA nested setup Action, an explicit post-event
tool version, and no deployment secrets.

## PostgreSQL restore procedure

Backups contain canonical worker payloads and complete evidence. Store them
encrypted, restrict access, and restore only into an isolated database created
for this deployment.

1. Select a backup created before the incident or failure. Record its immutable
   storage identity and cryptographic checksum without exposing its contents.
2. Create a fresh PostgreSQL database and fresh least-privilege application
   credential. Do not restore over the source database.
3. Restore using the database tool and version supported by the backup format.
   Capture only sanitized exit status and counts.
4. Point a one-shot migration process at the isolated restore. Inspect the
   current Alembic revision before changing it.
5. Apply only committed migrations up to the exact application revision. Fail
   closed if the restored schema is newer than the application.
6. Run the repository health and schema checks.
7. Validate canonical documents and content digests for datasets, completed
   runs, and release decisions. Compare safe record counts and identifiers with
   the backup manifest or pre-incident inventory.
8. Inspect nonterminal jobs and attempts. Do not manually copy active lease
   tokens. Once trusted workers resume, expired leases are reaped and recovered
   through the normal fenced protocol.
9. Exercise an isolated synthetic submission, claim, heartbeat, completion,
   cancellation, and terminal idempotent replay. Do not use customer data for
   the validation probe.
10. Switch the application secret reference and network endpoint to the restored
    database during a controlled maintenance window.
11. Keep the original database isolated and read-only until the incident lead
    approves retention or destruction.

## Migration failure

1. Stop API and workers; do not let a mixed application/schema version serve
   traffic.
2. Record the source commit, application image digest, previous revision,
   attempted revision, and sanitized migration failure class.
3. Preserve or verify a pre-migration backup.
4. Prefer correcting a forward migration and testing it on a restored copy.
   Downgrade only when the migration explicitly supports it and the downgrade
   has passed the repository's round-trip tests.
5. Run `alembic check`, upgrade/downgrade tests, repository integration tests,
   and readiness validation against the isolated copy.
6. Resume migration, API, then workers in that order.

Never edit the Alembic version table to force readiness.

## Worker crash storm or queue failure

1. Stop workers while leaving PostgreSQL and API inspection available to an
   authorized operator when safe.
2. Check database health, schema revision, nonterminal status counts, attempt
   exhaustion, lease expiry, and safe failure codes. Do not emit payloads,
   worker IDs, or lease tokens.
3. Correct the worker image, provider dependency, configuration, or database
   condition before scaling back up.
4. Start one worker and verify heartbeat and fenced completion with synthetic
   work.
5. Scale gradually. The reaper will reschedule expired attempts within the
   stored attempt budget and fail exhausted jobs with a safe code.
6. Review external provider calls for duplicates. Database fencing prevents
   duplicate evidence publication, not repeated external effects.

Do not bulk-update job states or manufacture lease tokens as a recovery shortcut.

## Telemetry incident

1. Disable the affected exporter or collector route without enabling verbose
   application logging.
2. Restrict and preserve the affected telemetry store according to incident
   policy.
3. Identify the disallowed attribute and every emission path using a synthetic
   sentinel, never real exposed content.
4. Remove the attribute at the allowlist boundary and add a regression test for
   logs, metrics, traces, and errors as applicable.
5. Rotate any credential that entered telemetry.
6. Apply retention deletion through the telemetry platform and verify replicas
   or exports according to its documented lifecycle.

## Return-to-service checklist

- [ ] Ingress is TLS-protected at a trusted edge for any non-loopback use.
- [ ] Authentication and single-project authorization checks pass.
- [ ] Old credentials are revoked and new values exist only in protected secret
      references.
- [ ] Database connectivity and exact Alembic revision pass readiness.
- [ ] Restored canonical evidence and digests validate.
- [ ] Dependency, static, secret-history, container, and CodeQL checks pass.
- [ ] PostgreSQL integration, worker recovery, API, and release gates pass.
- [ ] Logs, metrics, traces, health responses, and errors contain no sentinel
      values.
- [ ] One synthetic job completes through the normal leased-worker path.
- [ ] Rate limits and abuse controls are active at the edge before public use.
- [ ] Incident lead and security reviewer approve reopening.

## Backup and restore drill

Run a drill only against synthetic data and an isolated database:

1. Populate datasets, a completed run, a release decision, queued work, and
   attempt history through supported interfaces.
2. Create an encrypted logical backup and a safe manifest containing the source
   revision, record counts, public identifiers, and evidence digests.
3. Restore into a newly created database with a new credential.
4. Follow the full PostgreSQL restore procedure above.
5. Compare the safe manifest, validate immutable evidence, and exercise worker
   recovery.
6. Record elapsed detection, restore, validation, and service-recovery times.

Only measured drills can support an operator-defined recovery point or recovery
time objective. Until then, both remain explicitly unspecified.

## After-action review

Document the detection gap, containment decision, affected assets, root cause,
control that failed, evidence used, recovery validation, and assigned follow-up
owner. Use safe identifiers rather than sensitive payloads. Update this runbook,
the threat model, tests, and monitoring when the incident reveals a new boundary
or failure mode.
