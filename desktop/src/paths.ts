export interface Layout {
  dataDir: string;
  envFile: string;
  credentialDir: string;
  logDir: string;
}

function joinFor(platform: NodeJS.Platform, ...parts: string[]): string {
  const sep = platform === "win32" ? "\\" : "/";
  return parts.filter(Boolean).join(sep);
}

/** Pure: resolves the per-OS data layout for a platform without touching disk. */
export function layoutFor(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv
): Layout {
  let dataDir: string;
  switch (platform) {
    case "win32":
      dataDir = joinFor(
        platform,
        env.LOCALAPPDATA ?? joinFor(platform, env.USERPROFILE ?? "", "AppData", "Local"),
        "K2P"
      );
      break;
    case "darwin":
      dataDir = joinFor(platform, env.HOME ?? "", "Library", "Application Support", "K2P");
      break;
    default:
      dataDir = joinFor(
        platform,
        env.XDG_DATA_HOME ?? joinFor(platform, env.HOME ?? "", ".local", "share"),
        "k2p"
      );
      break;
  }
  if (env.K2P_DATA_DIR) {
    dataDir = env.K2P_DATA_DIR;
  }
  return {
    dataDir,
    envFile: joinFor(platform, dataDir, "k2p.env"),
    credentialDir: joinFor(platform, dataDir, "credentials"),
    logDir: joinFor(platform, dataDir, "logs"),
  };
}

/** Creates the layout's directories; called once at app startup, never in tests. */
export function ensureLayout(layout: Layout): void {
  const fs = require("fs") as typeof import("fs");
  for (const dir of [layout.dataDir, layout.credentialDir, layout.logDir]) {
    fs.mkdirSync(dir, { recursive: true });
  }
  if (process.platform !== "win32") {
    // The custody dir holds credential material — never world-readable/traversable.
    try {
      fs.chmodSync(layout.credentialDir, 0o700);
    } catch {
      // best effort; the files inside carry their own 0600 modes
    }
  }
}
