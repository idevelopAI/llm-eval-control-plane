import { spawnSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { staticHostingConfig } from './static-demo-policy.mjs';
import { verifyStaticDemo } from './verify-static-demo.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const result = spawnSync(process.execPath, ['node_modules/next/dist/bin/next', 'build', '--webpack'], {
  cwd: root,
  env: { ...process.env, CONTROL_PLANE_STATIC_EXPORT: '1', NEXT_TELEMETRY_DISABLED: '1' },
  stdio: 'inherit',
});
if (result.error) throw result.error;
if (result.status !== 0) throw new Error('Static compilation failed; nothing may be uploaded');
await writeFile(new URL('../out/vercel.json', import.meta.url), `${JSON.stringify(staticHostingConfig(), null, 2)}\n`);
await verifyStaticDemo();
