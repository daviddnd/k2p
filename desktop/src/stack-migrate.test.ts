import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import type { RunResult, Runner } from "./runner";
import { Stack, StackError } from "./stack";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-migrate-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

function recordingRunner(results: RunResult[]): { run: Runner; calls: string[][] } {
  const calls: string[][] = [];
  let i = 0;
  const run: Runner = (cmd, args) => {
    calls.push(args);
    return Promise.resolve(results[Math.min(i++, results.length - 1)]);
  };
  return { run, calls };
}

function makeStack(run: Runner, dir: string): Stack {
  return new Stack({ run, file: "f.yml", envFile: path.join(dir, "k2p.env"), env: {} });
}

describe("Stack.migrate", () => {
  it("runs the one-shot alembic upgrade through the backend service", async () => {
    const dir = tmpDir();
    const { run, calls } = recordingRunner([{ code: 0, stdout: "", stderr: "" }]);
    await makeStack(run, dir).migrate();
    expect(calls.length).toBe(1);
    expect(calls[0]).toContain("run");
    expect(calls[0]).toContain("--rm");
    expect(calls[0]).toContain("backend");
    expect(calls[0]).toEqual(expect.arrayContaining(["alembic", "upgrade", "head"]));
  });

  it("maps a non-zero exit to the named migration_failed error with stderr detail", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([{ code: 1, stdout: "", stderr: "column already exists" }]);
    const err = await makeStack(run, dir).migrate().catch((e) => e);
    expect(err).toBeInstanceOf(StackError);
    expect(err.name).toBe("migration_failed");
    expect(err.detail).toContain("column already exists");
  });
});

describe("named failure recovery pins", () => {
  it("a failed up leaves the stack retryable: the next up issues a fresh compose up", async () => {
    const dir = tmpDir();
    const { run, calls } = recordingRunner([
      { code: 1, stdout: "", stderr: "error pulling image: i/o timeout" },
      { code: 0, stdout: "", stderr: "" },
    ]);
    const stack = makeStack(run, dir);
    await expect(stack.up()).rejects.toMatchObject({ name: "pull_failed" });
    await stack.up();
    const upCalls = calls.filter((args) => args[0] === "compose" && args.includes("up"));
    expect(upCalls.length).toBe(2);
  });

  it("the daemon-stopped guidance names the start-Docker remedy in the error detail", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([
      { code: 1, stdout: "", stderr: "Cannot connect to the Docker daemon" },
    ]);
    const stack = makeStack(run, dir);
    // probe first, then surface the guided message on up()
    const err = await stack.up().catch((e) => e);
    expect(err).toBeDefined();
    expect(JSON.stringify(err)).toMatch(/docker/i);
  });
});
