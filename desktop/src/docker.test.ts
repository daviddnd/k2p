import { describe, expect, it } from "vitest";
import type { RunResult, Runner } from "./runner";
import { composeArgs, probeDocker } from "./docker";

function stubRunner(results: RunResult[]): Runner {
  const calls: Array<{ cmd: string; args: string[] }> = [];
  let i = 0;
  const run: Runner = (cmd, args) => {
    calls.push({ cmd, args });
    return Promise.resolve(results[Math.min(i++, results.length - 1)]);
  };
  return Object.assign(run, { calls });
}

describe("probeDocker", () => {
  it("reports missing when docker is not installed", async () => {
    const run = stubRunner([{ code: 127, stdout: "", stderr: "docker: command not found" }]);
    expect(await probeDocker(run)).toEqual({ ok: false, reason: "missing" });
  });

  it("reports daemon-stopped when the engine is not running", async () => {
    const run = stubRunner([
      { code: 1, stdout: "", stderr: "Cannot connect to the Docker daemon. Is the docker daemon running?" },
    ]);
    expect(await probeDocker(run)).toEqual({ ok: false, reason: "daemon-stopped" });
  });

  it("reports no-compose when the plugin is absent", async () => {
    const run = stubRunner([
      { code: 0, stdout: "Client: ...", stderr: "" },
      { code: 1, stdout: "", stderr: "docker: 'compose' is not a docker command" },
    ]);
    expect(await probeDocker(run)).toEqual({ ok: false, reason: "no-compose" });
  });

  it("reports ok when docker and compose both answer", async () => {
    const run = stubRunner([
      { code: 0, stdout: "Client/Server", stderr: "" },
      { code: 0, stdout: "Docker Compose version v2.30", stderr: "" },
    ]);
    expect(await probeDocker(run)).toEqual({ ok: true });
  });
});

describe("composeArgs", () => {
  it("pins the project name and the file/env-file flags", () => {
    expect(composeArgs("f.yml", "e.env", ["up", "-d"])).toEqual([
      "compose",
      "-f",
      "f.yml",
      "--env-file",
      "e.env",
      "-p",
      "k2p",
      "up",
      "-d",
    ]);
  });
});
