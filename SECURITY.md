# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature for this repository.

Include the affected version, reproduction steps, impact, and any suggested
mitigation. Avoid attaching real credentials, private prompts, customer
documents, database rows, or other sensitive evaluation inputs.

## Data-handling baseline

Evaluation payloads are potentially sensitive. The project follows these rules:

- Credentials are referenced by secret identifiers and are never embedded in
  artifact versions or run specifications.
- Prompts, responses, SQL, rows, documents, and tool arguments are excluded from
  logs, metrics, and traces by default.
- Target outputs are treated as untrusted input and must be validated before
  scoring or display.
- CLI summaries omit case inputs, expected values, target outputs, and raw
  exception text. Output disclosure requires both a case selection and the
  explicit `--include-output` flag.
- Complete run artifacts can contain evaluation content. `.llm-eval/` is ignored
  by Git; POSIX local stores use owner-only `0700` directories and `0600` files.
- Stored runs are create-once, size-bounded, canonicalized, and integrity-checked
  when read. Run identifiers are hashed before use as filenames.
- CI uses deterministic public or synthetic fixtures and does not require paid
  model access.
- Release reports expose bounded artifact identities, aggregate values, slice
  labels, case IDs, and failure codes. They omit inputs, expectations, target
  outputs, exception text, and absolute artifact-store paths.
- `compare --output` creates a new report and refuses to overwrite an existing
  path. Treat reports as internal evidence when case IDs or metric topology are
  sensitive.
- Live evaluation is an explicit mode with separate configuration and evidence
  retention controls.

The current Phase 2 workflow executes only the checked-in deterministic fake
target and deterministic evaluators. The release-gate Action has read-only
repository permissions and contains no provider client, API credential, secret
reference, network model call, or live model integration. Future live adapters
must preserve these redaction and retention defaults.
