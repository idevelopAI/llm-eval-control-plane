# ADR 0004: Use Absolute, Slice-Aware Release Gates

- Status: Accepted
- Date: 2026-08-20

## Context

A broad mean can hide a serious regression in a smaller language, safety, or
task cohort. Threshold-only checks can also approve a candidate that remains
above a floor but has degraded materially from its baseline. Conversely, a
candidate and baseline with different missing or failed cases do not have
comparable coverage even when their reported means look acceptable.

Release evidence must make direction, regression units, coverage, slice scope,
and numeric boundary behavior unambiguous.

## Decision

Each gate identifies a metric and optional dataset slice, declares whether
higher or lower values are better, defines an absolute candidate threshold, and
defines an absolute allowed regression in the metric's own units.

Every delta is `candidate - baseline`:

- higher-is-better regression passes when
  `delta >= -allowed_regression`;
- lower-is-better regression passes when
  `delta <= allowed_regression`.

Gate boundaries use a fixed `1e-12` absolute comparison tolerance only to absorb
binary floating-point noise. This does not scale with metric magnitude and does
not alter the configured policy budget.

Every gate also requires comparable coverage: both sides have no evaluation
errors, score at least one case, and have matching scored and skipped counts.
Coverage, threshold, and regression checks are preserved separately in the
decision.

The comparator recomputes global and slice aggregates from case evidence and
verifies stored global summaries before evaluating policy. Candidate and
baseline must align on the supplied dataset, ordered case IDs, target policy
references, metric sets, and evaluator revisions. A mismatch is a configuration
error, not a failed release.

A release fails when any gate fails. Case transitions are interpreted relative
to each gate threshold. The stable decision digest covers all resolved evidence
and excludes caller-selected run IDs.

## Consequences

- A narrow safety or language regression can block release independently of a
  passing global metric.
- Threshold and regression failures remain distinguishable for automation and
  review.
- Technical failures and asymmetric missing evidence fail closed through the
  coverage check.
- Policies must express relative-percentage regressions by converting them to
  absolute metric units before evaluation.
- Changing delta direction, coverage rules, numeric tolerance, or digest content
  requires a versioned contract change.
