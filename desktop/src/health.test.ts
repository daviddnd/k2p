import { describe, expect, it } from "vitest";
import { waitHttp } from "./health";

function sequenceFetch(codes: number[]): typeof fetch {
  let i = 0;
  return (async () => {
    const code = codes[Math.min(i++, codes.length - 1)];
    return new Response("", { status: code });
  }) as unknown as typeof fetch;
}

describe("waitHttp", () => {
  it("resolves once the endpoint answers 200, tolerating earlier failures", async () => {
    const fetchImpl = sequenceFetch([500, 500, 200]);
    await expect(waitHttp("http://x/healthz", 5_000, fetchImpl)).resolves.toBeUndefined();
  });

  it("rejects with a named timeout error when 200 never arrives", async () => {
    const fetchImpl = sequenceFetch([500]);
    const err = await waitHttp("http://x/healthz", 30, fetchImpl).catch((e) => e);
    expect(err.code).toBe("readiness_timeout");
    expect(err.message).toContain("readiness_timeout");
  });
});
