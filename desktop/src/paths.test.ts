import { describe, expect, it } from "vitest";
import { ensureLayout, layoutFor } from "./paths";

describe("layoutFor", () => {
  it("win32 uses LOCALAPPDATA\\K2P", () => {
    const l = layoutFor("win32", { LOCALAPPDATA: "C:\\Users\\x\\AppData\\Local" });
    expect(l.dataDir).toBe("C:\\Users\\x\\AppData\\Local\\K2P");
    expect(l.envFile).toBe(l.dataDir + "\\k2p.env");
  });

  it("darwin uses ~/Library/Application Support/K2P", () => {
    const l = layoutFor("darwin", { HOME: "/home/x" });
    expect(l.dataDir).toBe("/home/x/Library/Application Support/K2P");
    expect(l.envFile).toBe(l.dataDir + "/k2p.env");
  });

  it("respects K2P_DATA_DIR override on any platform", () => {
    const l = layoutFor("linux", { HOME: "/home/x", K2P_DATA_DIR: "/srv/k2p" });
    expect(l.dataDir).toBe("/srv/k2p");
  });

  it("credential and log dirs live under dataDir", () => {
    const l = layoutFor("linux", { HOME: "/home/x" });
    expect(l.credentialDir).toBe(l.dataDir + "/credentials");
    expect(l.logDir).toBe(l.dataDir + "/logs");
  });
});

describe("credential dir permissions", () => {
  it("creates the credentials dir 0700 on POSIX", () => {
    if (process.platform === "win32") return;
    const fs = require("fs") as typeof import("fs");
    const dir = fs.mkdtempSync(require("os").tmpdir() + "/k2p-perm-test-");
    try {
      const layout = layoutFor("linux", { HOME: dir });
      ensureLayout(layout);
      expect(fs.statSync(layout.credentialDir).mode & 0o777).toBe(0o700);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
