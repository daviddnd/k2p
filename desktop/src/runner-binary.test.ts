import { describe, expect, it } from "vitest";
import { execRunner } from "./runner";

describe("execRunner binary stdout", () => {
  it("returns raw bytes verbatim when binaryStdout is set", async () => {
    // Bytes that are invalid as UTF-8: 0xff 0xfe 0x00 0x80 — a text decode would
    // corrupt them with U+FFFD replacements (the backup-dump defect).
    const res = await execRunner("sh", ["-c", "printf '\\377\\376\\000\\200'"], {
      binaryStdout: true,
    });
    expect(res.code).toBe(0);
    expect([...res.stdoutBytes!]).toEqual([0xff, 0xfe, 0x00, 0x80]);
  });

  it("still returns text by default", async () => {
    const res = await execRunner("sh", ["-c", "printf hello"]);
    expect(res.stdout).toBe("hello");
  });
});

describe("probeDocker with a rejecting runner", () => {
  it("maps a spawn failure (docker absent) to the missing reason", async () => {
    const { probeDocker } = await import("./docker");
    const run = () => Promise.reject(new Error("spawn docker ENOENT"));
    expect(await probeDocker(run)).toEqual({ ok: false, reason: "missing" });
  });
});
