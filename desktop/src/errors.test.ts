import { describe, expect, it } from "vitest";
import { errorName } from "./errors";
import { StackError } from "./stack";

describe("errorName (named failure classification)", () => {
  it("uses a coded error's code", () => {
    const e = Object.assign(new Error("timed out"), { code: "readiness_timeout" });
    expect(errorName(e, "start")).toBe("readiness_timeout");
  });

  it("falls back to `<action>_failed` for plain errors", () => {
    expect(errorName(new Error("boom"), "backup")).toBe("backup_failed");
    expect(errorName("weird", "update")).toBe("update_failed");
  });

  it("passes StackError names through untouched", () => {
    expect(errorName(new StackError("ports_in_use", "bind"), "start")).toBe("ports_in_use");
  });
});
