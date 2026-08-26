import axe from 'axe-core';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import ReleaseOverview from './release-overview';

describe('ReleaseOverview', () => {
  it('makes the seeded regression understandable from the first view', () => {
    render(<ReleaseOverview />);

    expect(
      screen.getByRole('heading', { name: 'Release blocked' }),
    ).toBeTruthy();
    expect(
      screen.getByText('1', { selector: '.summary-number strong' }),
    ).toBeTruthy();
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
  });

  it('moves from the failed gate to bounded scoring evidence', async () => {
    const user = userEvent.setup();
    render(<ReleaseOverview />);

    await user.click(
      screen.getByRole('button', { name: /review safety regression/i }),
    );

    const evidenceHeading = screen.getByRole('heading', {
      name: 'Scoring evidence',
    });
    await waitFor(() => expect(document.activeElement).toBe(evidenceHeading));

    await user.click(
      screen.getByRole('button', { name: 'Inspect scoring evidence' }),
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
