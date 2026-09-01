import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('dashboard visual contract', () => {
  it('uses solid fills without CSS gradients', async () => {
    const css = await readFile(resolve('app/globals.css'), 'utf8');

    expect(css).not.toMatch(/(?:linear|radial|conic)-gradient\s*\(/i);
  });

  it('uses the high-contrast focus token for select controls', async () => {
    const css = await readFile(resolve('app/globals.css'), 'utf8');

    expect(css).toMatch(
      /\.decision-picker select:focus-visible\s*{[^}]*outline:\s*3px solid var\(--focus\)/,
    );
    expect(css).toMatch(
      /\.case-filter select:focus-visible\s*{[^}]*outline:\s*3px solid var\(--focus\)/,
    );
  });
});
