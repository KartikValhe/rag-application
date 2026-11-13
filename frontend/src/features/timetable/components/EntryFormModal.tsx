import React, { useEffect, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Alert } from "@/components/ui/Alert";
import { timetableApi, TimetableEntryItem, EntryFormPayload, AssignedTeacher } from "../api/timetableApi";
import { WEEKDAYS, isEndTimeAfterStartTime, normalizeTimeString, teacherDisplayName } from "../utils/timetableHelpers";
import { academicApi, SubjectItem } from "@/features/academic/api/academicApi";
import { getFriendlyError } from "@/api/errorMessages";

export interface EntryFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  classId: string;
  initialData?: TimetableEntryItem | null;
  onSave: (payload: EntryFormPayload) => Promise<void>;
  isSaving?: boolean;
}

export const EntryFormModal: React.FC<EntryFormModalProps> = ({
  isOpen,
  onClose,
  classId,
  initialData,
  onSave,
  isSaving = false,
}) => {
  const [subjects, setSubjects] = useState<SubjectItem[]>([]);
  const [teachers, setTeachers] = useState<AssignedTeacher[]>([]);
  const [isLoadingRelations, setIsLoadingRelations] = useState(false);

  // Form states
  const [dayOfWeek, setDayOfWeek] = useState<number>(0);
  const [subjectId, setSubjectId] = useState<string>("");
  const [teacherId, setTeacherId] = useState<string>("");
  const [startTime, setStartTime] = useState<string>("09:00");
  const [endTime, setEndTime] = useState<string>("10:00");
  const [room, setRoom] = useState<string>("");
  const [note, setNote] = useState<string>("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Load subjects linked to this class
  useEffect(() => {
    if (!isOpen || !classId) return;

    let isMounted = true;
    setIsLoadingRelations(true);
    setErrorMessage(null);

    academicApi
      .listClassSubjects(classId)
      .then((subList) => {
        if (!isMounted) return;
        setSubjects(subList || []);
      })
      .catch((err) => {
        if (!isMounted) return;
        setErrorMessage(getFriendlyError(err, "Failed to load subjects for this class."));
      })
      .finally(() => {
        if (isMounted) setIsLoadingRelations(false);
      });

    return () => {
      isMounted = false;
    };
  }, [isOpen, classId]);

  // Load teachers from the scoped endpoint; narrows to the chosen subject
  useEffect(() => {
    if (!isOpen || !classId) return;

    let isMounted = true;
    timetableApi
      .listAssignedTeachers(classId, subjectId || undefined)
      .then((list) => {
        if (!isMounted) return;
        setTeachers(list);
      })
      .catch((err) => {
        if (!isMounted) return;
        setTeachers([]);
        setErrorMessage(getFriendlyError(err, "Failed to load teachers for this class."));
      });

    return () => {
      isMounted = false;
    };
  }, [isOpen, classId, subjectId]);

  // Populate or reset form values
  useEffect(() => {
    if (initialData) {
      setDayOfWeek(initialData.day_of_week ?? 0);
      setSubjectId(initialData.subject_id || "");
      setTeacherId(initialData.teacher_id || "");
      setStartTime(normalizeTimeString(initialData.start_time) || "09:00");
      setEndTime(normalizeTimeString(initialData.end_time) || "10:00");
      setRoom(initialData.room || "");
      setNote(initialData.note || "");
    } else {
      setDayOfWeek(0);
      setSubjectId("");
      setTeacherId("");
      setStartTime("09:00");
      setEndTime("10:00");
      setRoom("");
      setNote("");
    }
    setErrorMessage(null);
  }, [initialData, isOpen]);

  const availableTeachers = React.useMemo(
    () => teachers.map((t) => ({ value: t.id, label: teacherDisplayName(t) })),
    [teachers]
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    if (!subjectId) {
      setErrorMessage("Please select a subject.");
      return;
    }
    if (!startTime || !endTime) {
      setErrorMessage("Please specify both start and end times.");
      return;
    }
    if (!isEndTimeAfterStartTime(startTime, endTime)) {
      setErrorMessage("End time must be later than start time.");
      return;
    }

    try {
      await onSave({
        day_of_week: Number(dayOfWeek),
        subject_id: subjectId,
        teacher_id: teacherId.trim() ? teacherId : null,
        start_time: startTime,
        end_time: endTime,
        room: room.trim() ? room.trim() : null,
        note: note.trim() ? note.trim() : null,
      });
      onClose();
    } catch (err: unknown) {
      setErrorMessage(getFriendlyError(err, "Failed to save lecture entry. Please check the values."));
    }
  };

  const subjectOptions = subjects.map((s) => ({
    value: s.id,
    label: s.code ? `${s.name} (${s.code})` : s.name,
  }));

  const weekdayOptions = WEEKDAYS.map((w) => ({
    value: String(w.index),
    label: w.label,
  }));

  const teacherOptions = [
    { value: "", label: "-- None / Unassigned --" },
    ...availableTeachers,
  ];

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={initialData ? "Edit Lecture Session" : "Schedule New Lecture"}
      description="Enter the timetable session parameters for this class cohort."
      size="md"
      footer={
        <div className="flex flex-col-reverse sm:flex-row items-center justify-end gap-3 w-full">
          <Button
            type="button"
            variant="secondary"
            onClick={onClose}
            disabled={isSaving}
            className="w-full sm:w-auto min-h-[44px]"
          >
            Cancel
          </Button>
          <Button
            type="submit"
            form="entry-form"
            variant="primary"
            isLoading={isSaving}
            disabled={isLoadingRelations || subjects.length === 0}
            className="w-full sm:w-auto min-h-[44px]"
          >
            {initialData ? "Save Changes" : "Add to Timetable"}
          </Button>
        </div>
      }
    >
      <form id="entry-form" onSubmit={handleSubmit} className="space-y-4">
        {errorMessage && (
          <Alert variant="danger" title="Validation Error">
            {errorMessage}
          </Alert>
        )}

        {subjects.length === 0 && !isLoadingRelations && (
          <Alert variant="warning" title="No Subjects Linked">
            This class has no subjects linked to it yet. Please link subjects under Classes &amp; Subjects before scheduling lectures.
          </Alert>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="sm:col-span-2">
            <Select
              label="Subject *"
              options={subjectOptions}
              value={subjectId}
              onChange={(e) => setSubjectId(e.target.value)}
              disabled={isLoadingRelations || subjects.length === 0}
            />
          </div>

          <div className="sm:col-span-2">
            <Select
              label="Assigned Teacher"
              options={teacherOptions}
              value={teacherId}
              onChange={(e) => setTeacherId(e.target.value)}
              disabled={isLoadingRelations}
            />
          </div>

          <div className="sm:col-span-2">
            <Select
              label="Day of Week *"
              options={weekdayOptions}
              value={String(dayOfWeek)}
              onChange={(e) => setDayOfWeek(Number(e.target.value))}
            />
          </div>

          <div>
            <Input
              label="Start Time *"
              type="time"
              value={startTime}
              onChange={(e) => setStartTime(e.target.value)}
              required
            />
          </div>

          <div>
            <Input
              label="End Time *"
              type="time"
              value={endTime}
              onChange={(e) => setEndTime(e.target.value)}
              required
            />
          </div>

          <div>
            <Input
              label="Room / Location (max 64)"
              type="text"
              placeholder="e.g. Lab 3, Hall B"
              value={room}
              onChange={(e) => setRoom(e.target.value)}
              maxLength={64}
            />
          </div>

          <div>
            <Input
              label="Notes (max 255)"
              type="text"
              placeholder="e.g. Bring lab equipment"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={255}
            />
          </div>
        </div>
      </form>
    </Modal>
  );
};

