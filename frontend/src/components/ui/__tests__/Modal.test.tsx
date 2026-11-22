import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import fs from "node:fs";
import path from "node:path";
import { Modal } from "../Modal";
import { ConfirmDialog } from "../ConfirmDialog";

// Phase 5f-10a: every dialog is named by its visible title (aria-labelledby), with one unique id per instance.
describe("Modal accessible name", () => {
  it("the dialog is named by its visible title", () => {
    render(
      <Modal isOpen onClose={() => undefined} title="Invite New User">
        <p>body</p>
      </Modal>
    );
    const dialog = screen.getByRole("dialog", { name: "Invite New User" });
    const labelId = dialog.getAttribute("aria-labelledby");
    expect(labelId).toBeTruthy();
    const heading = document.getElementById(labelId as string);
    expect(heading?.tagName).toBe("H2");
    expect(heading?.textContent).toBe("Invite New User");
  });

  it("two open modals get two different label ids and two different names", () => {
    render(
      <>
        <Modal isOpen onClose={() => undefined} title="First dialog">
          <p>a</p>
        </Modal>
        <Modal isOpen onClose={() => undefined} title="Second dialog">
          <p>b</p>
        </Modal>
      </>
    );
    const a = screen.getByRole("dialog", { name: "First dialog" });
    const b = screen.getByRole("dialog", { name: "Second dialog" });
    expect(a.getAttribute("aria-labelledby")).not.toBe(b.getAttribute("aria-labelledby"));
  });

  it("a title that is not plain text still names the dialog by what is visible", () => {
    render(
      <Modal isOpen onClose={() => undefined} title={<span>Edit Class: 10A</span>}>
        <p>body</p>
      </Modal>
    );
    expect(screen.getByRole("dialog", { name: "Edit Class: 10A" })).toBeDefined();
  });

  it("without a title there is no dangling aria-labelledby", () => {
    render(
      <Modal isOpen onClose={() => undefined}>
        <p>body</p>
      </Modal>
    );
    expect(screen.getByRole("dialog").hasAttribute("aria-labelledby")).toBe(false);
  });

  it("a closed modal renders nothing", () => {
    render(
      <Modal isOpen={false} onClose={() => undefined} title="Hidden">
        <p>body</p>
      </Modal>
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("ConfirmDialog accessible name", () => {
  it("is named by the title shown in the dialog body", () => {
    render(
      <ConfirmDialog
        isOpen
        title="Delete 'Notes'?"
        message="This cannot be undone."
        onConfirm={() => undefined}
        onClose={() => undefined}
      />
    );
    const dialog = screen.getByRole("dialog", { name: "Delete 'Notes'?" });
    // the title is shown once, not twice (no second heading was added to get the name)
    expect(screen.getAllByText("Delete 'Notes'?")).toHaveLength(1);
    expect(dialog.getAttribute("aria-labelledby")).toBeTruthy();
  });

  it("two confirm dialogs at once keep their own names", () => {
    render(
      <>
        <ConfirmDialog isOpen title="Delete one?" message="m" onConfirm={() => undefined} onClose={() => undefined} />
        <ConfirmDialog isOpen title="Delete two?" message="m" onConfirm={() => undefined} onClose={() => undefined} />
      </>
    );
    expect(screen.getByRole("dialog", { name: "Delete one?" })).toBeDefined();
    expect(screen.getByRole("dialog", { name: "Delete two?" })).toBeDefined();
  });
});

// Check for the other dialogs that use the shared Modal: each usage must pass a title (or label it itself),
// otherwise its dialog would have no name. A new Modal without a title fails here.
describe("every <Modal> in the app is given a title", () => {
  const srcDir = path.resolve(__dirname, "../../..");

  function walk(dir: string, out: string[] = []): string[] {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name === "__tests__") continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full, out);
      else if (entry.name.endsWith(".tsx")) out.push(full);
    }
    return out;
  }

  it("each <Modal ...> opening tag has a title= or labelledBy= prop", () => {
    const missing: string[] = [];
    let count = 0;
    for (const file of walk(srcDir)) {
      if (file.endsWith(path.join("ui", "Modal.tsx"))) continue;
      const text = fs.readFileSync(file, "utf-8");
      const re = /<Modal\b([\s\S]*?)>\s*\n/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(text)) !== null) {
        count += 1;
        // the opening tag ends at the first line that closes it; use a generous window of the tag text
        const start = m.index;
        const window = text.slice(start, start + 600);
        if (!/\b(title|labelledBy)=/.test(window)) missing.push(path.relative(srcDir, file));
      }
    }
    expect(count).toBeGreaterThanOrEqual(19);
    expect(missing).toEqual([]);
  });
});
