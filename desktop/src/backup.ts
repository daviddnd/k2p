import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";
import { composeArgs } from "./docker";
import type { Runner } from "./runner";
import { redactEnv } from "./env";
import { Stack } from "./stack";

export class BackupError extends Error {
  constructor(
    readonly name: string,
    readonly detail: string
  ) {
    super(`${name}: ${detail}`);
  }
}

export interface BackupDeps {
  run: Runner;
  stack: Stack;
  dataDir: string;
  destDir: string;
  envFile: string;
  composeFile: string;
  env: Record<string, string>;
  now?: Date;
}

function compose(
  deps: BackupDeps,
  args: string[],
  opts?: { binaryStdout?: boolean }
): ReturnType<Runner> {
  return deps.run("docker", composeArgs(deps.composeFile, deps.envFile, args), opts);
}

/**
 * The backup.sh policy, container-side: refuse to half-run unless the stack is
 * ready; dump postgres through the db service; mirror the bucket with the
 * MinIO image's own mc; archive with a manifest that lists credential paths
 * by NAME ONLY — their contents are never read.
 */
export async function runBackup(deps: BackupDeps): Promise<{ archive: string; manifest: string }> {
  const status = await deps.stack.status();
  if (status !== "ready") {
    throw new BackupError("stack_not_ready", `status is ${typeof status === "object" ? status.name : status}`);
  }

  const stamp = (deps.now ?? new Date()).toISOString().replace(/[-:]/g, "").replace(/\..+/, "Z").replace("T", "T");
  const staging = path.join(deps.dataDir, "backup-staging");
  fs.rmSync(staging, { recursive: true, force: true });
  fs.mkdirSync(path.join(staging, "postgres"), { recursive: true });
  fs.mkdirSync(path.join(staging, "objects"), { recursive: true });
  fs.mkdirSync(path.join(staging, "config"), { recursive: true });

  // 1. pg_dump through the running db service — binary format (-Fc), so the
  // runner must return raw bytes; a text decode would corrupt the archive.
  const dump = await compose(deps, ["exec", "-T", "db", "pg_dump", "-U", "aics", "-d", "aics", "-Fc"], {
    binaryStdout: true,
  });
  if (dump.code !== 0) {
    fs.rmSync(staging, { recursive: true, force: true });
    throw new BackupError("pg_dump_failed", dump.stderr);
  }
  fs.writeFileSync(
    path.join(staging, "postgres", "custom.dump"),
    dump.stdoutBytes ?? Buffer.from(dump.stdout, "latin1")
  );

  // 2. Object mirror, server-side inside the minio container, then cp out.
  const alias = `mc alias set local http://localhost:9000 $${"MINIO_ROOT_USER"} $${"MINIO_ROOT_PASSWORD"}`;
  const mirror = await compose(deps, [
    "exec",
    "-T",
    "minio",
    "sh",
    "-c",
    `${alias} && mc mirror --overwrite local/${deps.env.AICS_S3_BUCKET ?? "aics-documents"} /tmp/k2p-backup-objects`,
  ]);
  if (mirror.code !== 0) {
    fs.rmSync(staging, { recursive: true, force: true });
    throw new BackupError("mirror_failed", mirror.stderr);
  }
  const cp = await compose(deps, ["cp", "minio:/tmp/k2p-backup-objects", path.join(staging, "objects")]);
  await compose(deps, ["exec", "-T", "minio", "rm", "--recursive", "--force", "/tmp/k2p-backup-objects"]);
  if (cp.code !== 0) {
    fs.rmSync(staging, { recursive: true, force: true });
    throw new BackupError("copy_failed", cp.stderr);
  }

  // 3. Config bundle: the pinned compose, the redacted env, the image digests.
  fs.copyFileSync(deps.composeFile, path.join(staging, "config", "desktop.compose.yml"));
  fs.writeFileSync(
    path.join(staging, "config", "env.redacted"),
    `# Redacted copy: keys matching PASSWORD|SECRET|KEY|TOKEN replaced.\n` +
      Object.entries(redactEnv(deps.env))
        .map(([k, v]) => `${k}=${v}`)
        .join("\n") + "\n"
  );
  const images = await compose(deps, ["images"]);
  fs.writeFileSync(path.join(staging, "config", "images.txt"), images.stdout);

  // 4. Manifest: sizes + SHA-256; credential paths names only, never contents.
  const manifestPath = path.join(staging, "MANIFEST.txt");
  const lines: string[] = [
    "# AICS backup manifest",
    `created_utc: ${(deps.now ?? new Date()).toISOString()}`,
    "members_sha256:",
  ];
  const walk = (dir: string, rel: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(dir, entry.name);
      const relPath = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        walk(full, relPath);
      } else if (entry.name !== "MANIFEST.txt") {
        const buf = fs.readFileSync(full);
        lines.push(`${createHash("sha256").update(buf).digest("hex")}  ${buf.length}  ${relPath}`);
      }
    }
  };
  walk(staging, "");
  lines.push("# Credential material is NEVER included in a backup archive.");
  lines.push("# credential paths are names only (contents are never read):");
  const credDir = path.join(deps.dataDir, "credentials");
  if (fs.existsSync(credDir)) {
    for (const entry of fs.readdirSync(credDir).sort()) {
      lines.push(`credentials/${entry}`);
    }
  }
  fs.writeFileSync(manifestPath, lines.join("\n") + "\n");
  const manifestOut = path.join(deps.destDir, `MANIFEST-${stamp}.txt`);
  fs.copyFileSync(manifestPath, manifestOut);

  // 5. One timestamped archive at the destination.
  const archive = path.join(deps.destDir, `aics-backup-${stamp}.tar.gz`);
  const tar = await deps.run("tar", ["-C", staging, "-czf", archive, "."]);
  fs.rmSync(staging, { recursive: true, force: true });
  if (tar.code !== 0) {
    throw new BackupError("archive_failed", tar.stderr);
  }
  return { archive, manifest: manifestOut };
}
