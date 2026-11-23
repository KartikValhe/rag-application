// Helper for the API drift guard (not a test itself). It reads docs/api/phase5.md as TEXT and the
// frontend's own sources/modules, and returns a list of plain sentences that say WHAT drifted.
// No network, no backend files: the checked-in doc is the only source of truth.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { MAPPED_ERROR_CODES, getVersionFailure } from "../utils/documentErrors.ts";
import { FLAG_CODE_DESCRIPTIONS } from "../utils/flagDescriptions.ts";
import {
  SEARCH_MAX_QUERY_CHARS,
  buildPublishBody,
  buildTestSearchBody,
  buildUploadFormData,
  buildNewVersionFormData,
  createDocumentsApi,
} from "../api/documentsApi.ts";
import * as schemas from "../schemas/documentSchemas.ts";

const here = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url));
export const DOCUMENTS_DIR = path.resolve(here, "..");
export const SRC_DIR = path.resolve(here, "../../..");
export const DEFAULT_DOC_PATH = path.resolve(here, "../../../../../docs/api/phase5.md");

/** Doc error codes the frontend deliberately leaves to the generic per-status text, each with the reason. */
export const INTENTIONALLY_GENERIC_ERROR_CODES: Record<string, string> = {
  subject_has_documents: "shown by the subjects screen (backlog B20), not by the materials screens",
  document_not_deleted: "the purge route has no screen yet",
  unknown_limit: "platform limits screen does not exist in the frontend",
  invalid_limit_value: "platform limits screen does not exist in the frontend",
  limit_above_ceiling: "platform limits screen does not exist in the frontend",
};

/** Documented routes the frontend does not call yet, each with the reason. Key: "METHOD /path-without-/api/v1". */
export const ROUTES_NOT_USED_BY_FRONTEND: Record<string, string> = {
  "PATCH /documents/{}": "rename and unlink are not built",
  "GET /documents/{}/versions/{}/download": "download original is step 5f-11",
  "POST /admin/documents/{}/purge": "purge control is deferred (no route lists deleted documents)",
  "PATCH /platform/tenants/{}/limits": "platform portal limits screen is not built",
};

/** Documented response models the frontend has a schema for. */
const SCHEMA_FOR_MODEL: Record<string, { shape: Record<string, unknown> }> = {
  DocumentVersionResponse: schemas.DocumentVersionSchema,
  DocumentResponse: schemas.DocumentSchema,
  DocumentListResponse: schemas.DocumentListSchema,
  DocumentStatusResponse: schemas.DocumentStatusSchema,
  DocumentUploadLimitsResponse: schemas.DocumentUploadLimitsSchema,
  DocumentUsageResponse: schemas.DocumentUsageSchema,
  DocumentVersionListResponse: schemas.DocumentVersionListSchema,
  DocumentLimits: schemas.DocumentLimitsSchema,
  TestSearchResponse: schemas.TestSearchResponseSchema,
  TestSearchHit: schemas.TestSearchHitSchema,
  VersionFlagsResponse: schemas.VersionFlagsSchema,
  FlaggedChunk: schemas.FlaggedChunkSchema,
};

// ---------- reading the doc ----------

export function readDoc(docPath: string = process.env.PHASE5_API_DOC ?? DEFAULT_DOC_PATH): string {
  if (!fs.existsSync(docPath)) {
    throw new Error(
      `DRIFT GUARD cannot run: the API doc was not found at ${docPath}. ` +
        "The repository checkout must include docs/api/phase5.md."
    );
  }
  return fs.readFileSync(docPath, "utf8");
}

