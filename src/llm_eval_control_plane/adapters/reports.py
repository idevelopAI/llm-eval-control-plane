"""Deterministic, content-safe release decision report renderers."""

from __future__ import annotations

import json
from collections import Counter
from enum import StrEnum
from xml.etree import ElementTree

from llm_eval_control_plane.domain import (
    CaseChange,
    GateCaseComparison,
    GateResult,
    GateStatus,
    MetricAggregate,
    ReleaseDecision,
)


class ReportFormat(StrEnum):
    JSON = "json"
    MARKDOWN = "markdown"
    JUNIT = "junit"


def render_report(decision: ReleaseDecision, format: ReportFormat) -> str:
    """Render one release decision without case inputs, expectations, or outputs."""
    if format is ReportFormat.JSON:
        return _render_json(decision)
    if format is ReportFormat.MARKDOWN:
        return _render_markdown(decision)
    if format is ReportFormat.JUNIT:
        return _render_junit(decision)
    raise ValueError("Unsupported release report format")


def _render_json(decision: ReleaseDecision) -> str:
    return (
        json.dumps(
            decision.model_dump(mode="json"),
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _render_markdown(decision: ReleaseDecision) -> str:
    lines = [
        "# Release Gate Report",
        "",
        f"**Decision:** `{decision.status.value.upper()}`",
        "",
        f"- Policy: `{decision.spec_name}`",
        f"- Execution mode: `{decision.execution_mode.value}`",
        f"- Baseline run: `{decision.baseline_run_id}`",
        f"- Candidate run: `{decision.candidate_run_id}`",
        f"- Dataset digest: `{decision.dataset.digest}`",
        f"- Decision digest: `{decision.decision_digest}`",
        "",
        "## Gates",
        "",
        "| Status | Metric | Slice | Baseline | Candidate | Delta | "
        "Threshold | Regression budget | Failures |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        (
            "| "
            + " | ".join(
                (
                    gate.status.value.upper(),
                    _escape(gate.metric),
                    _escape(_scope(gate)),
                    _number(gate.aggregate.baseline.mean),
                    _number(gate.aggregate.candidate.mean),
                    _number(gate.aggregate.delta),
                    _number(gate.threshold),
                    _number(gate.allowed_regression),
                    _escape(
                        ", ".join(code.value for code in gate.failure_codes) or "—"
                    ),
                )
            )
            + " |"
        )
        for gate in decision.gates
    )

    lines.extend(
        (
            "",
            "## Case changes by gate",
            "",
            "| Metric | Slice | Newly passing | Newly failing | Unchanged passing | "
            "Unchanged failing | Incomparable |",
            "|---|---|---:|---:|---:|---:|---:|",
        )
    )
    for gate in decision.gates:
        changes = Counter(
            item.change
            for item in decision.cases
            if (item.metric, item.slice) == (gate.metric, gate.slice)
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape(gate.metric),
                    _escape(_scope(gate)),
                    str(changes[CaseChange.NEWLY_PASSING]),
                    str(changes[CaseChange.NEWLY_FAILING]),
                    str(changes[CaseChange.UNCHANGED_PASSING]),
                    str(changes[CaseChange.UNCHANGED_FAILING]),
                    str(changes[CaseChange.INCOMPARABLE]),
                )
            )
            + " |"
        )

    newly_failing = [
        item for item in decision.cases if item.change is CaseChange.NEWLY_FAILING
    ]
    lines.extend(("", "## Newly failing cases", ""))
    if not newly_failing:
        lines.append("None.")
    else:
        lines.extend(
            f"- `{item.case_id}` — `{item.metric}` ({_scope(item)})"
            for item in newly_failing
        )

    lines.extend(
        (
            "",
            "## Slice aggregates",
            "",
            "| Metric | Slice | Baseline coverage | Candidate coverage | "
            "Baseline mean | Candidate mean | Delta |",
            "|---|---|---:|---:|---:|---:|---:|",
        )
    )
    lines.extend(
        (
            "| "
            + " | ".join(
                (
                    _escape(aggregate.metric),
                    _escape(aggregate.slice or "all"),
                    _coverage(aggregate.baseline),
                    _coverage(aggregate.candidate),
                    _number(aggregate.baseline.mean),
                    _number(aggregate.candidate.mean),
                    _number(aggregate.delta),
                )
            )
            + " |"
        )
        for aggregate in decision.aggregates
    )
    return "\n".join(lines) + "\n"


def _render_junit(decision: ReleaseDecision) -> str:
    failures = sum(gate.status is GateStatus.FAILED for gate in decision.gates)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": decision.spec_name,
            "tests": str(len(decision.gates)),
            "failures": str(failures),
            "errors": "0",
        },
    )
    properties = ElementTree.SubElement(suite, "properties")
    for name, value in (
        ("baseline_run_id", decision.baseline_run_id),
        ("candidate_run_id", decision.candidate_run_id),
        ("dataset_digest", decision.dataset.digest or ""),
        ("decision_digest", decision.decision_digest),
        ("execution_mode", decision.execution_mode.value),
        ("release_status", decision.status.value),
    ):
        ElementTree.SubElement(properties, "property", {"name": name, "value": value})

    for gate in decision.gates:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": "llm_eval_control_plane.release_gate",
                "name": f"{gate.metric}[{_scope(gate)}]",
            },
        )
        if gate.status is GateStatus.FAILED:
            codes = ",".join(code.value for code in gate.failure_codes)
            failure = ElementTree.SubElement(
                case,
                "failure",
                {
                    "message": f"Release gate failed: {codes}",
                    "type": "release_gate_failure",
                },
            )
            failure.text = (
                f"baseline={_number(gate.aggregate.baseline.mean)}; "
                f"candidate={_number(gate.aggregate.candidate.mean)}; "
                f"delta={_number(gate.aggregate.delta)}; "
                f"threshold={_number(gate.threshold)}; "
                f"allowed_regression={_number(gate.allowed_regression)}"
            )
    system_out = ElementTree.SubElement(suite, "system-out")
    system_out.text = f"decision_digest={decision.decision_digest}"
    ElementTree.indent(suite, space="  ")
    return (
        ElementTree.tostring(
            suite,
            encoding="unicode",
            xml_declaration=True,
        )
        + "\n"
    )


def _scope(item: GateResult | GateCaseComparison) -> str:
    slice_name = item.slice
    return "all" if slice_name is None else str(slice_name)


def _coverage(aggregate: MetricAggregate) -> str:
    return (
        f"{aggregate.scored}/{aggregate.attempted} "
        f"(skip {aggregate.skipped}, error {aggregate.errors})"
    )


def _number(value: float | None) -> str:
    return "—" if value is None else format(value, ".10g")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")
