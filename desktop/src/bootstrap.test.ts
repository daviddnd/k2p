import * as http from "http";
import { AddressInfo } from "net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { bootstrapTenant, startActivation } from "./bootstrap";

/** A stub of the product backend: tenant bootstrap, login, activation. */
let backend: http.Server;
let backendUrl: string;
const state = {
  tenantPosts: 0,
  logins: 0,
  activationBodies: [] as Array<Record<string, unknown>>,
  lastTenantBody: {} as Record<string, unknown>,
};

beforeAll(async () => {
  backend = http.createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => {
      const parsed = body ? JSON.parse(body) : {};
      if (req.url === "/api/v1/tenants" && req.method === "POST") {
        state.tenantPosts += 1;
        if (state.tenantPosts > 1) {
          res.writeHead(409, { "content-type": "application/json" });
          res.end(JSON.stringify({ detail: "tenant already exists" }));
          return;
        }
        if (req.headers["x-setup-token"] !== "test-setup-token") {
          res.writeHead(403, { "content-type": "application/json" });
          res.end(JSON.stringify({ detail: "forbidden" }));
          return;
        }
        state.lastTenantBody = parsed;
        res.writeHead(201, { "content-type": "application/json" });
        res.end(JSON.stringify({ tenant_id: "t1" }));
        return;
      }
      if (req.url === "/api/v1/auth/login" && req.method === "POST") {
        state.logins += 1;
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ access_token: "tok" }));
        return;
      }
      if (req.url === "/api/v1/entitlement/activation" && req.method === "POST") {
        state.activationBodies.push(parsed);
        res.writeHead(200, { "content-type": "application/json" });
        if (parsed.registration_code) {
          res.end(JSON.stringify({ activated: true }));
        } else {
          res.end(JSON.stringify({ authorization_url: "https://cp.example/authorize?r=1" }));
        }
        return;
      }
      res.writeHead(404);
      res.end();
    });
  });
  await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
  backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise((resolve) => backend.close(resolve));
});

describe("bootstrapTenant", () => {
  it("posts the fields with the X-Setup-Token header and maps 201/409", async () => {
    const fields = {
      tenantName: "Demo TCSP",
      adminEmail: "demo.admin@example.com",
      adminName: "Demo Admin",
      adminPassword: "demo-admin-pass-2026",
    };
    expect(await bootstrapTenant(backendUrl, "test-setup-token", fields)).toBe("created");
    expect(state.lastTenantBody.tenant_name).toBe("Demo TCSP");
    expect(state.lastTenantBody.admin_email).toBe("demo.admin@example.com");
    expect(state.lastTenantBody).not.toHaveProperty("registrationCode");
    expect(await bootstrapTenant(backendUrl, "test-setup-token", fields)).toBe("exists");
  });

  it("maps other failures to error", async () => {
    const badUrl = backendUrl.replace("127.0.0.1", "127.0.0.2");
    expect(
      await bootstrapTenant(badUrl, "test-setup-token", {
        tenantName: "x",
        adminEmail: "a@b.c",
        adminName: "n",
        adminPassword: "demo-admin-pass-2026",
      })
    ).toBe("error");
  });
});

describe("startActivation", () => {
  it("with a code: logs in as the admin and returns silent-activated on an activated response", async () => {
    const result = await startActivation(
      backendUrl,
      "demo.admin@example.com",
      "demo-admin-pass-2026",
      "REG-CODE-1"
    );
    expect(result).toEqual({ kind: "silent-activated" });
    expect(state.logins).toBeGreaterThan(0);
    expect(state.activationBodies.at(-1)).toEqual({ registration_code: "REG-CODE-1" });
  });

  it("without a code: returns the browser authorization URL", async () => {
    const result = await startActivation(
      backendUrl,
      "demo.admin@example.com",
      "demo-admin-pass-2026",
      ""
    );
    expect(result).toEqual({ kind: "browser", authorizationUrl: "https://cp.example/authorize?r=1" });
  });
});
