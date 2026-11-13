import { test, expect, Page } from "@playwright/test";
import { ApiAuth, apiLogin, bodyOk, login, openViaSidebar, Role, watchErrors } from "./helpers";

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

test("teacher publish: no conflicts publishes directly; a double-booking shows the dialog and confirm publishes", async ({
  page,
  request,
}) => {
  // Fresh classes and subject so the result does not depend on earlier runs.
  const tag = Date.now();
  const classA = `E2E-A-${tag}`;
  const classB = `E2E-B-${tag}`;
  const subject = `E2E-Subject-${tag}`;
  const admin = await apiLogin(request, "admin");
  const teacher = await apiLogin(request, "teacher");

  const post = async (auth: ApiAuth, url: string, data: unknown) => {
    const res = await request.post(`/api/v1${url}`, { headers: auth.headers, data });
    expect(res.ok(), `${url} -> ${res.status()}`).toBeTruthy();
    return res.json();
  };
  const sub = await post(admin, "/subjects", { name: subject });
  const a = await post(admin, "/classes", { name: classA, kind: "section" });
  const b = await post(admin, "/classes", { name: classB, kind: "section" });
  for (const c of [a, b]) {
    await post(admin, `/classes/${c.id}/subjects/${sub.id}`, {});
    await post(admin, `/classes/${c.id}/subjects/${sub.id}/teachers`, { user_id: teacher.userId });
  }

  const errors = watchErrors(page);
  const checks: { conflicts: unknown[] }[] = [];
  const publishBodies: { confirm_conflicts: boolean }[] = [];
  page.on("response", async (res) => {
    if (res.url().includes(`/classes/${a.id}/timetables/check-conflicts`)) checks.push(await res.json());
  });
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes(`/classes/${a.id}/timetables/publish`)) {
      publishBodies.push(req.postDataJSON());
    }
  });

  await login(page, "teacher");
  await openViaSidebar(page, "/teacher/timetables");
  await page.getByRole("combobox").first().selectOption({ label: `${classA} (section)` });

  const addLecture = async (day: string, start: string, end: string, room: string) => {
    await page.getByRole("button", { name: "Add Lecture" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Subject *").selectOption({ label: subject });
    await dialog.getByLabel("Assigned Teacher").selectOption({ index: 1 });
    await dialog.getByLabel("Day of Week *").selectOption(day);
    await dialog.getByLabel("Start Time *").fill(start);
    await dialog.getByLabel("End Time *").fill(end);
    await dialog.getByLabel(/^Room/).fill(room);
    await dialog.getByRole("button", { name: "Add to Timetable" }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(room).first()).toBeVisible();
  };

  // 1. No conflicts: publishes straight away, no dialog.
  await addLecture("0", "09:00", "10:00", `R-A-${tag}`);
  await page.getByRole("button", { name: "Publish Timetable" }).click();
  await expect(page.getByText(/Published \(v\d+\)/)).toBeVisible();
  await expect(page.getByText("Schedule Conflicts Detected")).toHaveCount(0);
  expect(checks.at(-1)?.conflicts).toEqual([]);
  expect(publishBodies).toEqual([{ confirm_conflicts: false }]);

  // 2. Same teacher published in class B on Wednesday 11:30-12:30.
  await post(teacher, `/classes/${b.id}/timetables/draft/entries`, {
    day_of_week: 2,
    start_time: "11:30",
    end_time: "12:30",
    subject_id: sub.id,
    teacher_id: teacher.userId,
    room: `R-B-${tag}`,
  });
  await post(teacher, `/classes/${b.id}/timetables/publish`, { confirm_conflicts: false });

  // Class A draft gets Wednesday 11:00-12:00 with the same teacher.
  await page.getByRole("button", { name: "Draft Editor" }).click();
  await addLecture("2", "11:00", "12:00", `R-A2-${tag}`);
  await page.getByRole("button", { name: "Publish Timetable" }).click();

  const conflictDialog = page.getByRole("dialog").filter({ hasText: "Schedule Conflicts Detected" });
  await expect(conflictDialog).toBeVisible();
  await expect(conflictDialog).toContainText("1 schedule conflict was");
  await expect(conflictDialog).toContainText("Teacher Double-Booked");
  await expect(conflictDialog).toContainText(`already teaching ${subject} in ${classB} on Wednesday during 11:30 AM – 12:30 PM`);
  await expect(conflictDialog).toContainText(`${subject}, Wednesday 11:00 AM – 12:00 PM`);
  expect(checks.at(-1)?.conflicts).toHaveLength(1);
  expect(publishBodies).toHaveLength(1); // nothing sent until confirmed

  await conflictDialog.getByRole("button", { name: "Override & Publish Anyway" }).click();
  await expect(conflictDialog).toBeHidden();
  await expect(page.getByText(/Published \(v\d+\)/)).toBeVisible();
  expect(publishBodies.at(-1)).toEqual({ confirm_conflicts: true });
  await bodyOk(page);
  expect(errors.pageErrors).toEqual([]);
  expect(errors.consoleErrors).toEqual([]);
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
