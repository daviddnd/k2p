import * as fs from "fs";
import * as path from "path";

export interface Settings {
  autoStart: boolean;
}

const DEFAULTS: Settings = { autoStart: true };

/** Operator preferences; a missing or corrupt file yields the defaults. */
export function loadSettings(dataDir: string): Settings {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(dataDir, "settings.json"), "utf-8"));
    return { autoStart: typeof raw.autoStart === "boolean" ? raw.autoStart : DEFAULTS.autoStart };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveSettings(dataDir: string, settings: Settings): void {
  fs.writeFileSync(
    path.join(dataDir, "settings.json"),
    JSON.stringify(settings, null, 2) + "\n",
    { mode: 0o600 }
  );
}
