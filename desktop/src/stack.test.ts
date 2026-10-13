import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import { composeArgs } from "./docker";
import type { RunResult, Runner } from "./runner";
import { Stack, StackError } from "./stack";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-stack-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

interface RecordedCall {
  cmd: string;
  args: string[];
}
function recordingRunner(results: RunResult[]): { run: Runner; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  let i = 0;
  const run: Runner = (cmd, args) => {
    calls.push({ cmd, args });
    return Promise.resolve(results[Math.min(i++, results.length - 1)]);
  };
  return { run, calls };
}

function makeStack(run: Runner, dir: string): Stack {
  return new Stack({
    run,
    file: "/compose/desktop.compose.yml",
    envFile: path.join(dir, "k2p.env"),
    env: { POSTGRES_USER: "aics" },
  });
}

const UP_SERVICES = ["db", "minio", "minio-init", "clamav", "backend", "frontend"];

describe("Stack.up", () => {
  it("writes the env file on first up, then brings the six services up", async () => {
    const dir = tmpDir();
    const { run, calls } = recordingRunner([{ code: 0, stdout: "", stderr: "" }]);
    const stack = makeStack(run, dir);
    await stack.up();
    expect(fs.existsSync(path.join(dir, "k2p.env"))).toBe(true);
    expect(calls[0].cmd).toBe("docker");
    expect(calls[0].args).toEqual(
      composeArgs("/compose/desktop.compose.yml", path.join(dir, "k2p.env"), ["up", "-d", ...UP_SERVICES])
    );
  });

  it("does not rewrite an existing env file", async () => {
    const dir = tmpDir();
    fs.writeFileSync(path.join(dir, "k2p.env"), "POSTGRES_USER=aics\n");
    const { run, calls } = recordingRunner([{ code: 0, stdout: "", stderr: "" }]);
    const stack = makeStack(run, dir);
    await stack.up();
    expect(fs.readFileSync(path.join(dir, "k2p.env"), "utf-8")).toBe("POSTGRES_USER=aics\n");
    expect(calls.length).toBe(1);
  });

  it("maps a port collision to the named ports_in_use error", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([{ code: 1, stdout: "", stderr: "Error: bind: address already in use; port is already allocated" }]);
    const stack = makeStack(run, dir);
    const err = await stack.up().catch((e) => e);
    expect(err).toBeInstanceOf(StackError);
    expect(err.name).toBe("ports_in_use");
  });

  it("maps an image-pull failure to the named pull_failed error", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([{ code: 1, stdout: "", stderr: "error pulling image: failed to resolve reference" }]);
    const stack = makeStack(run, dir);
    const err = await stack.up().catch((e) => e);
    expect(err).toBeInstanceOf(StackError);
    expect(err.name).toBe("pull_failed");
  });
});

describe("Stack.status", () => {
  it("is ready when all five long-running services run", async () => {
    const dir = tmpDir();
    const ps = "backend\nclamav\ndb\nfrontend\nminio\n";
    const { run } = recordingRunner([{ code: 0, stdout: ps, stderr: "" }]);
    expect(await makeStack(run, dir).status()).toBe("ready");
  });

  it("is stopped when nothing runs", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([{ code: 0, stdout: "", stderr: "" }]);
    expect(await makeStack(run, dir).status()).toBe("stopped");
  });

  it("is starting on a partial service set", async () => {
    const dir = tmpDir();
    const { run } = recordingRunner([{ code: 0, stdout: "db\nminio\n", stderr: "" }]);
    expect(await makeStack(run, dir).status()).toBe("starting");
  });
});

describe("Stack.refreshImages / down", () => {
  it("refreshImages pulls then re-upgrades", async () => {
    const dir = tmpDir();
    const { run, calls } = recordingRunner([
      { code: 0, stdout: "", stderr: "" },
      { code: 0, stdout: "", stderr: "" },
    ]);
    await makeStack(run, dir).refreshImages();
    expect(calls[0].args).toContain("pull");
    expect(calls[1].args.slice(-UP_SERVICES.length - 1)).toEqual(["-d", ...UP_SERVICES]);
  });

  it("down stops the stack", async () => {
    const dir = tmpDir();
    const { run, calls } = recordingRunner([{ code: 0, stdout: "", stderr: "" }]);
    await makeStack(run, dir).down();
    expect(calls[0].args).toEqual(
      composeArgs("/compose/desktop.compose.yml", path.join(dir, "k2p.env"), ["stop"])
    );
  });
});
