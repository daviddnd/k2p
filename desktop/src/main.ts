import { app, Menu, nativeImage, Notification, shell, Tray } from "electron";
import * as fs from "fs";
import * as path from "path";
import { probeDocker } from "./docker";
import { errorName } from "./errors";
import { generateEnv } from "./env";
import { ensureLayout, layoutFor } from "./paths";
import { REMEDIES } from "./remedies";
import { execRunner } from "./runner";
import { loadSettings, saveSettings } from "./settings";
import { Stack, StackError, StackStatus } from "./stack";
import { menuFor, MenuView } from "./menu";
import { serveWizard } from "./wizard";
import { startActivation } from "./bootstrap";

// The K2P tray mark: white "K" on the brand-accent tile (--accent,
// frontend/src/index.css), 32x32 RGBA, generated asset embedded so the
// packaged app needs no extra files.
const TILE_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAABF0lEQVR42mNgGIyAM/NWA2fmrf9Uxg3EWOxAA4vRscNAWo7bEXS0HIxpFueKFff+N29+83/Thc9gWqH8LuE0QS3LrTse/T9489t/ZLD+3Of/IgV38IcCpRZzZ936HzHz2f+7r3/9Rwcfv/39b9n+kDIHoIOWLW/hcqIFd/7XrH/9//3Xv/+xAZo6ABTfi459/P/z97//uABVogCbA7DFNzIAOQrkODyJkHwH7L3+FWt8wwAoOkDRIorD5xQ7AB8AOQyUIEEJk+iygFoOAEUJKGpILowodQAsvkGJkqzSkBIHEBvfw9cBAx4FgyIR0j0bDnhBNOBF8aCojOhaHQ94g4QeTbKBbZQOeLN8UHRMBrxrNig6pwMBALflIUyJATvyAAAAAElFTkSuQmCC";

const APP_URL = "http://localhost:5173";
const BACKEND_URL = "http://127.0.0.1:8000";