/** Text from the heading that matches `heading` to the next heading of the same or a higher level. */
export function section(doc: string, heading: RegExp): string {
  const lines = doc.split(/\r?\n/);
  const start = lines.findIndex((l) => /^#{1,6} /.test(l) && heading.test(l));
  if (start < 0) throw new Error(`DRIFT GUARD: the doc has no section matching ${heading}. The doc layout changed.`);
  const level = /^(#+)/.exec(lines[start])![1].length;
  const out: string[] = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    const m = /^(#+) /.exec(lines[i]);
    if (m && m[1].length <= level) break;
    out.push(lines[i]);
  }
  return out.join("\n");
}

function splitCells(row: string): string[] {
  return row
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split(/(?<!\\)\|/)
    .map((c) => c.trim());
}

function tableRows(text: string): string[][] {
  return text
    .split(/\r?\n/)
    .filter((l) => l.trim().startsWith("|"))
    .map(splitCells)
    .filter((cells) => !cells.every((c) => /^:?-{2,}:?$/.test(c)));
}

function backticked(text: string): string[] {
  return [...text.matchAll(/`([^`]+)`/g)].map((m) => m[1]);
}

/** Tables of a section, one list of rows per table (a table ends at the first line that is not a row). */
function tableBlocks(text: string): string[][][] {
  const blocks: string[][][] = [];
  let cur: string[] = [];
  for (const line of [...text.split(/\r?\n/), ""]) {
    if (line.trim().startsWith("|")) cur.push(line);
    else if (cur.length) {
      blocks.push(tableRows(cur.join("\n")));
      cur = [];
    }
  }
  return blocks;
}

function codesOf(rows: string[][]): string[] {
  const out: string[] = [];
  for (const cells of rows) {
    for (const token of backticked(cells[0] ?? "")) if (/^[a-z][a-z0-9_]*$/.test(token)) out.push(token);
  }
  return out;
}

/** Codes in the first column of every row (rows whose first cell has no code are ignored). */
function firstColumnCodes(text: string): string[] {
  const out: string[] = [];
  for (const cells of tableRows(text)) {
    for (const token of backticked(cells[0] ?? "")) if (/^[a-z][a-z0-9_]*$/.test(token)) out.push(token);
  }
  return out;
}

export interface DocFacts {
  httpErrorCodes: string[];
  versionFailureCodes: string[];
  flagCodes: string[];
  permissions: string[];
  routes: Array<{ key: string; permission: string; requestCell: string }>;
  models: Record<string, string[]>;
  limits: {
    queryChars: number | null;
    kMin: number | null;
    kMax: number | null;
    titleMax: number | null;
    classesMax: number | null;
  };
}

function splitTopLevel(inner: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of inner) {
    if ("{[(".includes(ch)) depth += 1;
    if ("}])".includes(ch)) depth -= 1;
    if (ch === "," && depth === 0) {
      parts.push(cur);
      cur = "";
    } else cur += ch;
  }
  if (cur.trim()) parts.push(cur);
  return parts;
}

export function normalizePath(p: string): string {
  return p.replace(/^\/api\/v1/, "").replace(/\$\{[^}]*\}/g, "{}").replace(/\{[^}]*\}/g, "{}");
}

export function collectDocFacts(doc: string): DocFacts {
  const s2 = section(doc, /^## 2\./);
  const s7 = section(doc, /^## 7\./);
  const s114 = section(doc, /^### 11\.4/);
  const s8 = section(doc, /^## 8\./);
  const s9 = section(doc, /^## 9\./);
  const s113 = section(doc, /^### 11\.3/);
  const s41 = section(doc, /^### 4\.1/);
  const s5 = section(doc, /^## 5\./);
  const s111 = section(doc, /^### 11\.1/);

  // Section 11.3 holds two tables: the injection flag codes, then the HTTP codes of test search.
  const s113Tables = tableBlocks(s113);
  const httpErrorCodes = [
    ...new Set([
      ...firstColumnCodes(s2),
      ...firstColumnCodes(s7),
      ...s113Tables.slice(1).flatMap(codesOf),
      ...firstColumnCodes(s114),
    ]),
  ];

  // Only the first sentence names codes; later sentences of that line mention statuses such as `queued`.
  const unchanged = (s8.split(/\r?\n/).find((l) => l.startsWith("Unchanged for comparison")) ?? "").split(". ")[0];
  const versionFailureCodes = [
    ...new Set([
      ...firstColumnCodes(s8),
      ...backticked(unchanged).filter((t) => /^[a-z][a-z0-9_]*$/.test(t)),
      ...firstColumnCodes(s9),
    ]),
  ];

  const flagCodes = codesOf(s113Tables[0] ?? []);

  const routes: DocFacts["routes"] = [];
  const permissions = new Set<string>();
  for (const cells of tableRows(s41)) {
    const first = /^`((?:GET|POST|PATCH|PUT|DELETE) [^`]+)`/.exec(cells[0] ?? "");
    if (!first) continue;
    const [method, rawPath] = first[1].split(" ");
    const full = rawPath.startsWith("...") ? `/api/v1/documents/{document_id}${rawPath.slice(3)}` : rawPath;
    for (const t of backticked(cells[1] ?? "")) if (/^[a-z_]+:[a-z_]+$/.test(t)) permissions.add(t);
    routes.push({ key: `${method} ${normalizePath(full)}`, permission: cells[1] ?? "", requestCell: cells[2] ?? "" });
  }

  const models: Record<string, string[]> = {};
  for (const m of s5.matchAll(/`(\w+)`(?: \([^)]*\))?: `(\{[^`]*\})`/g)) {
    models[m[1]] = splitTopLevel(m[2].slice(1, -1))
      .map((p) => /^\s*([a-z_]+)/.exec(p)?.[1] ?? "")
      .filter(Boolean);
  }
  const testSearchLine = s5.split(/\r?\n/).find((l) => l.includes("`TestSearchRequest`")) ?? "";
  const k = /k\?: integer (\d+)-(\d+)/.exec(testSearchLine);
  const queryChars = /`RETRIEVAL_MAX_QUERY_CHARS` \((\d+)\)/.exec(s111);
  const uploadRow = tableRows(s41).find((c) => (c[0] ?? "").startsWith("`POST /api/v1/documents`"));
  const title = /`title` \(1-(\d+) characters/.exec(uploadRow?.[2] ?? "");
  const classes = /`class_ids` \([^)]*?1-(\d+)\)/.exec(uploadRow?.[2] ?? "");

  return {
    httpErrorCodes,
    versionFailureCodes,
    flagCodes,
    permissions: [...permissions],
    routes,
    models,
    limits: {
      queryChars: queryChars ? Number(queryChars[1]) : null,
      kMin: k ? Number(k[1]) : null,
      kMax: k ? Number(k[2]) : null,
      titleMax: title ? Number(title[1]) : null,
      classesMax: classes ? Number(classes[1]) : null,
    },
  };
}

// ---------- reading the frontend ----------

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name !== "__tests__") out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name)) out.push(full);
  }
  return out;
}

export interface FrontendFacts {
  routes: string[];
  permissionsUsed: string[];
  listParamKeys: string[];
  flagsParamKeys: string[];
  searchBodyKeys: string[];
  publishBodyKeys: string[];
  uploadFormKeys: string[];
  newVersionFormKeys: string[];
  schemaKeys: Record<string, string[]>;
  searchMaxChars: number;
  searchMaxK: number;
  titleMax: number | null;
  classesMax: number | null;
}

export async function collectFrontendFacts(): Promise<FrontendFacts> {
  const apiSrc = fs.readFileSync(path.join(DOCUMENTS_DIR, "api/documentsApi.ts"), "utf8");
  const routes = [...apiSrc.matchAll(/client\.(get|post|patch|put|delete)\(\s*([`"'])(.*?)\2/g)].map(
    (m) => `${m[1].toUpperCase()} ${normalizePath(m[3])}`
  );
  const used = new Set<string>();
  for (const file of sourceFiles(SRC_DIR)) {
    const text = fs.readFileSync(file, "utf8");
    for (const m of text.matchAll(/["']((?:document|settings):[a-z_]+)["']/g)) used.add(m[1]);
  }
  const listBlock = /export interface DocumentListParams \{([^}]*)\}/.exec(apiSrc)?.[1] ?? "";
  const listParamKeys = [...listBlock.matchAll(/^\s*(\w+)\??:/gm)].map((m) => m[1]);

  let flagsParams: Record<string, unknown> = {};
  const fake = {
    get: async (_url: string, config?: { params?: Record<string, unknown> }) => {
      flagsParams = config?.params ?? {};
      return { data: {} };
    },
  };
  try {
    await createDocumentsApi(fake as never).flags("d", "v", { afterOrdinal: 1, limit: 10 });
  } catch {
    // the empty response fails the schema; only the request params matter here
  }

  const fullSearch = buildTestSearchBody({ query: "q", classId: "c", subjectId: "s", includeDrafts: true, k: 5 });
  const upload = buildUploadFormData({ file: new File(["x"], "a.txt"), title: "t", subject_id: "s", class_ids: ["c"] });
  const newVersion = buildNewVersionFormData({ file: new File(["x"], "a.txt") });

  const schemaKeys: Record<string, string[]> = {};
  for (const [model, schema] of Object.entries(SCHEMA_FOR_MODEL)) schemaKeys[model] = Object.keys(schema.shape);

  const dialog = fs.readFileSync(path.join(DOCUMENTS_DIR, "components/UploadDialog.tsx"), "utf8");
  const titleMax = /maxLength=\{(\d+)\}/.exec(dialog)?.[1];
  const classesMax = /selectedClassIds\.length > (\d+)/.exec(dialog)?.[1];

  // The largest k the body builder still sends.
  let maxK = 0;
  for (let k = 1; k <= 60; k += 1) if ("k" in buildTestSearchBody({ query: "q", k })) maxK = k;

  return {
    routes,
    permissionsUsed: [...used],
    listParamKeys,
    flagsParamKeys: Object.keys(flagsParams),
    searchBodyKeys: Object.keys(fullSearch),
    publishBodyKeys: Object.keys(buildPublishBody(true)),
    uploadFormKeys: [...new Set([...upload.keys()])],
    newVersionFormKeys: [...new Set([...newVersion.keys()])],
    schemaKeys,
    searchMaxChars: SEARCH_MAX_QUERY_CHARS,
    searchMaxK: maxK,
    titleMax: titleMax ? Number(titleMax) : null,
    classesMax: classesMax ? Number(classesMax) : null,
  };
}

// ---------- comparing ----------

export function findDrift(doc: DocFacts, fe: FrontendFacts): string[] {
  const problems: string[] = [];
  const add = (m: string) => problems.push(m);

  // sanity: a parser that finds nothing must not pass silently
  if (doc.httpErrorCodes.length < 30)
    add(`DOC PARSE: only ${doc.httpErrorCodes.length} HTTP error codes found in sections 2, 7 and 11.4 (expected 30 or more); the doc layout changed`);
  if (doc.versionFailureCodes.length < 12)
    add(`DOC PARSE: only ${doc.versionFailureCodes.length} version failure codes found in sections 8 and 9; the doc layout changed`);
  if (doc.routes.length < 15) add(`DOC PARSE: only ${doc.routes.length} routes found in section 4.1; the doc layout changed`);
  if (doc.flagCodes.length < 8)
    add(`DOC PARSE: only ${doc.flagCodes.length} injection flag codes found in section 11.3; the doc layout changed`);
  if (Object.keys(doc.models).length < 8)
    add(`DOC PARSE: only ${Object.keys(doc.models).length} response models found in section 5; the doc layout changed`);

  // 1. error codes
  // A code can also be handled by name inside getDocumentError (for example the flags acknowledgement wording).
  const errorsSource = fs.readFileSync(path.join(DOCUMENTS_DIR, "utils/documentErrors.ts"), "utf8");
  const mapped = new Set(MAPPED_ERROR_CODES);
  for (const m of errorsSource.matchAll(/code === "([a-z_]+)"/g)) mapped.add(m[1]);
  for (const code of doc.httpErrorCodes) {
    const reason = INTENTIONALLY_GENERIC_ERROR_CODES[code];
    if (!mapped.has(code) && !reason)
      add(`ERROR CODE: the doc lists "${code}" but documentErrors.ts has no message for it (add a message, or add it to INTENTIONALLY_GENERIC_ERROR_CODES with a reason)`);
    if (mapped.has(code) && reason)
      add(`ERROR CODE: "${code}" is mapped now but still in INTENTIONALLY_GENERIC_ERROR_CODES; remove it from the list`);
  }
  for (const code of Object.keys(INTENTIONALLY_GENERIC_ERROR_CODES)) {
    if (!doc.httpErrorCodes.includes(code))
      add(`ERROR CODE: "${code}" is in INTENTIONALLY_GENERIC_ERROR_CODES but the doc no longer lists it; remove it from the list`);
  }
  const fallback = getVersionFailure(null).message;
  for (const code of doc.versionFailureCodes) {
    if (getVersionFailure(code).message === fallback)
      add(`VERSION FAILURE CODE: the doc lists "${code}" but getVersionFailure has no message for it`);
  }
  for (const code of doc.flagCodes) {
    if (!(code in FLAG_CODE_DESCRIPTIONS)) add(`FLAG CODE: the doc lists injection flag "${code}" but flagDescriptions.ts has no text for it`);
  }
  for (const code of Object.keys(FLAG_CODE_DESCRIPTIONS)) {
    if (!doc.flagCodes.includes(code)) add(`FLAG CODE: flagDescriptions.ts describes "${code}" but the doc no longer lists it`);
  }

  // 2. permission names
  const docDocumentPerms = doc.permissions.filter((p) => p.startsWith("document:"));
  for (const p of fe.permissionsUsed) {
    if (!doc.permissions.includes(p))
      add(`PERMISSION: the frontend uses "${p}" but the doc's route table has no such permission (documented: ${doc.permissions.join(", ")})`);
  }
  for (const p of docDocumentPerms) {
    if (!fe.permissionsUsed.includes(p)) add(`PERMISSION: the doc lists "${p}" but no frontend source uses it`);
  }
  if (!doc.permissions.includes("settings:manage"))
    add(`PERMISSION: the doc no longer lists "settings:manage" (the usage page relies on it)`);

  // 3. route paths
  const docRouteKeys = doc.routes.map((r) => r.key);
  for (const r of fe.routes) {
    if (!docRouteKeys.includes(r))
      add(`ROUTE: the frontend calls "${r}" but the doc has no such route (documented: ${docRouteKeys.join("; ")})`);
  }
  for (const r of docRouteKeys) {
    const reason = ROUTES_NOT_USED_BY_FRONTEND[r];
    if (!fe.routes.includes(r) && !reason)
      add(`ROUTE: the doc lists "${r}" but the frontend never calls it (call it, or add it to ROUTES_NOT_USED_BY_FRONTEND with a reason)`);
    if (fe.routes.includes(r) && reason)
      add(`ROUTE: "${r}" is called now but still in ROUTES_NOT_USED_BY_FRONTEND; remove it from the list`);
  }
  for (const r of Object.keys(ROUTES_NOT_USED_BY_FRONTEND)) {
    if (!docRouteKeys.includes(r))
      add(`ROUTE: "${r}" is in ROUTES_NOT_USED_BY_FRONTEND but the doc no longer lists it; remove it from the list`);
  }

  // 4. response field names the schemas rely on
  for (const [model, keys] of Object.entries(fe.schemaKeys)) {
    const documented = doc.models[model];
    if (!documented) {
      add(`FIELDS: the frontend has a schema for "${model}" but the doc's section 5 no longer describes that model`);
      continue;
    }
    for (const key of keys) {
      if (!documented.includes(key))
        add(`FIELD: the frontend schema for ${model} expects "${key}" but the doc lists only: ${documented.join(", ")}`);
    }
  }

  // 5. request field names and query parameters
  const findRoute = (key: string) => doc.routes.find((r) => r.key === key);
  const requestTokens = (key: string) =>
    backticked(findRoute(key)?.requestCell ?? "").map((t) => /^[a-z_]+/.exec(t)?.[0] ?? t);
  const testSearchFields = doc.models["TestSearchRequest"] ?? [];
  for (const k of fe.searchBodyKeys)
    if (!testSearchFields.includes(k))
      add(`REQUEST FIELD: test search sends "${k}" but TestSearchRequest in the doc has: ${testSearchFields.join(", ") || "(none found)"}`);
  const publishFields = doc.models["PublishRequest"] ?? [];
  for (const k of fe.publishBodyKeys)
    if (!publishFields.includes(k))
      add(`REQUEST FIELD: publish sends "${k}" but PublishRequest in the doc has: ${publishFields.join(", ") || "(none found)"}`);
  const uploadFields = requestTokens("POST /documents");
  for (const k of fe.uploadFormKeys)
    if (!uploadFields.includes(k)) add(`REQUEST FIELD: upload sends form field "${k}" but the doc's upload row has: ${uploadFields.join(", ")}`);
  const versionFields = requestTokens("POST /documents/{}/versions");
  for (const k of fe.newVersionFormKeys)
    if (!versionFields.includes(k)) add(`REQUEST FIELD: new version sends form field "${k}" but the doc's row has: ${versionFields.join(", ")}`);
  const listFields = requestTokens("GET /documents");
  for (const k of fe.listParamKeys)
    if (!listFields.includes(k)) add(`QUERY PARAMETER: the list call uses "${k}" but the doc's list row has: ${listFields.join(", ")}`);
  const flagsFields = requestTokens("GET /documents/{}/versions/{}/flags");
  for (const k of fe.flagsParamKeys)
    if (!flagsFields.includes(k)) add(`QUERY PARAMETER: the flags call uses "${k}" but the doc's flags row has: ${flagsFields.join(", ")}`);

  // 6. limits
  const lim = doc.limits;
  if (lim.queryChars === null) add("LIMIT: the doc no longer states the test-search question limit (RETRIEVAL_MAX_QUERY_CHARS) in section 11.1");
  else if (lim.queryChars !== fe.searchMaxChars)
    add(`LIMIT: the doc says the question may have ${lim.queryChars} characters but the frontend allows ${fe.searchMaxChars}`);
  if (lim.kMax === null || lim.kMin === null) add("LIMIT: the doc no longer states the range of k for TestSearchRequest");
  else {
    if (lim.kMin !== 1) add(`LIMIT: the doc says k starts at ${lim.kMin}; the frontend assumes 1`);
    if (lim.kMax !== fe.searchMaxK) add(`LIMIT: the doc says k goes up to ${lim.kMax} but the frontend sends up to ${fe.searchMaxK}`);
  }
  if (lim.titleMax === null) add("LIMIT: the doc no longer states the title length limit in the upload row");
  else if (lim.titleMax !== fe.titleMax)
    add(`LIMIT: the doc says a title has at most ${lim.titleMax} characters but the upload dialog uses ${fe.titleMax}`);
  if (lim.classesMax === null) add("LIMIT: the doc no longer states the class_ids limit in the upload row");
  else if (lim.classesMax !== fe.classesMax)
    add(`LIMIT: the doc says at most ${lim.classesMax} classes but the upload dialog uses ${fe.classesMax}`);

  return problems;
}
