const TOKEN_PATTERN = /^cpk_[A-Za-z0-9_-]{43}$/;
const PROJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export type RuntimeCredential = Readonly<{
  accessToken: string;
  projectId: string;
}>;

export type CredentialSource = () => RuntimeCredential | null;

export function isRuntimeCredential(
  value: RuntimeCredential | null | undefined,
): value is RuntimeCredential {
  return Boolean(
    value &&
      TOKEN_PATTERN.test(value.accessToken) &&
      PROJECT_PATTERN.test(value.projectId),
  );
}

export type RuntimeCredentialVault = Readonly<{
  clear: () => void;
  credential: CredentialSource;
  hasCredential: () => boolean;
  set: (value: RuntimeCredential) => void;
}>;

/**
 * Store one read-only browser credential exclusively in a closure. No field on
 * the returned object contains the token, and clearing drops the only retained
 * reference.
 */
export function createRuntimeCredentialVault(): RuntimeCredentialVault {
  let retained: RuntimeCredential | null = null;

  return Object.freeze({
    clear() {
      retained = null;
    },
    credential() {
      return retained;
    },
    hasCredential() {
      return retained !== null;
    },
    set(value: RuntimeCredential) {
      if (!isRuntimeCredential(value)) {
        throw new Error('The control-plane credential format is invalid.');
      }
      retained = Object.freeze({
        accessToken: value.accessToken,
        projectId: value.projectId,
      });
    },
  });
}
