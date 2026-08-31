import type { ReleaseDecisionDistributions } from '@/src/api/client';

type Quantile = ReleaseDecisionDistributions['baseline']['latency_ms']['statistics'];

function statistic(
  summary: Quantile,
  field: 'mean' | 'p50' | 'p95',
  unit = '',
): string {
  if (summary.suppressed) return 'Suppressed for privacy';
  if (summary.sample_count === 0) return 'Unavailable';
  const value = summary[field];
  if (value == null) return 'Unavailable';
  return `${value.toFixed(3)}${unit}`;
}

function coverage(measured: number, attempted: number): string {
  return `${measured}/${attempted}`;
}

export default function DistributionComparison({
  distributions,
}: {
  distributions: ReleaseDecisionDistributions;
}) {
  const { baseline, candidate, score } = distributions;
  const measurements = [
    ['Latency', 'latency_ms', ' ms'],
    ['Input units', 'input_units', ' units'],
    ['Output units', 'output_units', ' units'],
    ['Total units', 'total_units', ' units'],
  ] as const;
  const scoreSmallSample =
    score.baseline.statistics.small_sample ||
    score.candidate.statistics.small_sample ||
    score.delta.statistics.small_sample;

  return (
    <section
      className="distribution-panel"
      id="distribution-evidence"
      aria-labelledby="distribution-heading"
    >
      <div className="panel-heading distribution-heading">
        <div>
          <p className="eyebrow">Bounded aggregates</p>
          <h2 id="distribution-heading">Distribution comparison</h2>
        </div>
        <span className="result-count">
          {score.metric} · {score.gate_slice ?? 'all cases'}
        </span>
      </div>

      <div className="distribution-tables">
        <table>
          <caption>Score and candidate-minus-baseline distribution</caption>
          <thead>
            <tr>
              <th scope="col">Statistic</th>
              <th scope="col">Baseline</th>
              <th scope="col">Candidate</th>
              <th scope="col">Delta</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Coverage</th>
              <td>{coverage(score.baseline.scored, score.baseline.attempted)}</td>
              <td>{coverage(score.candidate.scored, score.candidate.attempted)}</td>
              <td>{coverage(score.delta.compared, score.delta.attempted)}</td>
            </tr>
            {(['mean', 'p50', 'p95'] as const).map((field) => (
              <tr key={field}>
                <th scope="row">{field.toUpperCase()}</th>
                <td>{statistic(score.baseline.statistics, field)}</td>
                <td>{statistic(score.candidate.statistics, field)}</td>
                <td>{statistic(score.delta.statistics, field)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <table>
          <caption>Operational distribution; no case-level values</caption>
          <thead>
            <tr>
              <th scope="col">Measurement</th>
              <th scope="col">Baseline p50 / p95</th>
              <th scope="col">Candidate p50 / p95</th>
              <th scope="col">Coverage</th>
            </tr>
          </thead>
          <tbody>
            {measurements.map(([label, key, unit]) => {
              const baselineMeasurement = baseline[key];
              const candidateMeasurement = candidate[key];
              return (
                <tr key={key}>
                  <th scope="row">{label}</th>
                  <td>
                    {statistic(baselineMeasurement.statistics, 'p50', unit)} /{' '}
                    {statistic(baselineMeasurement.statistics, 'p95', unit)}
                  </td>
                  <td>
                    {statistic(candidateMeasurement.statistics, 'p50', unit)} /{' '}
                    {statistic(candidateMeasurement.statistics, 'p95', unit)}
                  </td>
                  <td>
                    {coverage(
                      baselineMeasurement.measured,
                      baselineMeasurement.attempted,
                    )}{' '}
                    ↔{' '}
                    {coverage(
                      candidateMeasurement.measured,
                      candidateMeasurement.attempted,
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="distribution-note">
        {scoreSmallSample
          ? 'Small score samples are descriptive only. '
          : ''}
        Operational quantiles below 20 measurements are suppressed for privacy.
        Counts remain visible; raw samples are never transferred.
      </p>
    </section>
  );
}
