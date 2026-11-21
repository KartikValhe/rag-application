import test from "node:test";
import assert from "node:assert/strict";
import {
  validateDocumentFile,
  sanitizeDisplayFilename,
  sanitizeDisplayText,
  ALLOWED_EXTENSIONS,
  DEFAULT_MAX_FILE_BYTES,
} from "../utils/fileChecks.ts";

test("ALLOWED_EXTENSIONS includes pdf, docx, pptx, txt, md", () => {
  assert.ok(ALLOWED_EXTENSIONS.includes(".pdf"));
  assert.ok(ALLOWED_EXTENSIONS.includes(".docx"));
  assert.ok(ALLOWED_EXTENSIONS.includes(".pptx"));
  assert.ok(ALLOWED_EXTENSIONS.includes(".txt"));
  assert.ok(ALLOWED_EXTENSIONS.includes(".md"));
});

test("validateDocumentFile accepts valid files (case-insensitive)", () => {
  assert.equal(validateDocumentFile({ name: "notes.pdf", size: 1024 }), null);
  assert.equal(validateDocumentFile({ name: "lecture.PDF", size: 1024 }), null);
  assert.equal(validateDocumentFile({ name: "doc.Docx", size: 1024 }), null);
  assert.equal(validateDocumentFile({ name: "slides.PPTX", size: 1024 }), null);
  assert.equal(validateDocumentFile({ name: "readme.TXT", size: 1024 }), null);
  assert.equal(validateDocumentFile({ name: "guide.MD", size: 1024 }), null);
});

test("validateDocumentFile refuses empty files", () => {
  const err = validateDocumentFile({ name: "notes.pdf", size: 0 });
  assert.equal(err, "The file is empty.");
});

test("validateDocumentFile refuses spreadsheets and CSV with the plan's message", () => {
  const expected = "Spreadsheets are not accepted here; export it as PDF or DOCX";
  assert.equal(validateDocumentFile({ name: "table.xlsx", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "sheet.XLS", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "data.csv", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "data.CSV", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "data.ods", size: 1024 }), expected);
});

test("validateDocumentFile refuses disallowed file formats", () => {
  const expected =
    "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets and CSV are not accepted.";
  assert.equal(validateDocumentFile({ name: "script.exe", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "image.png", size: 1024 }), expected);
  assert.equal(validateDocumentFile({ name: "archive.zip", size: 1024 }), expected);
});

test("validateDocumentFile 50 MB boundary test (exactly 50 MB passes)", () => {
  assert.equal(DEFAULT_MAX_FILE_BYTES, 50 * 1024 * 1024);
  const fiftyMB = DEFAULT_MAX_FILE_BYTES;
  assert.equal(validateDocumentFile({ name: "huge.pdf", size: fiftyMB }), null);

  const fiftyMBPlusOne = fiftyMB + 1;
  assert.equal(
    validateDocumentFile({ name: "huge.pdf", size: fiftyMBPlusOne }),
    "This file is larger than your organisation allows. Make it smaller or split it."
  );
});

test("validateDocumentFile respects custom tenant maxBytes limit", () => {
  const tenMB = 10 * 1024 * 1024;
  assert.equal(validateDocumentFile({ name: "doc.pdf", size: tenMB }, tenMB), null);
  assert.equal(
    validateDocumentFile({ name: "doc.pdf", size: tenMB + 1 }, tenMB),
    "This file is larger than your organisation allows. Make it smaller or split it."
  );
});

test("sanitizeDisplayFilename removes paths, control characters, and bidi marks", () => {
  // Strip windows and unix paths
  assert.equal(sanitizeDisplayFilename("C:\\Users\\Teacher\\notes.pdf"), "notes.pdf");
  assert.equal(sanitizeDisplayFilename("/var/data/uploads/syllabus.docx"), "syllabus.docx");

  // Control characters
  assert.equal(sanitizeDisplayFilename("bad\u0000\u0007\u001fname.pdf"), "badname.pdf");

  // Bidi override characters (e.g., \u202e RLO)
  assert.equal(sanitizeDisplayFilename("safe\u202eexe.pdf"), "safeexe.pdf");

  // Truncation preserving extension
  const longName = "a".repeat(100) + ".pdf";
  const sanitized = sanitizeDisplayFilename(longName, 20);
  assert.ok(sanitized.endsWith(".pdf"));
  assert.ok(sanitized.includes("…"));
  assert.ok(sanitized.length <= 20);
});

test("sanitizeDisplayText: removes bidi and control characters but keeps slashes in a title", () => {
  assert.equal(sanitizeDisplayText("Notes\u202E gpj.exe\u0000"), "Notes gpj.exe");
  assert.equal(sanitizeDisplayText("Maths / Algebra \u2066x\u2069"), "Maths / Algebra x");
  assert.equal(sanitizeDisplayText("\u202E\u200F"), "");
});
