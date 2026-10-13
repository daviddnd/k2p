export interface TenantFields {
  tenantName: string;
  adminEmail: string;
  adminName: string;
  adminPassword: string;
  registrationCode?: string;
}

async function postJSON(
  url: string,
  body: unknown,
  headers: Record<string, string> = {}
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: (await res.json().catch(() => ({}))) as Record<string, unknown> };
}

/** The one unauthenticated write: tenant bootstrap, guarded by the setup token. */
export async function bootstrapTenant(
  baseURL: string,
  token: string,
  fields: TenantFields
): Promise<"created" | "exists" | "error"> {
  try {
    const { status } = await postJSON(
      `${baseURL}/api/v1/tenants`,
      {
        tenant_name: fields.tenantName,
        admin_email: fields.adminEmail,
        admin_display_name: fields.adminName,
        admin_password: fields.adminPassword,
      },
      { "X-Setup-Token": token }
    );
    if (status === 201) return "created";
    if (status === 409) return "exists";
    return "error";
  } catch {
    return "error";
  }
}

export type ActivationResult =
  | { kind: "silent-activated" }
  | { kind: "browser"; authorizationUrl: string };

/**
 * Whether this deployment already has its tenant — the first-run read.
 * Prefers the product's ``GET /api/v1/bootstrap-state`` (setup-token guarded);
 * falls back to the legacy empty-POST probe on older backends, and treats an
 * unreachable backend as not-yet-bootstrapped (the wizard will 502 visibly).
 */
export async function probeTenantExists(baseURL: string, token: string): Promise<boolean> {
  try {
    const res = await fetch(`${baseURL}/api/v1/bootstrap-state`, {
      headers: { "X-Setup-Token": token },
    });
    if (res.status === 200) {
      const body = (await res.json()) as { tenant_exists?: unknown };
      return body.tenant_exists === true;
    }
  } catch {
    // unreachable — the legacy probe below answers the same question
  }
  const legacy = await bootstrapTenant(baseURL, token, {
    tenantName: "",
    adminEmail: "",
    adminName: "",
    adminPassword: "",
  });
  return legacy === "exists";
}

/**
 * D11 + the ADR-0016 2026-10-11 amendment: with a registration code the control
 * plane returns the activation token directly (silent); without one the caller
 * gets the browser authorization URL. Admin credentials are used in memory only.
 */
export async function startActivation(
  baseURL: string,
  adminEmail: string,
  adminPassword: string,
  registrationCode: string
): Promise<ActivationResult> {
  const login = await postJSON(`${baseURL}/api/v1/auth/login`, {
    email: adminEmail,
    password: adminPassword,
  });
  if (login.status !== 200) {
    throw new Error("activation_login_failed");
  }
  const token = (login.json as { access_token?: string }).access_token ?? "";
  const body: Record<string, unknown> = {};
  if (registrationCode) {
    body.registration_code = registrationCode;
  }
  const res = await postJSON(`${baseURL}/api/v1/entitlement/activation`, body, {
    authorization: `Bearer ${token}`,
  });
  if (res.json.activated) {
    return { kind: "silent-activated" };
  }
  const url = res.json.authorization_url;
  if (typeof url === "string" && url) {
    return { kind: "browser", authorizationUrl: url };
  }
  throw new Error("activation_unexpected_response");
}
