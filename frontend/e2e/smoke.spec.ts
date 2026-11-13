import { test, expect, Page } from "@playwright/test";
import { bodyOk, login, openViaSidebar, Role, watchErrors } from "./helpers";

const SIDEBAR = 'aside[aria-label="Desktop Navigation"]';

// Every sidebar page is opened by clicking its link. The list comes from the DOM,
// so a new sidebar item is covered automatically.
for (const role of ["admin", "teacher", "student"] as Role[]) {
  test(`${role} opens every sidebar page without errors`, async ({ page }) => {
    const errors = watchErrors(page);
    await login(page, role);

    const hrefs = await page
      .locator(`${SIDEBAR} nav a`)
      .evaluateAll((els) => els.map((e) => e.getAttribute("href") as string));
    expect(hrefs.length).toBeGreaterThan(2);

    for (const href of hrefs) {
      await openViaSidebar(page, href);
      await page.waitForLoadState("networkidle");
      await bodyOk(page);
      await expect(page.locator("h1").first()).toBeVisible();
    }

    expect(errors.pageErrors).toEqual([]);
    expect(errors.consoleErrors).toEqual([]);
  });
}

test("student typing an admin URL sees the 403 page", async ({ page }) => {
  const errors = watchErrors(page);
  await login(page, "student");
  // A full page load keeps the session through the refresh cookie.
  await page.goto("/admin");
  await expect(page.getByText("Administrative Access Required (403)")).toBeVisible();
  await expect(page.getByText("Roles & Access")).toHaveCount(0);
  expect(errors.pageErrors).toEqual([]);
});

test("teacher drafts and publishes an entry, student sees it and the Next lecture card", async ({
  browser,
}) => {
  const room = `E2E-${Date.now()}`;
  // Random slot so repeated runs do not pile onto the same cell.
  const day = String(Math.floor(Math.random() * 5));
  const hour = 6 + Math.floor(Math.random() * 16);
  const start = `${String(hour).padStart(2, "0")}:00`;
  const end = `${String(hour).padStart(2, "0")}:45`;

  const tctx = await browser.newContext();
  const t = await tctx.newPage();
  const tErrors = watchErrors(t);
  await login(t, "teacher");
  await openViaSidebar(t, "/teacher/timetables");

  await t.getByRole("combobox").first().selectOption({ label: "Class 10-A (section)" });
  await t.getByRole("button", { name: "Add Lecture" }).click();
  const dialog = t.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Subject *").selectOption({ index: 0 });
  await dialog.getByLabel("Day of Week *").selectOption(day);
  await dialog.getByLabel("Start Time *").fill(start);
  await dialog.getByLabel("End Time *").fill(end);
  await dialog.getByLabel(/^Room/).fill(room);
  await dialog.getByRole("button", { name: "Add to Timetable" }).click();
  await expect(dialog).toBeHidden();
  await expect(t.getByText(room).first()).toBeVisible();
  await expect(t.getByText("Draft In Progress")).toBeVisible();

  // The draft stays invisible to the student.
  const sctx = await browser.newContext();
  const s = await sctx.newPage();
  const sErrors = watchErrors(s);
  await login(s, "student");
  await openViaSidebar(s, "/student/schedule");
  await s.waitForLoadState("networkidle");
  await expect(s.getByText(room)).toHaveCount(0);

  // Publish, confirming the conflict dialog if one appears.
  await t.getByRole("button", { name: "Publish Timetable" }).click();
  const override = t.getByRole("button", { name: "Override & Publish Anyway" });
  const published = t.getByText(/Published \(v\d+\)/);
  await expect(override.or(published)).toBeVisible();
  if (await override.isVisible()) await override.click();
  await expect(published).toBeVisible();

  // The student sees the entry and the Next lecture card.
  await openViaSidebar(s, "/student/dashboard");
  await expect(s.getByText("Next Lecture")).toBeVisible();
  await expect(s.getByText(/Next Up|Happening Now/).first()).toBeVisible();
  await openViaSidebar(s, "/student/schedule");
  await expect(s.getByText(room).first()).toBeVisible();

  expect(tErrors.pageErrors).toEqual([]);
  expect(tErrors.consoleErrors).toEqual([]);
  expect(sErrors.pageErrors).toEqual([]);
  expect(sErrors.consoleErrors).toEqual([]);
  await tctx.close();
  await sctx.close();
});

test.describe("import wizard", () => {
  async function openWizard(page: Page) {
    await login(page, "admin");
    await openViaSidebar(page, "/admin/timetables");
    // Class 10-B is not used by the publish test above.
    await page.getByRole("combobox").first().selectOption({ label: "Class 10-B (section)" });
    await page.getByRole("button", { name: "Import" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "Next: upload" }).click();
  }

  test("accepts a small valid CSV and saves it as a draft only", async ({ page }) => {
    const errors = watchErrors(page);
    await openWizard(page);
    const csv =
      "weekday,start_time,end_time,subject_code,teacher_email,room,note\n" +
      "Monday,09:00,09:45,MATH101,teacher@acme.edu,E2E-CSV,imported\n";
    await page.locator('input[type="file"]').setInputFiles({
      name: "timetable.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(csv),
    });
    await expect(page.getByText("Total: 1")).toBeVisible();
    await expect(page.getByText("Valid: 1")).toBeVisible();
    await page.getByRole("button", { name: "Next: save" }).click();
    await expect(page.getByText(/Nothing is published until you publish it/)).toBeVisible();
    await page.getByRole("button", { name: "Save as draft" }).click();
    // An existing draft asks for confirmation to replace it.
    const wizard = page.getByRole("dialog").filter({ hasText: "Import timetable" });
    const replace = page.getByRole("button", { name: "Replace draft" });
    await Promise.race([wizard.waitFor({ state: "hidden" }), replace.waitFor()]);
    if (await replace.isVisible()) await replace.click();
    await expect(wizard).toBeHidden();
    await expect(page.getByText("Draft In Progress")).toBeVisible();
    expect(errors.pageErrors).toEqual([]);
  });

  test("rejects a hostile file with a friendly message", async ({ page }) => {
    const errors = watchErrors(page);
    await openWizard(page);
    // A Windows executable renamed to .xlsx.
    await page.locator('input[type="file"]').setInputFiles({
      name: "timetable.xlsx",
      mimeType: "application/octet-stream",
      buffer: Buffer.concat([Buffer.from("MZ"), Buffer.alloc(2048, 0x90)]),
    });
    const alert = page.getByRole("alert").first();
    await expect(alert).toBeVisible();
    const text = (await alert.textContent()) ?? "";
    expect(text).toMatch(/not valid|damaged|does not match|not allowed|could not read/i);
    expect(text).not.toMatch(/traceback|exception|stack|errno|\.py/i);
    // Still on the upload step, nothing was parsed.
    await expect(page.getByRole("button", { name: "Choose file" })).toBeVisible();
    expect(errors.pageErrors).toEqual([]);
  });
});

test("backend unreachable shows a friendly error, not a blank screen", async ({ page }) => {
  const errors = watchErrors(page);
  await login(page, "student");
  await page.route("**/api/**", (route) => route.abort("connectionrefused"));
  await openViaSidebar(page, "/student/schedule");
  await expect(
    page.getByText(/Cannot reach the server|Failed to load|Something went wrong|try again/i).first()
  ).toBeVisible();
  // The shell is still rendered.
  await expect(page.locator(SIDEBAR)).toBeVisible();
  expect((await page.locator("body").innerText()).trim().length).toBeGreaterThan(50);
  expect(errors.pageErrors).toEqual([]);
});
