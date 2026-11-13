import { APIRequestContext, expect, Page } from "@playwright/test";

export const ACCOUNTS = {
  admin: { email: "admin@acme.edu", password: "Admin123!" },
  teacher: { email: "teacher@acme.edu", password: "Teacher123!" },
  student: { email: "student@acme.edu", password: "Student123!" },
} as const;

export type Role = keyof typeof ACCOUNTS;

/** Collects uncaught page errors and console errors for one page. */
export function watchErrors(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (err) => pageErrors.push(err.message));
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (text.includes("favicon.ico") || text.includes("WebSocket connection")) return;
    // The app probes /auth/refresh on boot. Before login it returns 401 by design,
    // and the browser logs that as a console error.
    if (text.includes("401") && msg.location().url.includes("/auth/refresh")) return;
    consoleErrors.push(text);
  });
  return { pageErrors, consoleErrors };
}

export async function login(page: Page, role: Role, tenant = "acme") {
  await page.goto(`/login?tenant=${tenant}`);
  await page.getByLabel("Email Address").fill(ACCOUNTS[role].email);
  await page.getByLabel("Password", { exact: true }).fill(ACCOUNTS[role].password);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"));
  await expect(page.locator('aside[aria-label="Desktop Navigation"]')).toBeVisible();
}

export interface ApiAuth {
  userId: string;
  headers: Record<string, string>;
}

/** Logs in through the API (for test setup) and returns auth headers. */
export async function apiLogin(request: APIRequestContext, role: Role, tenant = "acme"): Promise<ApiAuth> {
  const res = await request.post("/api/v1/auth/login", {
    headers: { "X-Tenant-Slug": tenant },
    data: { ...ACCOUNTS[role], tenant_slug: tenant },
  });
  expect(res.ok(), `login ${role} -> ${res.status()}`).toBeTruthy();
  const body = await res.json();
  return {
    userId: body.user_id,
    headers: { Authorization: `Bearer ${body.access_token}`, "X-Tenant-Slug": tenant },
  };
}

/** Opens a page through the sidebar link, so the in-memory session is kept. */
export async function openViaSidebar(page: Page, href: string) {
  await page.locator(`aside[aria-label="Desktop Navigation"] a[href="${href}"]`).click();
  await expect(page).toHaveURL(new RegExp(`${href}$`));
}

export const bodyOk = async (page: Page) => {
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
  await expect(page.getByText("An unexpected error occurred")).toHaveCount(0);
};
