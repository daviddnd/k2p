import { randomBytes } from "crypto";
import * as fs from "fs";

export const SETUP_TOKEN_KEY = "AICS_SETUP_TOKEN";

const PINNED: Record<string, string> = {
  // The vendor control plane (ADR-0016) — the production activation/lease
  // endpoint every fresh install points at; non-secret, per-deployment
  // overrides (test LAN CPs) go in k2p.env by hand as before.
  AICS_CP_BASE_URL: "https://cp.kairospinax.com",
  POSTGRES_USER: "aics",
  POSTGRES_DB: "aics",
  AICS_S3_BUCKET: "aics-documents",
};

/**
 * Builds the per-install env: pinned product values plus generated secrets.
 * `version` is injected by the caller (app.getVersion() in the shell, the
 * package.json version the release pipeline pins from the tag) — one version
 * axis: launcher version = compose manifest = image tags.
 */
export function generateEnv(
  rand: () => string = () => randomBytes(32).toString("hex"),
  version: string = "0.0.0-dev",
  credentialDir: string = ""
): Record<string, string> {
  return {
    ...PINNED,
    K2P_VERSION: version,
    K2P_CREDENTIAL_DIR: credentialDir,
    POSTGRES_PASSWORD: rand(),
    MINIO_ROOT_PASSWORD: rand(),
    [SETUP_TOKEN_KEY]: rand(),
  };
}

const SECRET_KEY = /PASSWORD|SECRET|KEY|TOKEN/;

/** Values of secret-looking keys replaced with <redacted>; the backup policy input. */
export function redactEnv(env: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(env)) {
    out[key] = SECRET_KEY.test(key) ? "<redacted>" : value;
  }
  return out;
}

export function writeEnvFile(file: string, env: Record<string, string>): void {
  const text = Object.entries(env)
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
  fs.writeFileSync(file, text + "\n", { mode: 0o600 });
}
