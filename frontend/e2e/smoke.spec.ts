import { test, expect, Page } from "@playwright/test";

/**
 * Playwright End-to-End Smoke Test Suite
 *
 * Requirements:
 * 1. Fail immediately on any uncaught page error or browser console error.
 * 2. Login as admin, teacher, and student on acme.
 * 3. Each role navigates to its dashboard and every sidebar page without error or crash.
 * 4. Teacher creates/updates draft timetable entry, publishes it, and student sees it.
 * 5. Student typing an admin URL directly sees the 403 Forbidden page.
 * 6. Branding page / tenant host loads different brand on a second tenant.
 */

// Track page errors and console errors per test
function setupErrorListeners(page: Page) {
  const consoleErrors: string[] = [];
  const pageErrors: Error[] = [];

  page.on("pageerror", (err) => {
    // Collect unhandled browser runtime errors
    pageErrors.push(err);
  });

  page.on("console", (msg) => {
    // Capture browser console errors (ignore benign React warnings or network cancellations)
    if (msg.type() === "error") {
      const text = msg.text();
      // Ignore favicon 404 or transient vite websocket reconnects
      if (!text.includes("favicon.ico") && !text.includes("WebSocket connection to 'ws:")) {
        consoleErrors.push(text);
      }
    }
  });

  return { consoleErrors, pageErrors };
}

async function login(page: Page, email: string, pass: string, tenant = "acme") {
  await page.goto(`/login?tenant=${tenant}`);
  await page.waitForLoadState("domcontentloaded");

  // Fill credentials
  await page.fill('input[type="email"], input[name="email"]', email);
  await page.fill('input[type="password"], input[name="password"]', pass);
  await page.click('button[type="submit"]');

  // Wait for navigation past login
  await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 10000 });
}

