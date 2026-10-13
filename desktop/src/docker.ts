import type { Runner } from "./runner";

export type DockerProbe =
  | { ok: true }
  | { ok: false; reason: "missing" | "daemon-stopped" | "no-compose" };

/** Is docker present, its daemon up, and the compose v2 plugin installed? */
export async function probeDocker(run: Runner): Promise<DockerProbe> {
  let version: Awaited<ReturnType<Runner>>;
  try {
    version = await run("docker", ["version"]);
  } catch {
    // spawn itself failed — the binary is absent (ENOENT on a clean machine)
    return { ok: false, reason: "missing" };
  }
  if (version.code !== 0) {
    const text = (version.stderr + version.stdout).toLowerCase();
    if (
      text.includes("daemon") ||
      text.includes("docker desktop") ||
      text.includes("cannot connect") ||
      text.includes("is the docker daemon running")
    ) {
      return { ok: false, reason: "daemon-stopped" };
    }
    return { ok: false, reason: "missing" };
  }
  const compose = await run("docker", ["compose", "version"]);
  if (compose.code !== 0) {
    return { ok: false, reason: "no-compose" };
  }
  return { ok: true };
}

/** The pinned compose invocation: `docker compose -f <file> --env-file <env> -p k2p …`. */
export function composeArgs(file: string, envFile: string, args: string[]): string[] {
  return ["compose", "-f", file, "--env-file", envFile, "-p", "k2p", ...args];
}
