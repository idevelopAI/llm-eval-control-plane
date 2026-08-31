import axe from 'axe-core';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import PublicReleaseDashboard from './public-release-dashboard';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('PublicReleaseDashboard', () => {
  it('ships an explicit request-free environment without live credentials', () => {
    const fetchMock = vi.fn();
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem');
    vi.stubGlobal('fetch', fetchMock);

    render(<PublicReleaseDashboard />);

    expect(screen.getByText('Public example environment')).toBeTruthy();
    expect(
      screen.getByText(
        'Synthetic data · no credentials, API, or model requests.',
      ),
    ).toBeTruthy();
    expect(
      screen.getByText('Synthetic release evidence · zero API or model calls'),
    ).toBeTruthy();
    expect(screen.queryByText(/live control plane/i)).toBeNull();
    expect(screen.queryByLabelText(/access token/i)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storageWrite).not.toHaveBeenCalled();
  });

  it('keeps evidence interactions local to the synthetic fixture', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn();
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem');
    vi.stubGlobal('fetch', fetchMock);

    render(<PublicReleaseDashboard />);

    await user.click(screen.getByRole('button', { name: 'Language' }));
    await user.click(
      screen.getByRole('button', { name: /review failed gate/i }),
    );
    await user.click(
      screen.getByRole('button', {
        name: 'Inspect scoring evidence for refusal-de-001',
      }),
    );

    expect(screen.getByText('1.000 · passed')).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storageWrite).not.toHaveBeenCalled();
  });

  it('has no automated structural accessibility violations', async () => {
    const { container } = render(<PublicReleaseDashboard />);
    const results = await axe.run(container, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
