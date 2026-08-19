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
- CI uses deterministic public or synthetic fixtures and does not require paid
  model access.
- Live evaluation is an explicit mode with separate configuration and evidence
  retention controls.

The current foundation release validates contracts only and does not connect to
external targets or execute model output.
