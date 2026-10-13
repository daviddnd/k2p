import { StackStatus } from "./stack";

export type DockerState = "ok" | "missing" | "daemon-stopped" | "no-compose";

export interface MenuView {
  status: StackStatus;
  docker: DockerState;
  autoStart: boolean;
  /** Whether the deployment already has its tenant (drives the setup item). */
  tenantExists: boolean;
  /** The host platform — some items only make sense where Docker Desktop ships. */
  platform: NodeJS.Platform;
}

export interface MenuItem {
  id: string;
  label: string;
  enabled: boolean;
  checked?: boolean;
}
export type MenuModel = MenuItem[];

/** The whole tray/menu content as data — the Electron seam renders this verbatim. */
export function menuFor(view: MenuView): MenuModel {
  const items: MenuModel = [{ id: "open-app", label: "Open K2P", enabled: view.docker === "ok" && view.status === "ready" }];
  if (view.docker === "missing") {
    items.push({ id: "install-docker", label: "Install Docker Desktop…", enabled: true });
  }
  if (view.docker === "daemon-stopped" && (view.platform === "win32" || view.platform === "darwin")) {
    items.push({ id: "start-docker", label: "Start Docker Desktop…", enabled: true });
  }
  const isError = typeof view.status === "object";
  items.push({
    id: "start-stack",
    label: isError ? "Start stack (retry)" : "Start stack",
    enabled: view.docker === "ok" && (view.status === "stopped" || view.status === "starting" || isError),
  });
  items.push({
    id: "stop-stack",
    label: "Stop stack",
    enabled: view.docker === "ok" && view.status === "ready",
  });
  items.push({
    id: "open-setup",
    label: "Open first-run setup…",
    enabled: view.status === "ready" && !view.tenantExists,
  });
  items.push({ id: "backup", label: "Backup…", enabled: view.status === "ready" });
  items.push({ id: "refresh-images", label: "Refresh images", enabled: view.status === "ready" });
  items.push({ id: "update-k2p", label: "Update K2P…", enabled: true });
  items.push({ id: "view-logs", label: "View logs", enabled: true });
  items.push({ id: "auto-start", label: "Start K2P on login", enabled: true, checked: view.autoStart });
  items.push({ id: "quit", label: "Quit K2P", enabled: true });
  return items;
}
