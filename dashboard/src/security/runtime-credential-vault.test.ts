import { describe, expect, it, vi } from 'vitest';

import { createRuntimeCredentialVault } from './runtime-credential-vault';

const TEST_TOKEN = `cpk_${'A'.repeat(43)}`;

describe('createRuntimeCredentialVault', () => {
  it('retains a frozen credential only inside volatile memory', () => {
    const localWrite = vi.spyOn(Storage.prototype, 'setItem');
    const vault = createRuntimeCredentialVault();
    const input = {
      accessToken: TEST_TOKEN,
      projectId: 'project-alpha',
    };

    vault.set(input);
    input.accessToken = 'changed-after-capture';

    expect(vault.hasCredential()).toBe(true);
    expect(vault.credential()).toEqual({
      accessToken: TEST_TOKEN,
      projectId: 'project-alpha',
    });
    expect(Object.isFrozen(vault.credential())).toBe(true);
    expect(JSON.stringify(vault)).toBe('{}');
    expect(localWrite).not.toHaveBeenCalled();
  });

  it('drops its retained reference when cleared', () => {
    const vault = createRuntimeCredentialVault();
    vault.set({ accessToken: TEST_TOKEN, projectId: 'project-alpha' });

    vault.clear();

    expect(vault.hasCredential()).toBe(false);
    expect(vault.credential()).toBeNull();
  });

  it.each([
    { accessToken: '', projectId: 'project-alpha' },
    { accessToken: 'not-a-control-plane-token', projectId: 'project-alpha' },
    { accessToken: TEST_TOKEN, projectId: '' },
    { accessToken: TEST_TOKEN, projectId: 'project with spaces' },
    { accessToken: TEST_TOKEN, projectId: 'project\nheader-injection' },
  ])('rejects invalid input without echoing it', (credential) => {
    const vault = createRuntimeCredentialVault();

    expect(() => vault.set(credential)).toThrow(
      'The control-plane credential format is invalid.',
    );
    expect(vault.credential()).toBeNull();
  });
});
