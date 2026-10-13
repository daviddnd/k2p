import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import type { RunResult, Runner } from "./runner";
import { Stack } from "./stack";
import { runBackup, BackupError } from "./backup";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-backup-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

function makeStack(run: Runner, dir: string, psResult: RunResult): Stack {
  return new Stack({ run, file: "/c/desktop.compose.yml", envFile: path.join(dir, "k2p.env"), env: {} });
}

function deps(run: Runner, stack: Stack, dir: string, dest: string) {
  const composeFile = path.join(dir, "desktop.compose.yml");
  fs.writeFileSync(composeFile, "services: {}\n");
  return {
    run,
    stack,
    dataDir: dir,
    destDir: dest,
    envFile: path.join(dir, "k2p.env"),
    composeFile,
    env: { POSTGRES_USER: "aics", POSTGRES_PASSWORD: "secretpw" },
    now: new Date("2026-10-11T12:00:00Z"),
  };
}

describe("runBackup", () => {
  it("refuses to half-run when the stack is not ready", async () => {
    const dir = tmpDir();
    const results: RunResult[] = [{ code: 0, stdout: "", stderr: "" }]; // ps: nothing running
    const run: Runner = () => Promise.resolve(results[0]);
    const stack = makeStack(run, dir, results[0]);
    const err = await runBackup(deps(run, stack, dir, tmpDir())).catch((e) => e);
    expect(err).toBeInstanceOf(BackupError);
    expect(err.name).toBe("stack_not_ready");
  });

  it("runs the container-side dump and mirror, then archives with a names-only manifest", async () => {
    const dir = tmpDir();
    const dest = tmpDir();
    fs.writeFileSync(path.join(dir, "k2p.env"), "POSTGRES_PASSWORD=secretpw\nPOSTGRES_USER=aics\n");
    fs.mkdirSync(path.join(dir, "credentials"));
    fs.writeFileSync(path.join(dir, "credentials", "tenant-key"), "TOP-SECRET-KEY-MATERIAL");

    const calls: string[][] = [];
    const results: RunResult[] = [
      { code: 0, stdout: "db\nminio\nclamav\nbackend\nfrontend\n", stderr: "" }, // ps → ready
      { code: 0, stdout: "PGDUMP-BYTES", stderr: "" }, // pg_dump
      { code: 0, stdout: "mirrored", stderr: "" }, // mc mirror (container)
      { code: 0, stdout: "", stderr: "" }, // compose cp
      { code: 0, stdout: "IMAGE DIGEST LIST", stderr: "" }, // compose images
      { code: 0, stdout: "", stderr: "" }, // cleanup rm
    ];
    let i = 0;
    const run: Runner = (cmd, args) => {
      calls.push(args);
      if (cmd === "tar" && args.includes("-czf")) {
        fs.writeFileSync(args[args.indexOf("-czf") + 1], "tar-bytes");
      }
      return Promise.resolve(results[Math.min(i++, results.length - 1)]);
    };
    const stack = makeStack(run, dir, results[0]);

    const { archive, manifest } = await runBackup(deps(run, stack, dir, dest));
    expect(path.basename(archive)).toBe("aics-backup-20261011T120000Z.tar.gz");
    expect(fs.existsSync(archive)).toBe(true);
    expect(fs.existsSync(manifest)).toBe(true);

    // Command sequence: ps, pg_dump, mc mirror, cp, images, rm
    const kinds = calls.map((args) => args[args.length - 1] === "running" ? "ps" : args.join(" "));
    expect(calls[1]).toEqual(expect.arrayContaining(["exec", "-T", "db", "pg_dump", "-U", "aics", "-d", "aics", "-Fc"]));
    expect(calls[2].join(" ")).toContain("mc mirror");
    expect(calls[3][0]).toBe("compose"); // cp is a compose subcommand
    expect(calls[3]).toEqual(expect.arrayContaining(["cp", "minio:/tmp/k2p-backup-objects"]));

    // Staging must be gone; archive must contain the members and the redacted env,
    // and the manifest lists credential paths by name only.
    expect(fs.existsSync(path.join(dir, "backup-staging"))).toBe(false);
    const manifestText = fs.readFileSync(manifest, "utf-8");
    expect(manifestText).toMatch(/^# AICS backup manifest/);
    expect(manifestText).toContain("postgres/custom.dump");
    expect(manifestText).toContain("credential paths are names only");
    expect(manifestText).toContain("credentials/tenant-key");
    expect(manifestText).not.toContain("TOP-SECRET-KEY-MATERIAL");
  });
});
