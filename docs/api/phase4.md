# Phase 4 API Reference: Timetables, Scheduling, Ingestion & Timezone

Comprehensive API contract specification for Phase 4 backend endpoints, covering schedule lookup, timetable management, scoped teacher picking, secure multi-format import, template generation, and tenant timezone settings.

---

## 1. Authentication & Security Headers

All endpoints require standard tenant-scoped JWT bearer authorization and tenant host routing headers:

```http
Authorization: Bearer <access_token>
X-Tenant-Slug: <tenant_slug>
```

Tenant context is derived and locked at the transaction boundary (`SET LOCAL app.tenant_id = :tenant_id`) under Row-Level Security (`NOBYPASSRLS`).

---

## 2. Schedule & Lookup Endpoints (`/me`)

Deterministic schedule retrieval for authenticated users without LLM overhead. Single typed Pydantic contract per endpoint across all roles.

### 2.1 What is my next lecture?

- **Route**: `GET /api/v1/me/next-lecture`
- **Permission**: `timetable:view` (Granted to `tenant_admin`, `teacher`, `student`)
- **Query Parameters**:
  - `class_id` (`uuid.UUID`, optional): Filter next lecture to a specific class.
- **Response Model**: `NextLectureResponse` (HTTP 200)
  ```json
  {
    "status": "upcoming_today",
    "message": "Next class: Mathematics at 09:00",
    "current_lecture": null,
    "next_lecture": {
      "class_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "class_name": "Grade 10-A",
      "subject_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "subject_name": "Mathematics",
      "subject_code": "MATH101",
      "teacher_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "teacher_name": "Dr. Alan Turing",
      "day_of_week": 0,
      "day_name": "Monday",
      "start_time": "09:00",
      "end_time": "10:00",
      "room": "Room 204",
      "note": null
    },
    "tenant_timezone": "America/New_York"
  }
  ```
- **Statuses**: `in_progress`, `upcoming_today`, `upcoming_future`, `no_more_today`, `no_lectures_scheduled`, `no_published_timetable`, `not_enrolled`.
- **Error Codes**:
  - `401 Unauthorized`: Invalid or expired JWT token.
  - `403 Forbidden`: Token lacks `timetable:view`.

---

### 2.2 My Published Timetable

- **Route**: `GET /api/v1/me/timetable`
- **Permission**: `timetable:view` (Granted to `tenant_admin`, `teacher`, `student`)
- **Query Parameters**:
  - `class_id` (`uuid.UUID`, optional): Filter to a single class.
  - `day_of_week` (`int` 0..6, optional): Filter to a specific weekday (0=Mon..6=Sun).
- **Response Model**: `MyTimetableResponse` (HTTP 200)
  ```json
  {
    "tenant_timezone": "America/New_York",
    "classes": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "tenant_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "class_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "class_name": "Grade 10-A",
        "academic_year": "2026-2027",
        "term": "Fall",
        "status": "published",
        "version": 1,
        "entries": [],
        "created_at": "2026-11-13T08:00:00Z",
        "updated_at": "2026-11-13T08:00:00Z"
      }
    ],
    "teaching_entries": []
  }
  ```
- **Error Codes**:
  - `401 Unauthorized`: Invalid or expired JWT token.
  - `403 Forbidden`: Token lacks `timetable:view`.

---

## 3. Scoped Teacher Picker Endpoints

Allows teachers managing a class timetable to populate teacher select dropdowns without granting broad `assignment:manage` privileges.

### 3.1 List Assigned Teachers for Class and Subject

- **Route**: `GET /api/v1/classes/{class_id}/subjects/{subject_id}/assigned-teachers`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response Model**: `list[AvailableTeacherItem]` (HTTP 200)
  ```json
  [
    {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "full_name": "Dr. Alan Turing",
      "email": "turing@university.local"
    }
  ]
  ```
- **Error Codes**:
  - `401 Unauthorized`: Missing or invalid token.
  - `403 Forbidden`: Caller lacks `timetable:manage`.
  - `404 Not Found`: Generic `{"code": "not_found", "message": "Resource not found"}` if caller is not assigned to `class_id` or `class_id` does not exist.

### 3.2 List Assigned Teachers for Class (All or Filtered by Subject)

- **Route**: `GET /api/v1/classes/{class_id}/assigned-teachers`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Query Parameters**:
  - `subject_id` (`uuid.UUID`, optional): Optional subject filter.
