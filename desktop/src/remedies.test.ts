import { describe, expect, it } from "vitest";
import { REMEDIES } from "./remedies";
import { classifyUpFailure, StackError } from "./stack";
import { waitHttp } from "./health";

describe("REMEDIES covers every error name the shell can produce", () => {
  it("has a remedy for each named state", () => {
    for (const name of [
      "ports_in_use",
      "pull_failed",
      "up_failed",
      "migration_failed",
      "stop_failed",
      "status_failed",
      "stack_not_ready",
      "pg_dump_failed",
      "mirror_failed",
      "copy_failed",
      "archive_failed",
      "download_failed",
      "digest_mismatch",
      "readiness_timeout",
    ]) {
      expect(REMEDIES[name], `missing remedy for ${name}`).toBeTruthy();
    }
  });
});

describe("classifyUpFailure narrowing (the 'network' substring fix)", () => {
  it("does NOT classify compose network-state errors as pull failures", () => {
    const err = classifyUpFailure("Error: network k2p_default not found");
    expect(err).toBeInstanceOf(StackError);
    expect(err.name).toBe("up_failed");
  });

  it("still classifies genuine network-unreachable errors as pull failures", () => {
    expect(classifyUpFailure("error pulling image: network is unreachable").name).toBe("pull_failed");
    expect(classifyUpFailure("error pulling image: i/o timeout").name).toBe("pull_failed");
  });
});

describe("waitHttp named timeout", () => {
  it("rejects with a coded readiness_timeout error", async () => {
    const never200 = (async () => new Response("", { status: 500 })) as unknown as typeof fetch;
    const err = await waitHttp("http://x/", 30, never200).catch((e) => e);
    expect(err.code).toBe("readiness_timeout");
    expect(err.message).toContain("readiness_timeout");
  });
});
