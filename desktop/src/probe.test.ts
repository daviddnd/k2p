import * as http from "http";
import { AddressInfo } from "net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { bootstrapTenant, probeTenantExists } from "./bootstrap";

let backend: http.Server;
let backendUrl: string;
let mode: "bootstrap-state" | "legacy" | "unreachable" = "bootstrap-state";
let tenantExists = false;
let tenantPosts = 0;

beforeAll(async () => {
  backend = http.createServer((req, res) => {
    if (req.url === "/api/v1/bootstrap-state" && req.method === "GET") {
      if (mode === "bootstrap-state") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ tenant_exists: tenantExists }));
        return;
      }
      res.writeHead(404, { "content-type": "application/json" });
      res.end("{}");
      return;
    }
    if (req.url === "/api/v1/tenants" && req.method === "POST") {
      tenantPosts += 1;
      res.writeHead(tenantExists ? 409 : 422, { "content-type": "application/json" });
      res.end("{}");
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise<void>((resolve) => backend.listen(0, "127.0.0.1", resolve));
  backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise((resolve) => backend.close(resolve));
});

describe("probeTenantExists", () => {
  it("reads the bootstrap-state endpoint when the backend has it", async () => {
    mode = "bootstrap-state";
    tenantExists = true;
    expect(await probeTenantExists(backendUrl, "tok")).toBe(true);
    tenantExists = false;
    expect(await probeTenantExists(backendUrl, "tok")).toBe(false);
  });

  it("falls back to the legacy empty-POST probe on older backends", async () => {
    mode = "legacy";
    tenantExists = true;
    const before = tenantPosts;
    expect(await probeTenantExists(backendUrl, "tok")).toBe(true);
    expect(tenantPosts).toBe(before + 1);
  });

  it("treats an unreachable backend as not-yet-bootstrapped", async () => {
    expect(await probeTenantExists("http://127.0.0.1:9", "tok")).toBe(false);
  });
});
