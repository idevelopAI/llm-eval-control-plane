import type { Metadata } from 'next';

export const SITE_ORIGIN = new URL(
  'https://llm-eval-control-plane.nick0ne.chatgpt.site',
);

export const SITE_TITLE = 'LLM Eval Control Plane';
export const SITE_DESCRIPTION =
  'Deterministic release gates and decision evidence for production AI systems.';
export const SOCIAL_PREVIEW_URL = new URL('/og.png', SITE_ORIGIN).toString();

const socialPreview = {
  alt: 'LLM Eval Control Plane release-gate preview',
  height: 630,
  url: SOCIAL_PREVIEW_URL,
  width: 1200,
};

export const SITE_METADATA = {
  alternates: {
    canonical: SITE_ORIGIN,
  },
  applicationName: SITE_TITLE,
  description: SITE_DESCRIPTION,
  metadataBase: SITE_ORIGIN,
  openGraph: {
    description: SITE_DESCRIPTION,
    images: [socialPreview],
    siteName: SITE_TITLE,
    title: SITE_TITLE,
    type: 'website',
    url: SITE_ORIGIN,
  },
  robots: {
    follow: false,
    index: false,
  },
  title: `${SITE_TITLE} · Release evidence`,
  twitter: {
    card: 'summary_large_image',
    description: SITE_DESCRIPTION,
    images: [socialPreview],
    title: SITE_TITLE,
  },
} satisfies Metadata;