async function main(): Promise<void> {
  const layout = layoutFor(process.platform, process.env);
  ensureLayout(layout);
  const log = (msg: string) =>
    fs.appendFileSync(path.join(layout.logDir, "app.log"), `${new Date().toISOString()} ${msg}\n`);
  const envFile = layout.envFile;
  if (!fs.existsSync(envFile)) {
    // First ever launch: the secrets are written by Stack.up(); generate now
    // so the wizard's tenant bootstrap can hand the setup token to the stack.
    const { writeEnvFile } = await import("./env");
    writeEnvFile(envFile, generateEnv(undefined, app.getVersion(), layout.credentialDir));
  }
  const env = Object.fromEntries(
    fs.readFileSync(envFile, "utf-8").trim().split("\n").map((line) => {
      const idx = line.indexOf("=");
      return [line.slice(0, idx), line.slice(idx + 1)];
    })
  );
  // Remote support needs a starting point on a customer's machine: the launch
  // fact line is unconditional. Never log env contents — versions and probe
  // results only, no secrets ever (k2p.env values stay out of app.log).
  log(`launcher start version=${app.getVersion()} platform=${process.platform} envVersion=${env.K2P_VERSION ?? "unset"}`);

  // docker compose is an external binary and cannot read inside the packaged
  // asar archive, so the bundled pinned compose is extracted next to the env
  // file at startup (Electron's fs reads asar paths; the docker CLI cannot).
  const composeFile = path.join(layout.dataDir, "desktop.compose.yml");
  fs.writeFileSync(
    composeFile,
    fs.readFileSync(path.join(__dirname, "..", "assets", "desktop.compose.yml"))
  );
  const stack = new Stack({
    run: execRunner,
    file: composeFile,
    envFile,
    env,
    log: (msg) => fs.appendFileSync(path.join(layout.logDir, "stack.log"), `${new Date().toISOString()} ${msg}\n`),
  });

  let docker: MenuView["docker"] = "ok";
  let status: StackStatus = "stopped";
  let tenantExists = false;
  let settings = loadSettings(layout.dataDir);
  let tray: Tray | null = null;

  // Every status/docker mutation flows through render(), so one seam there
  // records each transition once — the support timeline. State names only,
  // same no-secrets rule as the launch line.
  let loggedState: string | null = null;
  const logState = (): void => {
    const state = typeof status === "object" ? `error:${status.name}` : status;
    if (state === loggedState) return;
    loggedState = state;
    log(`state ${state}; docker ${docker}`);
  };

  async function refresh(): Promise<void> {
    try {
      const probe = await probeDocker(execRunner);
      docker = probe.ok ? "ok" : probe.reason;
      if (probe.ok) {
        try {
          status = await stack.status();
        } catch {
          status = { state: "error", name: "status_failed", detail: "see logs" };
        }
      }
    } catch (e) {
      docker = "missing";
      log(`docker probe failed: ${e instanceof Error ? e.message : String(e)}`);
    }
    render();
  }

  function render(): void {
    logState();
    if (!tray) return;
    const view: MenuView = {
      status,
      docker,
      autoStart: settings.autoStart,
      tenantExists,
      platform: process.platform,
    };
    const template = menuFor(view).map((item) => ({
      label: item.label,
      enabled: item.enabled,
      type: item.checked !== undefined ? ("checkbox" as const) : ("normal" as const),
      checked: item.checked,
      click: () => void dispatch(item.id),
    }));
    tray.setContextMenu(Menu.buildFromTemplate(template));
    tray.setToolTip(
      typeof status === "object" ? `K2P — error: ${status.name}` : `K2P — ${docker === "ok" ? status : docker}`
    );
  }

  async function dispatch(id: string): Promise<void> {
    switch (id) {
      case "open-app":
        await shell.openExternal(APP_URL);
        break;
      case "install-docker":
        await shell.openExternal("https://www.docker.com/products/docker-desktop/");
        break;
      case "start-docker":
        if (process.platform === "darwin") {
          await shell.openPath("/Applications/Docker.app");
        } else if (process.platform === "win32") {
          await shell.openPath("C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe");
        }
        break;
      case "start-stack":
        await run("start", async () => {
          await stack.up();
          status = "starting";
          render();
          await stack.migrate();
          await stack.waitReady(5 * 60_000);
          status = "ready";
          await maybeFirstRun();
        });
        break;
      case "stop-stack":
        await run("stop", async () => {
          await stack.down();
          status = "stopped";
        });
        break;
      case "refresh-images":
        await run("refresh", async () => {
          await stack.refreshImages();
          status = "ready";
        });
        break;
      case "view-logs":
        await shell.openPath(layout.logDir);
        break;
      case "open-setup":
        await maybeFirstRun();
        break;
      case "auto-start":
        settings = { autoStart: !settings.autoStart };
        saveSettings(layout.dataDir, settings);
        app.setLoginItemSettings({ openAtLogin: settings.autoStart });
        break;
      case "backup": {
        const dest = path.join(layout.dataDir, "backups");
        fs.mkdirSync(dest, { recursive: true });
        await run("backup", async () => {
          const { runBackup } = await import("./backup");
          const { archive } = await runBackup({
            run: execRunner,
            stack,
            dataDir: layout.dataDir,
            destDir: dest,
            envFile,
            composeFile,
            env,
          });
          log(`backup written: ${archive}`);
        });
        break;
      }
      case "update-k2p": {
        await run("update", async () => {
          const { applyUpdate, checkUpdate, DEFAULT_MANIFEST_URL } = await import("./update");
          const release = await checkUpdate(DEFAULT_MANIFEST_URL, app.getVersion());
          if (release.update && release.url) {
            await applyUpdate(
              {
                stack,
                downloadsDir: app.getPath("downloads"),
                open: (p) => shell.openPath(p).then(() => {}),
                sha256: release.sha256,
              },
              { url: release.url }
            );
          } else {
            log("K2P is up to date");
          }
        });
        break;
      }
      case "quit":
        app.quit();
        break;
    }
    render();
  }

  async function run(name: string, action: () => Promise<void>): Promise<void> {
    try {
      await action();
    } catch (e) {
      log(`action ${name} failed: ${e instanceof Error ? e.message : String(e)}`);
      const named = errorName(e, name);
      status = {
        state: "error",
        name: named,
        detail: e instanceof Error ? e.message : String(e),
      };
      // The named-error convention lands in-app: name + the remedy sentence.
      if (Notification.isSupported()) {
        new Notification({ title: `K2P — ${named}`, body: REMEDIES[named] ?? status.detail }).show();
      }
    }
  }

  async function maybeFirstRun(): Promise<void> {
    // The honest read (product bootstrap-state endpoint, setup-token guarded);
    // the empty-POST probe lives on inside probeTenantExists as the legacy
    // fallback for older backends.
    const { probeTenantExists } = await import("./bootstrap");
    tenantExists = await probeTenantExists(BACKEND_URL, env.AICS_SETUP_TOKEN ?? "");
    render();
    if (tenantExists) {
      await shell.openExternal(APP_URL);
      return;
    }
    // The empty probe failed with 4xx/transport — treat as not-yet-bootstrapped
    // only when the backend is actually up; otherwise the wizard will 502 visibly.
    const wizard = await serveWizard({
      port: 0,
      baseURL: BACKEND_URL,
      token: env.AICS_SETUP_TOKEN ?? "",
      onResult: () => void shell.openExternal(APP_URL),
      onCreated: async (fields) => {
        // D11 + ADR-0016 amendment: with a code the activation completes
        // silently; without one the browser handshake opens for confirmation.
        const result = await startActivation(
          BACKEND_URL,
          fields.adminEmail,
          fields.adminPassword,
          fields.registrationCode ?? ""
        );
        if (result.kind === "browser") {
          await shell.openExternal(result.authorizationUrl);
        }
      },
    });
    await shell.openExternal(wizard.url);
  }

  await app.whenReady();
  app.setLoginItemSettings({ openAtLogin: settings.autoStart });
  tray = new Tray(nativeImage.createFromDataURL(`data:image/png;base64,${TILE_PNG}`));
  tray.on("click", () => void dispatch("open-app"));
  await refresh();
  setInterval(() => void refresh(), 5_000);

  // Bring the stack up on launch (the "K2P 自启" behavior).
  if (docker === "ok" && status === "stopped") {
    await dispatch("start-stack");
  }
}

// One instance owns the tray, the env file and the stack; a second launch
// (double-click, installer's "Launch K2P", login item) must not race it —
// two composers on one project is exactly how pulls and state get corrupted.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (Notification.isSupported()) {
      new Notification({
        title: "K2P",
        body: "K2P is already running — its icon is in the notification area, by the clock.",
      }).show();
    }
  });
  void main();
}
