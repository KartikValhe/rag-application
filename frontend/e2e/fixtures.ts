import { test as base, expect, APIRequestContext } from "@playwright/test";
import { ApiAuth, apiLogin } from "./helpers";

/**
 * Fresh, tagged data for the timetable scenarios, created through the real REST API as admin:
 * two classes, a subject linked to both, teacher@acme.edu assigned to both and
 * student@acme.edu enrolled in both. The tests never depend on what earlier runs or the dev seed
 * left behind. Everything is deleted afterwards, also when the test failed (best effort), and
 * leftovers named E2E-* from an aborted run are removed before the next run starts.
 */
export interface World {
  tag: number;
  subject: { id: string; name: string; code: string };
  classA: { id: string; name: string };
  classB: { id: string; name: string };
  admin: ApiAuth;
  teacher: ApiAuth;
  student: ApiAuth;
  /** POST as the given user and assert success. */
  post: (auth: ApiAuth, url: string, data?: unknown) => Promise<any>; // eslint-disable-line @typescript-eslint/no-explicit-any
}

const PREFIX = "E2E-";

async function send(
  request: APIRequestContext,
  method: "get" | "post" | "delete",
  auth: ApiAuth,
  url: string,
  data?: unknown
) {
  return request[method](`/api/v1${url}`, { headers: auth.headers, data });
}

async function removeLeftovers(request: APIRequestContext, admin: ApiAuth) {
  for (const kind of ["classes", "subjects"] as const) {
    const res = await send(request, "get", admin, `/${kind}?limit=100`);
    if (!res.ok()) continue;
    const body = (await res.json()) as { items: { id: string; name: string }[] };
    for (const item of body.items.filter((i) => i.name.startsWith(PREFIX))) {
      await send(request, "delete", admin, `/${kind}/${item.id}`).catch(() => undefined);
    }
  }
}

export const test = base.extend<{ world: World }>({
  world: async ({ request }, use) => {
    const admin = await apiLogin(request, "admin");
    const teacher = await apiLogin(request, "teacher");
    const student = await apiLogin(request, "student");
    await removeLeftovers(request, admin);

    const tag = Date.now();
    const post = async (auth: ApiAuth, url: string, data: unknown = {}) => {
      const res = await send(request, "post", auth, url, data);
      expect(res.ok(), `POST ${url} -> ${res.status()} ${await res.text()}`).toBeTruthy();
      return res.json();
    };

    const subject = await post(admin, "/subjects", { name: `${PREFIX}Subject-${tag}`, code: `E2E${tag}` });
    const classA = await post(admin, "/classes", { name: `${PREFIX}A-${tag}`, kind: "section" });
    const classB = await post(admin, "/classes", { name: `${PREFIX}B-${tag}`, kind: "section" });
    for (const c of [classA, classB]) {
      await post(admin, `/classes/${c.id}/subjects/${subject.id}`);
      await post(admin, `/classes/${c.id}/subjects/${subject.id}/teachers`, { user_id: teacher.userId });
      await post(admin, `/classes/${c.id}/students`, { user_id: student.userId });
    }

    try {
      await use({ tag, subject, classA, classB, admin, teacher, student, post });
    } finally {
      // Fresh login: the original token may have expired during a long test. Never throws.
      try {
        const cleaner = await apiLogin(request, "admin");
        for (const c of [classA, classB]) {
          await send(request, "delete", cleaner, `/classes/${c.id}`);
        }
        await send(request, "delete", cleaner, `/subjects/${subject.id}`);
      } catch (err) {
        console.warn("e2e cleanup failed (best effort):", err);
      }
    }
  },
});

export { expect };
