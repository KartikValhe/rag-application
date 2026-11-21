import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileUpload } from "../FileUpload";

describe("FileUpload component", () => {
  it("renders no <p> tags inside the button", () => {
    render(
      <FileUpload
        onFileSelect={vi.fn()}
        selectedFile={null}
        helperText="Accepted formats: PDF, Word. Max 50 MB."
      />
    );

    const button = screen.getByRole("button");
    const pInsideButton = button.querySelector("p");
    expect(pInsideButton).toBeNull();
    const spans = button.querySelectorAll("span");
    expect(spans.length).toBeGreaterThan(0);
  });

  it("Enter or Space on the button triggers file picker click", async () => {
    const user = userEvent.setup();
    render(<FileUpload onFileSelect={vi.fn()} selectedFile={null} />);

    const button = screen.getByRole("button");
    const fileInput = document.querySelector("input[type='file']") as HTMLInputElement;
    const clickSpy = vi.spyOn(fileInput, "click");

    await user.click(button);
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockClear();

    // Focus button and press Enter
    button.focus();
    fireEvent.keyDown(button, { key: "Enter", code: "Enter" });
    expect(clickSpy).toHaveBeenCalled();
    clickSpy.mockClear();

    // Focus button and press Space
    button.focus();
    fireEvent.keyDown(button, { key: " ", code: "Space" });
    expect(clickSpy).toHaveBeenCalled();
  });

  it("remove button has aria-label and triggers onFileSelect(null)", async () => {
    const user = userEvent.setup();
    const onFileSelectMock = vi.fn();
    const file = new File(["dummy content"], "lecture_notes.pdf", {
      type: "application/pdf",
    });

    render(<FileUpload onFileSelect={onFileSelectMock} selectedFile={file} />);

    const removeBtn = screen.getByRole("button", { name: "Remove file" });
    expect(removeBtn).toBeDefined();
    expect(removeBtn.getAttribute("aria-label")).toBe("Remove file");

    await user.click(removeBtn);
    expect(onFileSelectMock).toHaveBeenCalledWith(null);
  });

  it("aria-describedby links helperText and error to the input and button", () => {
    render(
      <FileUpload
        onFileSelect={vi.fn()}
        selectedFile={null}
        helperText="Max 50 MB"
        error="This file is too large"
      />
    );

    const button = screen.getByRole("button");
    const describedBy = button.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();

    const errorAlert = screen.getByRole("alert");
    expect(errorAlert.textContent).toBe("This file is too large");
    expect(describedBy).toContain(errorAlert.id);
  });

  it("displays the selected file name through sanitizeDisplayFilename", () => {
    // Filename containing control characters and bidi overrides
    const unsafeName = "notes\u0000\u001f\u202eunsafe.pdf";
    const file = new File(["content"], unsafeName, { type: "application/pdf" });

    render(<FileUpload onFileSelect={vi.fn()} selectedFile={file} />);

    expect(screen.getByText("notesunsafe.pdf")).toBeDefined();
    expect(screen.queryByText(unsafeName)).toBeNull();
  });
});
