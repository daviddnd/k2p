import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";
import { Stack } from "./stack";

export class UpdateError extends Error {
  constructor(
    readonly name: string,
    readonly detail: string
  ) {
    super(`${name}: ${detail}`);
  }
}

export interface UpdateCheck {
  update: boolean;
  version?: string;
  url?: string;
  sha256?: string;
}

/** The release manifest the checkUpdate call polls; set at release time. */
export const DEFAULT_MANIFEST_URL = "https://releases.kairospinax.com/desktop/manifest.json";

function semverLessThan(a: string, b: string): boolean {
  const pa = a.split(".").map((n) => parseInt(n, 10) || 0);
  const pb = b.split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) < (pb[i] ?? 0);
  }
  return false;
}

export async function checkUpdate(
  manifestUrl: string,
  currentVersion: string,
  fetchImpl: typeof fetch = fetch
): Promise<UpdateCheck> {
  const res = await fetchImpl(manifestUrl);
  if (res.status !== 200) {
    return { update: false };
  }
  const manifest = (await res.json()) as { version?: string; url?: string; sha256?: string };
  if (!manifest.version || !semverLessThan(currentVersion, manifest.version)) {
    return { update: false };
  }
  return {
    update: true,
    version: manifest.version,
    url: manifest.url,
    sha256: manifest.sha256,
  };
}

export interface ApplyDeps {
  stack: Stack;
  downloadsDir: string;
  open: (path: string) => Promise<void>;
  fetchImpl?: typeof fetch;
  /** Manifest digest: when present, the downloaded installer must match. */
  sha256?: string;
}

/**
 * Download-and-replace (D7): fetch the signed installer into the downloads
 * directory, verify its SHA-256 against the manifest when one was supplied,
 * then hand it to the OS opener; the stack must be Ready so the update never
 * runs mid-failure.
 */
export async function applyUpdate(
  deps: ApplyDeps,
  release: { url: string }
): Promise<{ installer: string }> {
  const status = await deps.stack.status();
  if (status !== "ready") {
    throw new UpdateError("stack_not_ready", `status is ${typeof status === "object" ? status.name : status}`);
  }
  const target = path.join(deps.downloadsDir, path.basename(new URL(release.url).pathname));
  const res = await (deps.fetchImpl ?? fetch)(release.url);
  if (res.status !== 200) {
    throw new UpdateError("download_failed", `${res.status}`);
  }
  const bytes = Buffer.from(await res.arrayBuffer());
  if (deps.sha256) {
    const digest = createHash("sha256").update(bytes).digest("hex");
    if (digest !== deps.sha256.toLowerCase()) {
      throw new UpdateError("digest_mismatch", `${digest} ≠ manifest ${deps.sha256}`);
    }
  }
  fs.writeFileSync(target, bytes);
  await deps.open(target);
  return { installer: target };
}
