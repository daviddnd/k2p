import * as http from "http";
import { AddressInfo } from "net";
import { afterEach, describe, expect, it } from "vitest";
import { serveWizard } from "./wizard";

let servers: http.Server[] = [];
afterEach(async () => {
  while (servers.length) {
    const s = servers.pop()!;
    await new Promise((resolve) => s.close(resolve));
  }
});

function post(url: string, body: Record<string, string>): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const encoded = new URLSearchParams(body).toString();
    const req = http.request(
      url,
      { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" } },
      (res) => {
        let text = "";
        res.on("data", (d) => (text += d));
        res.on("end", () => resolve({ status: res.statusCode!, text }));
      }
    );
    req.on("error", reject);
    req.end(encoded);
  });
}

function get(url: string): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let text = "";
      res.on("data", (d) => (text += d));
      res.on("end", () => resolve({ status: res.statusCode!, text }));
    }).on("error", reject);
  });
}

describe("serveWizard", () => {
  it("binds loopback only, serves the form, and shuts down on close()", async () => {
    const stub = http.createServer((req, res) => {
      res.writeHead(201, { "content-type": "application/json" });
      res.end("{}");
    });
    servers.push(stub);
    await new Promise<void>((resolve) => stub.listen(0, "127.0.0.1", resolve));
    const stubUrl = `http://127.0.0.1:${(stub.address() as AddressInfo).port}`;
    const { url, close } = await serveWizard({ port: 0, baseURL: stubUrl, token: "t", onResult: () => {} });
    expect(url).toContain("127.0.0.1");
    const res = await get(url);
    expect(res.status).toBe(200);
    expect(res.text).toContain("registration code");
    expect(res.text).toContain("K2P");
    await close();
    await expect(get(url)).rejects.toThrow();
  });

  it("POSTing the form reports the bootstrap result and never bootstraps twice", async () => {
    let calls = 0;
    const stub = http.createServer((req, res) => {
      calls += 1;
      res.writeHead(calls === 1 ? 201 : 409, { "content-type": "application/json" });
      res.end("{}");
    });
    servers.push(stub);
    await new Promise<void>((resolve) => stub.listen(0, "127.0.0.1", resolve));
    const stubUrl = `http://127.0.0.1:${(stub.address() as AddressInfo).port}`;
    const w = await serveWizard({ port: 0, baseURL: stubUrl, token: "t", onResult: () => {} });
    const fields = { tenantName: "T", adminName: "N", adminEmail: "e@x.co", adminPassword: "demo-admin-pass-2026" };
    const first = await post(w.url, fields);
    expect(first.status).toBe(200);
    expect(first.text).toContain("set up");
    expect(calls).toBe(1);
    const second = await post(w.url, fields);
    expect(second.status).toBe(200);
    expect(second.text).toContain("Already");
    expect(calls).toBe(1);
    await w.close();
  });
});
