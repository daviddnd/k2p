/** Polls GET url until a 200 (bounded backoff); rejects with `timeout` past the deadline. */
export async function waitHttp(
  url: string,
  timeoutMs: number,
  fetchImpl: typeof fetch = fetch
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let delay = 200;
  for (;;) {
    try {
      const res = await fetchImpl(url);
      if (res.status === 200) return;
    } catch {
      // not up yet — keep polling until the deadline
    }
    if (Date.now() + delay > deadline) {
      throw Object.assign(new Error("readiness_timeout: the endpoint never answered 200"), {
        code: "readiness_timeout",
      });
    }
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 2_000);
  }
}
