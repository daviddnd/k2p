import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import { generateEnv, redactEnv, SETUP_TOKEN_KEY, writeEnvFile } from "./env";

const tmpDirs: string[] = [];
function tmpFile(name: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-env-test-"));
  tmpDirs.push(dir);
  return path.join(dir, name);
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

describe("generateEnv", () => {
  it("contains exactly the seven pinned keys with the pinned non-secret values", () => {
    const env = generateEnv(() => "ab".repeat(32));
    expect(Object.keys(env).sort()).toEqual(
      [
        "AICS_SETUP_TOKEN",
        "AICS_S3_BUCKET",
        "K2P_CREDENTIAL_DIR",
        "K2P_VERSION",
        "MINIO_ROOT_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
      ].sort()
    );
    expect(env.POSTGRES_USER).toBe("aics");
    expect(env.POSTGRES_DB).toBe("aics");
    expect(env.AICS_S3_BUCKET).toBe("aics-documents");
    expect(env.K2P_VERSION).toBeTruthy();
    expect(env.K2P_CREDENTIAL_DIR).toBe("");
  });

  it("generates three pairwise-distinct 64-hex-char secrets", () => {
    const values = ["11".repeat(32), "22".repeat(32), "33".repeat(32)];
    let i = 0;
    const env = generateEnv(() => values[i++]);
    const secrets = [env.POSTGRES_PASSWORD, env.MINIO_ROOT_PASSWORD, env[SETUP_TOKEN_KEY]];
    for (const s of secrets) {
      expect(s).toMatch(/^[0-9a-f]{64}$/);
    }
    expect(new Set(secrets).size).toBe(3);
  });
});

describe("redactEnv", () => {
  it("replaces values of keys matching PASSWORD|SECRET|KEY|TOKEN, keeps the rest", () => {
    const redacted = redactEnv({
      POSTGRES_USER: "aics",
      POSTGRES_PASSWORD: "supersecret",
      AICS_SETUP_TOKEN: "tok",
      MINIO_ROOT_USER: "aics",
      K2P_VERSION: "1.0.0",
    });
    expect(redacted.POSTGRES_PASSWORD).toBe("<redacted>");
    expect(redacted.AICS_SETUP_TOKEN).toBe("<redacted>");
    expect(redacted.POSTGRES_USER).toBe("aics");
    expect(redacted.MINIO_ROOT_USER).toBe("aics");
    expect(redacted.K2P_VERSION).toBe("1.0.0");
  });
});

describe("writeEnvFile", () => {
  it("writes KEY=value lines with mode 0600", () => {
    const file = tmpFile("k2p.env");
    writeEnvFile(file, { POSTGRES_USER: "aics", POSTGRES_PASSWORD: "x" });
    const text = fs.readFileSync(file, "utf-8");
    expect(text).toContain("POSTGRES_USER=aics\n");
    expect(text).toContain("POSTGRES_PASSWORD=x\n");
    expect(fs.statSync(file).mode & 0o777).toBe(0o600);
  });
});
