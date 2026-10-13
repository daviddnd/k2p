import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { afterEach, describe, expect, it } from "vitest";
import type { RunResult, Runner } from "./runner";
import { Stack } from "./stack";
import { applyUpdate, checkUpdate, UpdateError } from "./update";

const tmpDirs: string[] = [];
function tmpDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "k2p-update-test-"));
  tmpDirs.push(dir);
  return dir;
}
afterEach(() => {
  while (tmpDirs.length) {
    fs.rmSync(tmpDirs.pop()!, { recursive: true, force: true });
  }
});

const MANIFEST = {
  version: "1.2.0",
  url: "https://releases.example/K2P-Setup-1.2.0.exe",
  sha256: "abc123",
};

function manifestFetch(): typeof fetch {
  return (async () =>
    new Response(JSON.stringify(MANIFEST), { status: 200 })) as unknown as typeof fetch;
}

describe("checkUpdate", () => {
  it("parses the manifest and reports an update when the release is newer", async () => {
    const result = await checkUpdate("https://releases.example/manifest.json", "1.0.0", manifestFetch());
    expect(result).toEqual({ update: true, version: "1.2.0", url: MANIFEST.url, sha256: "abc123" });
  });

  it("reports no update when current >= manifest version", async () => {
    expect((await checkUpdate("m", "1.2.0", manifestFetch())).update).toBe(false);
    expect((await checkUpdate("m", "1.3.0", manifestFetch())).update).toBe(false);
  });
});

describe("applyUpdate", () => {
  it("refuses on a non-ready stack", async () => {
    const dir = tmpDir();
    const run: Runner = () => Promise.resolve({ code: 0, stdout: "", stderr: "" });
    const stack = new Stack({ run, file: "f.yml", envFile: path.join(dir, "e.env"), env: {} });
    const err = await applyUpdate(
      { stack, downloadsDir: dir, open: async () => {} },
      { url: MANIFEST.url }
    ).catch((e) => e);
    expect(err).toBeInstanceOf(UpdateError);
    expect(err.name).toBe("stack_not_ready");
  });

  it("refuses a downloaded installer whose SHA-256 disagrees with the manifest", async () => {
    const dir = tmpDir();
    const results: RunResult[] = [
      { code: 0, stdout: "db\nminio\nclamav\nbackend\nfrontend\n", stderr: "" },
    ];
    const run: Runner = () => Promise.resolve(results[0]);
    const stack = new Stack({ run, file: "f.yml", envFile: path.join(dir, "e.env"), env: {} });
    const opened: string[] = [];
    const fetchImpl = (async () => new Response("INSTALLER-BYTES")) as unknown as typeof fetch;
    const err = await applyUpdate(
      {
        stack,
        downloadsDir: dir,
        open: async (p: string) => void opened.push(p),
        fetchImpl,
        sha256: "deadbeef",
      },
      { url: MANIFEST.url }
    ).catch((e) => e);
    expect(err).toBeInstanceOf(UpdateError);
    expect(err.name).toBe("digest_mismatch");
    expect(opened).toEqual([]);
  });

  it("downloads the installer and hands it to the OS opener when ready", async () => {
    const dir = tmpDir();
    const results: RunResult[] = [
      { code: 0, stdout: "db\nminio\nclamav\nbackend\nfrontend\n", stderr: "" }, // ps → ready
    ];
    const run: Runner = () => Promise.resolve(results[0]);
    const stack = new Stack({ run, file: "f.yml", envFile: path.join(dir, "e.env"), env: {} });
    const opened: string[] = [];
    const fetchImpl = (async () => new Response("INSTALLER-BYTES")) as unknown as typeof fetch;
    await applyUpdate(
      { stack, downloadsDir: dir, open: async (p: string) => void opened.push(p), fetchImpl },
      { url: MANIFEST.url }
    );
    const installer = path.join(dir, path.basename(MANIFEST.url));
    expect(fs.existsSync(installer)).toBe(true);
    expect(fs.readFileSync(installer, "utf-8")).toBe("INSTALLER-BYTES");
    expect(opened).toEqual([installer]);
  });
});
