import * as fs from "fs";
import { composeArgs } from "./docker";
import type { Runner } from "./runner";
import { writeEnvFile } from "./env";

export type StackState = "stopped" | "starting" | "ready";
export interface StackErrorShape {
  state: "error";
  name: string;
  detail: string;
}
export type StackStatus = StackState | StackErrorShape;

export class StackError extends Error implements StackErrorShape {
  readonly state = "error" as const;
  constructor(
    readonly name: string,
    readonly detail: string
  ) {
    super(`${name}: ${detail}`);
  }
}

const READY_SERVICES = ["db", "minio", "clamav", "backend", "frontend"];
export const UP_SERVICES = ["db", "minio", "minio-init", "clamav", "backend", "frontend"];

export interface StackDeps {
  run: Runner;
  file: string;
  envFile: string;
  env: Record<string, string>;
  log?: (msg: string) => void;
}

export class Stack {
  constructor(private readonly deps: StackDeps) {}

  private compose(
    args: string[],
    opts?: { input?: string; binaryStdout?: boolean }
  ): Promise<{ code: number; stdout: string; stderr: string; stdoutBytes?: Buffer }> {
    const p = this.deps.run("docker", composeArgs(this.deps.file, this.deps.envFile, args), opts);
    void p.then((r) => {
      const tail = (r.stderr || r.stdout).trim().slice(-500);
      this.deps.log?.(`compose ${args.join(" ")} → code ${r.code}${tail ? ` | ${tail}` : ""}`);
    });
    return p;
  }

  async up(): Promise<void> {
    if (!fs.existsSync(this.deps.envFile)) {
      this.deps.log?.(`writing ${this.deps.envFile}`);
      writeEnvFile(this.deps.envFile, this.deps.env);
    }
    const result = await this.compose(["up", "-d", ...UP_SERVICES]);
    if (result.code !== 0) {
      throw classifyUpFailure(result.stderr);
    }
  }

  async down(): Promise<void> {
    const result = await this.compose(["stop"]);
    if (result.code !== 0) {
      throw new StackError("stop_failed", result.stderr);
    }
  }

  async refreshImages(): Promise<void> {
    const pull = await this.compose(["pull"]);
    if (pull.code !== 0) {
      throw new StackError("pull_failed", pull.stderr);
    }
    const up = await this.compose(["up", "-d", ...UP_SERVICES]);
    if (up.code !== 0) {
      throw classifyUpFailure(up.stderr);
    }
  }

  async status(): Promise<StackStatus> {
    const result = await this.compose(["ps", "--services", "--status", "running"]);
    if (result.code !== 0) {
      return { state: "error", name: "status_failed", detail: result.stderr };
    }
    const running = new Set(result.stdout.split("\n").map((s) => s.trim()).filter(Boolean));
    const readyCount = READY_SERVICES.filter((s) => running.has(s)).length;
    if (readyCount === READY_SERVICES.length) return "ready";
    if (readyCount === 0) return "stopped";
    return "starting";
  }

  /** The upgrade runbook's migration step, as a one-shot compose job. */
  async migrate(): Promise<void> {
    const result = await this.compose(["run", "--rm", "backend", "alembic", "upgrade", "head"]);
    if (result.code !== 0) {
      throw new StackError("migration_failed", result.stderr || result.stdout);
    }
  }

  /** Readiness: the backend answers healthz. Named `timeout` on deadline. */
  async waitReady(timeoutMs: number): Promise<void> {
    const { waitHttp } = await import("./health");
    await waitHttp("http://127.0.0.1:8000/healthz", timeoutMs);
  }
}

export function classifyUpFailure(stderr: string): StackError {
  const text = stderr.toLowerCase();
  if (text.includes("port is already allocated") || text.includes("address already in use")) {
    return new StackError("ports_in_use", stderr);
  }
  if (
    text.includes("pull") ||
    text.includes("i/o timeout") ||
    text.includes("network is unreachable") ||
    text.includes("temporary failure in name resolution") ||
    text.includes("connection refused") // registry-side
  ) {
    return new StackError("pull_failed", stderr);
  }
  return new StackError("up_failed", stderr);
}
