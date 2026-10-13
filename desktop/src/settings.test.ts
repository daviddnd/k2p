import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import { loadSettings, saveSettings } from "./settings";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-settings-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

describe("settings persistence (autoStart toggle)", () => {
  it("defaults to autoStart true when no settings file exists", () => {
    expect(loadSettings(tmpDir()).autoStart).toBe(true);
  });

  it("round-trips the autoStart preference", () => {
    const dir = tmpDir();
    saveSettings(dir, { autoStart: false });
    expect(loadSettings(dir).autoStart).toBe(false);
    saveSettings(dir, { autoStart: true });
    expect(loadSettings(dir).autoStart).toBe(true);
  });
});