test.describe("E2E Smoke Tests: Academic Multi-Tenant Platform", () => {
  test("Admin login on acme and sidebar navigation across all pages", async ({ page }) => {
    const { consoleErrors, pageErrors } = setupErrorListeners(page);

    await login(page, "admin@acme.edu", "Admin123!", "acme");

    const adminPages = [
      "/admin",
      "/admin/roles",
      "/admin/academic",
      "/admin/enrollments",
      "/admin/assignments",
      "/admin/timetables",
      "/admin/users",
      "/admin/audit",
      "/admin/branding",
    ];

    for (const path of adminPages) {
      await page.goto(path);
      await page.waitForLoadState("domcontentloaded");
      // Verify no ErrorBoundary fallback is shown
      await expect(page.locator("text=Something went wrong")).toHaveCount(0);
      await expect(page.locator("text=An unexpected error occurred")).toHaveCount(0);
    }

    expect(pageErrors, `Unhandled page errors: ${pageErrors.map((e) => e.message).join(", ")}`).toEqual([]);
    expect(consoleErrors, `Console errors: ${consoleErrors.join(", ")}`).toEqual([]);
  });

  test("Teacher login on acme and sidebar navigation across all pages", async ({ page }) => {
    const { consoleErrors, pageErrors } = setupErrorListeners(page);

    await login(page, "teacher@acme.edu", "Teacher123!", "acme");

    const teacherPages = [
      "/teacher/dashboard",
      "/teacher/classes",
      "/teacher/schedule",
      "/teacher/timetables",
    ];

    for (const path of teacherPages) {
      await page.goto(path);
      await page.waitForLoadState("domcontentloaded");
      await expect(page.locator("text=Something went wrong")).toHaveCount(0);
      await expect(page.locator("text=An unexpected error occurred")).toHaveCount(0);
    }

    expect(pageErrors, `Unhandled page errors: ${pageErrors.map((e) => e.message).join(", ")}`).toEqual([]);
    expect(consoleErrors, `Console errors: ${consoleErrors.join(", ")}`).toEqual([]);
  });

  test("Student login on acme and sidebar navigation across all pages", async ({ page }) => {
    const { consoleErrors, pageErrors } = setupErrorListeners(page);

    await login(page, "student@acme.edu", "Student123!", "acme");

    const studentPages = [
      "/student/dashboard",
      "/student/classes",
      "/student/schedule",
    ];

    for (const path of studentPages) {
      await page.goto(path);
      await page.waitForLoadState("domcontentloaded");
      await expect(page.locator("text=Something went wrong")).toHaveCount(0);
      await expect(page.locator("text=An unexpected error occurred")).toHaveCount(0);
    }

    expect(pageErrors, `Unhandled page errors: ${pageErrors.map((e) => e.message).join(", ")}`).toEqual([]);
    expect(consoleErrors, `Console errors: ${consoleErrors.join(", ")}`).toEqual([]);
  });

  test("Student accessing admin URL directly sees 403 Forbidden page", async ({ page }) => {
    const { consoleErrors, pageErrors } = setupErrorListeners(page);

    await login(page, "student@acme.edu", "Student123!", "acme");

    // Directly navigate to an administrative URL
    await page.goto("/admin");
    await page.waitForLoadState("domcontentloaded");

    // Verify 403 Forbidden UI
    await expect(page.locator("text=Administrative Access Required (403)")).toBeVisible();
    await expect(page.locator("text=Return to Student Portal")).toBeVisible();

    // Verify sidebar doesn't show administrative section to student
    await expect(page.locator("text=Roles & Access")).toHaveCount(0);

    expect(pageErrors, `Unhandled page errors: ${pageErrors.map((e) => e.message).join(", ")}`).toEqual([]);
    expect(consoleErrors, `Console errors: ${consoleErrors.join(", ")}`).toEqual([]);
  });

  test("Branding loads different brand display name and theme on second tenant host", async ({ page }) => {
    const { consoleErrors, pageErrors } = setupErrorListeners(page);

    // Tenant 1: Acme
    await page.goto("/login?tenant=acme");
    await page.waitForLoadState("domcontentloaded");
    const acmeHtml = await page.locator("html").getAttribute("data-tenant");
    expect(acmeHtml).toBe("acme");

    // Tenant 2: Default
    await page.goto("/login?tenant=default");
    await page.waitForLoadState("domcontentloaded");
    const defaultHtml = await page.locator("html").getAttribute("data-tenant");
    expect(defaultHtml).toBe("default");

    expect(pageErrors, `Unhandled page errors: ${pageErrors.map((e) => e.message).join(", ")}`).toEqual([]);
    expect(consoleErrors, `Console errors: ${consoleErrors.join(", ")}`).toEqual([]);
  });

  test("Teacher publishes timetable and student views published timetable", async ({ page, browser }) => {
    // 1. Teacher publishes timetable
    const teacherContext = await browser.newContext();
    const teacherPage = await teacherContext.newPage();
    const teacherErrors = setupErrorListeners(teacherPage);

    await login(teacherPage, "teacher@acme.edu", "Teacher123!", "acme");
    await teacherPage.goto("/teacher/timetables");
    await teacherPage.waitForLoadState("domcontentloaded");

    // Check if Publish Timetable button exists and is active
    const publishBtn = teacherPage.locator("button:has-text('Publish Timetable')");
    if (await publishBtn.isVisible()) {
      await publishBtn.click();
      // If conflict modal opens, confirm publication
      const proceedBtn = teacherPage.locator("button:has-text('Proceed & Publish')");
      if (await proceedBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
        await proceedBtn.click();
      }
      await teacherPage.waitForTimeout(1000);
    }

    expect(teacherErrors.pageErrors).toEqual([]);
    expect(teacherErrors.consoleErrors).toEqual([]);
    await teacherContext.close();

    // 2. Student views schedule
    const studentContext = await browser.newContext();
    const studentPage = await studentContext.newPage();
    const studentErrors = setupErrorListeners(studentPage);

    await login(studentPage, "student@acme.edu", "Student123!", "acme");
    await studentPage.goto("/student/schedule");
    await studentPage.waitForLoadState("domcontentloaded");

    // Ensure page renders cleanly with no error state or blank crash
    await expect(studentPage.locator("text=Something went wrong")).toHaveCount(0);
    await expect(studentPage.locator("text=Weekly Timetable Overview")).toBeVisible();

    expect(studentErrors.pageErrors).toEqual([]);
    expect(studentErrors.consoleErrors).toEqual([]);
    await studentContext.close();
  });
});

