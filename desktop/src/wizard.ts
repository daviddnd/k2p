import * as fs from "fs";
import * as http from "http";
import { AddressInfo } from "net";
import * as path from "path";
import { bootstrapTenant, TenantFields } from "./bootstrap";

export interface WizardDeps {
  port: number; // 0 = ephemeral
  baseURL: string; // the product backend
  token: string; // the setup token
  onResult: (result: "created" | "exists") => void;
  /** D11 seam: invoked with the submitted fields after a successful bootstrap —
   * the shell performs the activation handoff (silent or browser) here. */
  onCreated?: (fields: TenantFields) => Promise<void>;
}

export interface WizardHandle {
  url: string;
  close: () => Promise<void>;
}

const PAGE = `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>K2P — first-run setup</title>
<style>body{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}
label{display:block;margin:.75rem 0 .15rem}input{width:100%;padding:.4rem}
button{margin-top:1.25rem;padding:.6rem 1.2rem}p{color:#444}</style></head>
<body>
<h1>K2P — first-run setup</h1>
<p>Create your tenant and its Administrator. The optional registration code (from
the vendor, if you registered on the website) activates your licence immediately.</p>
<form method="post" action="/">
<label>TCSP / company name<input name="tenantName" required></label>
<label>Administrator name<input name="adminName" required></label>
<label>Administrator email<input name="adminEmail" type="email" required></label>
<label>Administrator password<input name="adminPassword" type="password" minlength="12" required></label>
<label>Registration code (optional)<input name="registrationCode"></label>
<button type="submit">Create my K2P</button>
</form>
</body></html>`;

const SUCCESS = `<!doctype html><html><head><meta charset="utf-8"><title>K2P</title></head>
<body><h1>K2P is set up</h1><p>K2P is opening in your browser.</p></body></html>`;

const ALREADY = `<!doctype html><html><head><meta charset="utf-8"><title>K2P</title></head>
<body><h1>Already set up</h1><p>This K2P deployment already has its tenant — sign in at
<a href="http://localhost:5173">the app</a>.</p></body></html>`;

function readPage(): string {
  const bundled = path.join(__dirname, "..", "assets", "wizard-page.html");
  return fs.existsSync(bundled) ? fs.readFileSync(bundled, "utf-8") : PAGE;
}

/** Loopback-only first-run server; lives until the tenant exists (or the app quits). */
export async function serveWizard(deps: WizardDeps): Promise<WizardHandle> {
  let bootstrapped: "created" | "exists" | null = null;
  const server = http.createServer((req, res) => {
    const respond = (status: number, html: string) => {
      res.writeHead(status, { "content-type": "text/html; charset=utf-8" });
      res.end(html);
    };
    if (req.method === "GET" && req.url === "/") {
      respond(200, readPage());
      return;
    }
    if (req.method === "POST" && req.url === "/") {
      let body = "";
      req.on("data", (d) => (body += d));
      req.on("end", async () => {
        if (bootstrapped) {
          respond(200, ALREADY);
          return;
        }
        const parsed = new URLSearchParams(body);
        const fields: TenantFields = {
          tenantName: parsed.get("tenantName") ?? "",
          adminEmail: parsed.get("adminEmail") ?? "",
          adminName: parsed.get("adminName") ?? "",
          adminPassword: parsed.get("adminPassword") ?? "",
          registrationCode: parsed.get("registrationCode") || undefined,
        };
        const result = await bootstrapTenant(deps.baseURL, deps.token, fields);
        if (result === "error") {
          respond(502, `<!doctype html><html><body><h1>Setup failed</h1>
<p>The backend refused the bootstrap — check the logs and try again.</p>
<p><a href="/">Back</a></p></body></html>`);
          return;
        }
        bootstrapped = result;
        deps.onResult(result);
        if (result === "created" && deps.onCreated) {
          // Activation failure must not fail the bootstrap — the admin can
          // activate later from the product UI.
          await deps.onCreated(fields).catch(() => {});
        }
        respond(200, SUCCESS);
      });
      return;
    }
    respond(404, "not found");
  });
  await new Promise<void>((resolve) => server.listen(deps.port, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return {
    url,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
