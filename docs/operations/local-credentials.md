# Local credential setup

This walkthrough is for a **new local, single-project deployment** on macOS or
Linux. It does not enable hosted access, contact a model provider, or configure
the public demo. Run commands from the repository root after `uv sync --locked`.
Use a trusted interactive terminal, with screen recording and terminal-session
logging disabled. Never send credentials in chat or paste them into a command,
`.env`, tracked file, or screenshot.

For an existing deployment, keep its project ID and authentication registry.
Do not replace it with this starter configuration: doing so revokes omitted
principals. Use the [credential rotation procedure](recovery.md#api-authentication-material)
instead. The bootstrap below refuses to overwrite an existing configuration.

## 1. Save two distinct credentials in your secret manager

Create two independently generated random credentials. Each value must contain
the exact `cpk_` prefix followed by **43 characters** drawn only from letters,
digits, `_`, and `-`. Use your secret manager's random generator, not a memorable
phrase or a repeated character. Save the complete values as separate entries:

| Entry | Principal | Scopes | Use |
| --- | --- | --- | --- |
| Local dashboard reader | `dashboard-reader` | `control-plane:read` | Browse history, jobs, and release evidence |
| Local operator | `local-operator` | `control-plane:read`, `control-plane:write` | Register the starter suite, submit runs/comparisons, inspect their status |

The reader cannot submit work. Neither credential can cancel jobs or read
`/metrics`; those require separately provisioned `control-plane:cancel` and
`observability:read` permissions. The operator has project-wide write access,
not a credential restricted to one run or comparison. The dashboard uses it
for one explicit request, but that does not make the credential single-use.

These are application credentials, not paid model-provider API keys. No provider
account or paid API access is needed. The raw values stay in your secret manager;
they cannot be recovered from the server's digest file.

## 2. Create the digest-only authentication file

Choose one non-secret project ID. The example uses `local-eval`; edit only that
assignment if needed. IDs allow letters, digits, `.`, `_`, `:`, and `-`, must
start with a letter or digit, and have at most 128 characters. Use the exact
same ID in the dashboard, starter script, and `X-Project-ID` request header.

Paste each credential **only at its hidden prompt**, not inside the code below.
The command validates the values and configuration, refuses symlinked secret
directories and existing output files, and writes only SHA-256 digests. If the
terminal cannot hide input, it fails instead of falling back to echoed input.

```bash
uv run --locked python - <<'PY'
import getpass
import json
import os
import warnings
from pathlib import Path

from llm_eval_control_plane.api.security import (
    AuthenticationConfiguration,
    digest_token,
)

project_id = "local-eval"  # Non-secret; choose once for this deployment.
directory = Path(".secrets")
target = directory / "control-plane-auth.json"

try:
    os.umask(0o077)
    if directory.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        reader_digest = digest_token(getpass.getpass("Dashboard reader credential (hidden): "))
        operator_digest = digest_token(getpass.getpass("Local operator credential (hidden): "))
    configuration = AuthenticationConfiguration.model_validate_json(json.dumps({
        "schema_version": "control-plane-auth/v1",
        "project_id": project_id,
        "principals": [
            {
                "principal_id": "dashboard-reader",
                "token_digest": reader_digest,
                "scopes": ["control-plane:read"],
            },
            {
                "principal_id": "local-operator",
                "token_digest": operator_digest,
                "scopes": ["control-plane:read", "control-plane:write"],
            },
        ],
    }), strict=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(configuration.model_dump_json(indent=2) + "\n")
except (ValueError, OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
    raise SystemExit(
        "Setup stopped. Check credential format, distinct values, project ID, "
        "directory permissions, and whether a configuration already exists. "
        "Use an interactive terminal; never print the credentials for diagnosis."
    ) from None

print("Created .secrets/control-plane-auth.json with two credential digests.")
PY
```

The directory is mode `0700`, and the new file is mode `0600` on POSIX. Both
remain outside Git through `.gitignore`; this is not a substitute for protecting
your workstation. The command does not create a plaintext credential file or
change a running service. Do not use `cat` to inspect authentication material or
force-add `.secrets/` to Git. If interrupted during a write, a partial file may
remain; inspect it privately before deciding how to recover, rather than
blindly rerunning or overwriting it.

## 3. Start Compose and use the correct credential

Create the **separate PostgreSQL password file** and start the stack using the
[Compose quickstart](local-control-plane.md#local-compose-quickstart). Do not
reuse an API credential as a database password. The Compose instructions make
the bind-mounted secret files readable by the fixed non-root container user;
keep the enclosing `.secrets/` directory owner-only. Do not loosen its permissions
or publish the API outside loopback.

Register the one-case suite in an interactive terminal:

```bash
uv run --locked python scripts/seed_local_starter.py --project local-eval
```

Use your chosen project ID instead of `local-eval` if you changed it. Enter the
**operator** credential at the hidden prompt. This registers synthetic data and
the suite; it does not start an evaluation.

Then follow [Run locally](../../dashboard/README.md#run-locally):

1. Open the local development dashboard and choose **Use local live data**.
2. Enter the same project ID and the **reader** credential to browse.
3. Enter the **operator** credential only in the explicit run/comparison submit
   form. Keep the reader as the browsing credential; submission does not upgrade it.
4. Refresh job status and review the decision using the reader session. Choose
   **Disconnect** when finished and clear any clipboard copy of a credential.

Follow the [starter workflow](../../dashboard/README.md#run-the-smallest-offline-suite)
for the baseline, candidate, and comparison steps. The public Vercel demo has no
credential fields and must never receive either value.

## Troubleshooting without exposing credentials

- **Setup stops:** values must be distinct and have the exact format; the
  project ID must be valid. Existing configuration is deliberately not replaced.
- **401:** check that the intended credential is saved and its digest is in the
  loaded registry. Never include the value or its digest in logs or a support post.
- **403:** check the exact project ID and required scope. A reader's write request
  being refused is expected. Reconnect after fixing access; the dashboard clears
  its session on authorization failure.
- **Changed registry not applied:** API processes load it at startup. After a
  reviewed rotation, recreate the API container as described in the recovery
  runbook; do not reset the database or broaden scopes to work around a denial.
- **Database authentication fails:** API credentials cannot fix it. Changing the
  password file alone does not rotate an existing PostgreSQL role; follow the
  [database credential procedure](recovery.md#postgresql-credential).
