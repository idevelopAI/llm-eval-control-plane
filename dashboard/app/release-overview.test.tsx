import axe from 'axe-core';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import ReleaseOverview from './release-overview';

describe('ReleaseOverview', () => {
  it('makes the seeded regression understandable from the first view', () => {
    render(<ReleaseOverview />);

    expect(
      screen.getByRole('heading', { name: 'Release blocked' }),
    ).toBeTruthy();
    const releaseSummary = screen.getByRole('region', {
      name: 'Release summary',
    });
    const gateCard = within(releaseSummary)
      .getByText('Gates passed')
      .closest('article');
    const decisionCard = within(releaseSummary)
      .getByText('Release decision')
      .closest('article');
    const failingCaseCard = within(releaseSummary)
      .getByText('Newly failing cases')
      .closest('article');
    const coverageCard = within(releaseSummary)
      .getByText('Selected gate coverage')
      .closest('article');

    expect(gateCard?.textContent).toContain('3 / 4');
    expect(decisionCard?.textContent).toContain('Blocked');
    expect(failingCaseCard?.textContent).toContain('1');
    expect(coverageCard?.textContent).toContain('8 / 8');
    expect(screen.getByText('−0.125')).toBeTruthy();
    expect(
      screen.getByText(
        (_content, element) =>
          element?.matches('.coverage-cell strong') === true &&
          element.textContent === '8/8 ↔ 8/8',
      ),
    ).toBeTruthy();
    expect(screen.getByText('refusal-de-001')).toBeTruthy();
    expect(
      screen.getByText('Small sample · descriptive result'),
    ).toBeTruthy();
    expect(
      screen.getByRole('table', {
        name: 'Operational distribution; no case-level values',
      }),
    ).toBeTruthy();
    expect(screen.getAllByText(/Suppressed for privacy/).length).toBeGreaterThan(0);
  });

  it('moves from the failed gate to bounded scoring evidence', async () => {
    const user = userEvent.setup();
    render(<ReleaseOverview />);

    await user.click(
      screen.getByRole('button', { name: /review failed gate/i }),
    );

    const evidenceHeading = screen.getByRole('heading', {
      name: 'Scoring evidence',
    });
    await waitFor(() => expect(document.activeElement).toBe(evidenceHeading));

    await user.click(
      screen.getByRole('button', {
        name: 'Inspect scoring evidence for refusal-de-001',
      }),
    );
    expect(screen.getByText('1.000 · passed')).toBeTruthy();
    expect(screen.getByText('0.000 · failed')).toBeTruthy();
    expect(
      screen.getByText(/Prompt, expected value, target output, SQL/),
    ).toBeTruthy();
  });

  it('has no automated structural accessibility violations', async () => {
    const { container } = render(<ReleaseOverview />);
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
