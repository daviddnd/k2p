import { describe, expect, it } from "vitest";
import { menuFor, MenuView } from "./menu";

const enabledIds = (view: MenuView) =>
  menuFor(view).filter((i) => i.enabled).map((i) => i.id);
const allIds = (view: MenuView) => menuFor(view).map((i) => i.id);

describe("menuFor", () => {
  const base: MenuView = { status: "ready", docker: "ok", autoStart: true, tenantExists: true, platform: "linux" };

  it("ready: full menu, Start disabled", () => {
    expect(enabledIds(base)).toEqual(
      expect.arrayContaining(["open-app", "stop-stack", "backup", "refresh-images", "update-k2p", "view-logs", "quit"])
    );
    expect(enabledIds(base)).not.toContain("start-stack");
    expect(allIds(base)).not.toContain("install-docker");
  });

  it("stopped: Start enabled, backup and refresh disabled", () => {
    const view: MenuView = { ...base, status: "stopped" };
    expect(enabledIds(view)).toContain("start-stack");
    expect(enabledIds(view)).not.toContain("stop-stack");
    expect(enabledIds(view)).not.toContain("backup");
    expect(enabledIds(view)).not.toContain("refresh-images");
  });

  it("error: Start becomes the retry action", () => {
    const view: MenuView = {
      ...base,
      status: { state: "error", name: "pull_failed", detail: "i/o timeout" },
    };
    const start = menuFor(view).find((i) => i.id === "start-stack");
    expect(start?.enabled).toBe(true);
    expect(start?.label).toContain("retry");
  });

  it("docker missing: only install, logs, quit and the auto-start toggle", () => {
    const view: MenuView = { ...base, docker: "missing" };
    expect(enabledIds(view)).toEqual(
      expect.arrayContaining(["install-docker", "view-logs", "auto-start", "quit"])
    );
    expect(enabledIds(view)).not.toContain("start-stack");
    expect(enabledIds(view)).not.toContain("open-app");
  });

  it("docker daemon stopped on a Desktop platform: start-docker offered instead of start-stack", () => {
    const view: MenuView = { ...base, platform: "win32", docker: "daemon-stopped" };
    expect(enabledIds(view)).toContain("start-docker");
    expect(enabledIds(view)).not.toContain("start-stack");
  });

  it("autoStart flag is reflected on the toggle item", () => {
    const off = menuFor({ ...base, autoStart: false }).find((i) => i.id === "auto-start");
    expect(off?.checked).toBe(false);
    const on = menuFor(base).find((i) => i.id === "auto-start");
    expect(on?.checked).toBe(true);
  });

  it("open-setup is offered only when ready and no tenant exists yet", () => {
    const winBase: MenuView = { status: "ready", docker: "ok", autoStart: true, tenantExists: true, platform: "win32" };
    expect(menuFor(winBase).find((i) => i.id === "open-setup")?.enabled).toBe(false);
    expect(menuFor({ ...winBase, tenantExists: false }).find((i) => i.id === "open-setup")?.enabled).toBe(true);
    expect(menuFor({ ...winBase, tenantExists: false, status: "stopped" }).find((i) => i.id === "open-setup")?.enabled).toBe(false);
  });

  it("start-docker is only offered on platforms that ship Docker Desktop (win/mac)", () => {
    const view = (platform: NodeJS.Platform): MenuView => ({
      status: "stopped",
      docker: "daemon-stopped",
      autoStart: true,
      tenantExists: true,
      platform,
    });
    expect(menuFor(view("win32")).some((i) => i.id === "start-docker")).toBe(true);
    expect(menuFor(view("darwin")).some((i) => i.id === "start-docker")).toBe(true);
    expect(menuFor(view("linux")).some((i) => i.id === "start-docker")).toBe(false);
  });
});
