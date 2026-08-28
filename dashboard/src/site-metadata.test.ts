import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  SITE_DESCRIPTION,
  SITE_METADATA,
  SITE_ORIGIN,
  SITE_TITLE,
  SOCIAL_PREVIEW_URL,
} from './site-metadata';

describe('hosted Site metadata', () => {
  it('pins canonical and social URLs to the trusted deployment origin', () => {
    expect(SITE_ORIGIN.toString()).toBe(
      'https://llm-eval-control-plane.nick0ne.chatgpt.site/',
    );
    expect(SOCIAL_PREVIEW_URL).toBe(
      'https://llm-eval-control-plane.nick0ne.chatgpt.site/og.png',
    );
    expect(SITE_METADATA).toMatchObject({
      alternates: { canonical: SITE_ORIGIN },
      description: SITE_DESCRIPTION,
      metadataBase: SITE_ORIGIN,
      openGraph: {
        description: SITE_DESCRIPTION,
        title: SITE_TITLE,
        url: SITE_ORIGIN,
      },
      title: `${SITE_TITLE} · Release evidence`,
      twitter: {
        card: 'summary_large_image',
        description: SITE_DESCRIPTION,
        title: SITE_TITLE,
      },
    });
  });

  it('keeps the private fixture out of search indexes', () => {
    expect(SITE_METADATA.robots).toEqual({
      follow: false,
      index: false,
    });
  });

  it('ships the expected 1200 by 630 PNG preview', async () => {
    const image = await readFile(resolve('public/og.png'));
    const header = new DataView(
      image.buffer,
      image.byteOffset,
      image.byteLength,
    );

    expect(new TextDecoder().decode(image.subarray(1, 4))).toBe('PNG');
    expect(header.getUint32(16)).toBe(1200);
    expect(header.getUint32(20)).toBe(630);
  });
});
