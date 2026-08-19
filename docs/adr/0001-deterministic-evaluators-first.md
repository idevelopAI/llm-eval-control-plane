# ADR 0001: Prefer Deterministic Evaluators First

- Status: Accepted
- Date: 2026-08-19

## Context

Model-based judges are useful for qualities that cannot be measured directly,
but they add provider dependence, cost, latency, variance, prompt sensitivity,
and correlated failure modes. A judge score alone is weak evidence for a release
decision.

## Decision

The initial evaluation spine will use deterministic evaluators for properties
such as schema validity, exact or normalized equivalence, SQL policy compliance,
retrieval ranking, citation linkage, refusal behavior, latency, and usage.

Model-based judges may be added behind the evaluator contract only when:

1. deterministic evidence is insufficient for the property;
2. the judge configuration and prompt are versioned;
3. agreement is measured against a reviewed calibration set; and
4. the judge is not the sole gate for a safety-critical property.

## Consequences

- CI remains offline, inexpensive, and reproducible.
- Early metrics may cover fewer subjective qualities.
- Optional judge results require calibration evidence and explicit uncertainty.
