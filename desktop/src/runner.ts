import { spawn } from "child_process";

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
  /** Raw stdout bytes — present when the caller asked for binaryStdout. */
  stdoutBytes?: Buffer;
}

export type Runner = (
  cmd: string,
  args: string[],
  opts?: { input?: string; binaryStdout?: boolean }
) => Promise<RunResult>;

/**
 * The real runner: spawn a command, capture both streams, optional stdin.
 * `binaryStdout` returns stdout verbatim as bytes — text mode would corrupt
 * non-UTF-8 output (pg_dump -Fc is the case in point) with U+FFFD replacements.
 */
export const execRunner: Runner = (cmd, args, opts) =>
  new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"] });
    const chunks: Buffer[] = [];
    let stdoutText = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => {
      chunks.push(d);
      if (!opts?.binaryStdout) stdoutText += d.toString("utf-8");
    });
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", reject);
    child.on("close", (code) =>
      resolve({
        code: code ?? -1,
        stdout: opts?.binaryStdout ? "" : stdoutText,
        stdoutBytes: opts?.binaryStdout ? Buffer.concat(chunks) : undefined,
        stderr,
      })
    );
    if (opts?.input !== undefined) {
      child.stdin.write(opts.input);
    }
    child.stdin.end();
  });
