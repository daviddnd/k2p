import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import type { RunResult, Runner } from "./runner";
import { Stack } from "./stack";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-log-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

describe("Stack failure logging (review finding: details must reach the logs)", () => {
  it("logs the failing command and its stderr for a failed migration", async () => {
    const dir = tmpDir();
    const lines: string[] = [];
    const results: RunResult[] = [
      { code: 1, stdout: "", stderr: "column already exists" },
    ];
    let i = 0;
    const run: Runner = () => Promise.resolve(results[Math.min(i++, results.length - 1)]);
    const stack = new Stack({
      run,
      file: "f.yml",
      envFile: path.join(dir, "e.env"),
      env: {},
      log: (msg) => lines.push(msg),
    });
    await stack.migrate().catch(() => {});
    const text = lines.join("\n");
    expect(text).toContain("alembic");
    expect(text).toContain("column already exists");
  });
});
