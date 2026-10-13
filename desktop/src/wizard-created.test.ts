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

describe("serveWizard onCreated (D11 wiring seam)", () => {
  it("invokes onCreated with the submitted fields, then reports activation in the success page", async () => {
    const seen: Array<Record<string, string | undefined>> = [];
    const stub = http.createServer((req, res) => {
      res.writeHead(201, { "content-type": "application/json" });
      res.end("{}");
    });
    servers.push(stub);
    await new Promise<void>((resolve) => stub.listen(0, "127.0.0.1", resolve));
    const stubUrl = `http://127.0.0.1:${(stub.address() as AddressInfo).port}`;
    const w = await serveWizard({
      port: 0,
      baseURL: stubUrl,
      token: "t",
      onResult: () => {},
      onCreated: async (fields) => {
        seen.push({ ...fields });
      },
    });
    const res = await post(w.url, {
      tenantName: "T",
      adminName: "N",
      adminEmail: "e@x.co",
      adminPassword: "demo-admin-pass-2026",
      registrationCode: "REG-1",
    });
    expect(res.status).toBe(200);
    expect(res.text).toContain("set up");
    expect(seen.length).toBe(1);
    expect(seen[0].registrationCode).toBe("REG-1");
    expect(seen[0].adminEmail).toBe("e@x.co");
    await w.close();
  });
});