- **Response Model**: `list[AvailableTeacherItem]` (HTTP 200)

---

## 4. Timetable Management Endpoints

Draft creation, entry manipulation, conflict checking, and publish/unpublish workflow.

### 4.1 List Class Timetables

- **Route**: `GET /api/v1/classes/{class_id}/timetables`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response Model**: `list[TimetableResponse]` (HTTP 200)

### 4.2 Get or Initialize Draft Timetable

- **Route**: `GET /api/v1/classes/{class_id}/timetables/draft`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Behavior**: Returns existing draft, or clones published version into new draft version, or initializes blank draft.
- **Response Model**: `TimetableResponse` (HTTP 200)

### 4.3 Create or Overwrite Draft Metadata & Entries

- **Route**: `POST /api/v1/classes/{class_id}/timetables/draft`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `TimetableCreate`
  ```json
  {
    "academic_year": "2026-2027",
    "term": "Fall",
    "entries": []
  }
  ```
- **Response Model**: `TimetableResponse` (HTTP 200)
- **Error Codes**:
  - `422 Unprocessable Content`: `entries_cap_exceeded` (>200 entries), `subject_not_linked`, `teacher_not_assigned`.

### 4.4 Bulk Update Draft Metadata & Entries

- **Route**: `PUT /api/v1/classes/{class_id}/timetables/draft`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `TimetableUpdate`
- **Response Model**: `TimetableResponse` (HTTP 200)
- **Error Codes**: `404 Not Found` (`no_draft`), `422 Unprocessable Content`.

### 4.5 Add Single Entry to Draft

- **Route**: `POST /api/v1/classes/{class_id}/timetables/draft/entries`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `TimetableEntryCreate`
  ```json
  {
    "day_of_week": 0,
    "start_time": "09:00",
    "end_time": "10:00",
    "subject_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "teacher_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "room": "Room 204",
    "note": "Bring scientific calculator"
  }
  ```
- **Response Model**: `TimetableEntryResponse` (HTTP 200)
- **Audited**: `timetable:create_entry`
- **Error Codes**:
  - `422 Unprocessable Content`: `entries_cap_exceeded` (when total >= 200), `subject_not_linked`, `teacher_not_assigned`, `invalid_time_range` (`end_time <= start_time`).

### 4.6 Update Draft Entry

- **Route**: `PUT /api/v1/classes/{class_id}/timetables/draft/entries/{entry_id}`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `TimetableEntryUpdate`
- **Response Model**: `TimetableEntryResponse` (HTTP 200)
- **Audited**: `timetable:update_entry`
- **Error Codes**: `404 Not Found` (generic `not_found` if entry belongs to another class/tenant), `422 Unprocessable Content`.

### 4.7 Delete Draft Entry

- **Route**: `DELETE /api/v1/classes/{class_id}/timetables/draft/entries/{entry_id}`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response**: HTTP 204 No Content
- **Audited**: `timetable:delete_entry`
- **Error Codes**: `404 Not Found` (generic `not_found`).

### 4.8 Pre-Publish Conflict Check

- **Route**: `POST /api/v1/classes/{class_id}/timetables/check-conflicts`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response Model**: `ConflictCheckResponse` (HTTP 200)
  ```json
  {
    "has_conflicts": true,
    "conflicts": [
      {
        "conflict_type": "room_double_booking",
        "description": "Room 204 is double-booked on Monday 09:00-10:00 with Class 10-B",
        "day_of_week": 0,
        "start_time": "09:00",
        "end_time": "10:00",
        "room": "Room 204",
        "conflicting_class_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "conflicting_class_name": "Grade 10-B"
      }
    ]
  }
  ```

### 4.9 Publish Timetable

- **Route**: `POST /api/v1/classes/{class_id}/timetables/publish`
- **Permission**: `timetable:publish` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `PublishTimetableRequest`
  ```json
  {
    "confirm_conflicts": false
  }
  ```
- **Response Model**: `TimetableResponse` (HTTP 200, `status="published"`)
- **Audited**: `timetable:publish`
- **Error Codes**:
  - `404 Not Found`: `no_draft`
  - `409 Conflict`: `timetable_conflicts_detected` with `conflicts: list[ConflictDetail]` returned if double bookings exist and `confirm_conflicts` is not `true`.

### 4.10 Unpublish Timetable

- **Route**: `POST /api/v1/classes/{class_id}/timetables/unpublish`
- **Permission**: `timetable:publish` scoped to `Scope(ScopeType.CLASS)`
- **Response Model**: `TimetableResponse` (HTTP 200, `status="draft"`)
- **Audited**: `timetable:unpublish`
- **Error Codes**: `404 Not Found` (`no_published_timetable`).

---

## 5. File Import & Template Endpoints

Upload and parse schedule files (CSV, XLSX, DOCX, text PDF) using an isolated kill-able process with Redis rate limits and concurrency caps.

### 5.1 Download CSV Template

- **Route**: `GET /api/v1/classes/{class_id}/timetables/templates/csv`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response**: `text/csv; charset=utf-8` stream with formula-neutralized prefilled class subjects and assigned teachers.

### 5.2 Download XLSX Template

- **Route**: `GET /api/v1/classes/{class_id}/timetables/templates/xlsx`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Response**: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` stream with data-validation dropdowns.

### 5.3 Import Preview (Dry-run parser)

- **Route**: `POST /api/v1/classes/{class_id}/timetables/import-preview`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request**: `multipart/form-data` with `file: UploadFile` (max size: 2 MB)
- **Response Model**: `ImportPreviewResponse` (HTTP 200)
  ```json
  {
    "filename": "timetable.xlsx",
    "total_rows": 24,
    "valid_rows": 23,
    "error_rows": 1,
    "requires_period_times": false,
    "period_labels": [],
    "rows": [
      {
        "row_number": 2,
        "day_of_week": 0,
        "weekday_raw": "Monday",
        "start_time": "09:00",
        "end_time": "10:00",
        "subject_raw": "MATH101",
        "teacher_raw": "turing@university.local",
        "room": "Room 204",
        "note": null,
        "subject_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "teacher_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "is_valid": true,
        "error_message": null,
        "warnings": []
      }
    ],
    "available_subjects": [],
    "available_teachers": []
  }
  ```
- **Hardening Rules**:
  - Does NOT create or change database records.
  - Does NOT store original uploaded file to disk or log file content.
  - Rate limited (3 req / 60s per user) and concurrency-capped (1 concurrent parse per tenant, 5 globally) in Redis with short TTL and `finally` release.
- **Error Codes**:
  - `413 Content Too Large`: `file_too_large` (>2 MB).
  - `422 Unprocessable Content`: `scanned_pdf_unsupported`, `unreadable_file`, `unsupported_file_type`.
  - `429 Too Many Requests`: `import_rate_limit_exceeded`, `import_concurrency_limit_exceeded`.

### 5.4 Save Imported Draft

- **Route**: `POST /api/v1/classes/{class_id}/timetables/save-imported-draft`
- **Permission**: `timetable:manage` scoped to `Scope(ScopeType.CLASS)`
- **Request Model**: `SaveImportedDraftRequest`
  ```json
  {
    "replace_existing": false,
    "entries": [
      {
        "day_of_week": 0,
        "start_time": "09:00",
        "end_time": "10:00",
        "subject_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "teacher_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "room": "Room 204"
      }
    ]
  }
  ```
- **Response Model**: `TimetableResponse` (HTTP 200)
- **Error Codes**:
  - `409 Conflict`: `draft_already_exists` when an existing draft exists and `replace_existing` is `false`.
  - `422 Unprocessable Content`: Row-level academic re-validation failure or entries > 200.

---

## 6. Tenant Timezone Settings Endpoints

Configure and read tenant timezone validated against standard IANA names (`zoneinfo`).

### 6.1 Get Tenant Timezone

- **Route**: `GET /api/v1/admin/settings/timezone`
- **Permission**: `tenant:view` / `tenant_admin`
- **Response Model**: `TimezoneResponse` (HTTP 200)
  ```json
  {
    "timezone": "America/New_York"
  }
  ```

### 6.2 Update Tenant Timezone

- **Route**: `PATCH /api/v1/admin/settings/timezone`
- **Permission**: `tenant:manage` / `tenant_admin`
- **Request Model**: `TimezoneUpdateRequest`
  ```json
  {
    "timezone": "America/New_York"
  }
  ```
- **Response Model**: `TimezoneResponse` (HTTP 200)
- **Audited**: `tenant:update_timezone`
- **Error Codes**:
  - `403 Forbidden`: `permission_denied` (teacher / student callers rejected).
  - `422 Unprocessable Content`: `invalid_timezone` (rejected if not present in `zoneinfo.available_timezones()`).

