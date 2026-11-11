import React, { useEffect, useState, useMemo } from "react";
import { useLocation } from "react-router-dom";
import { AppShell, PageHeader } from "@/components/layout";
import {
  timetableApi,
  TimetableEntryItem,
  MyTimetableResponse,
} from "../api/timetableApi";
import { WeeklyTimetableGrid } from "../components/WeeklyTimetableGrid";
import { NextLectureCard } from "../components/NextLectureCard";
import { Select } from "@/components/ui/Select";
import { Badge } from "@/components/ui/Badge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { usePermission } from "@/hooks/usePermission";
import {
  getPythonWeekday,
  WEEKDAYS,
  formatTimeRange,
} from "../utils/timetableHelpers";
import { academicApi, ClassItem } from "@/features/academic/api/academicApi";
import {
  Calendar,
  Clock,
  User,
  DoorOpen,
  School,
  CheckCircle2,
} from "lucide-react";

export const ScheduleViewPage: React.FC = () => {
  const location = useLocation();
  const { isAdmin } = usePermission();

  const isTeacherPortal = location.pathname.includes("/teacher");
  const isStudentPortal = location.pathname.includes("/student");

  // Admin class filter
  const [classes, setClasses] = useState<ClassItem[]>([]);
  const [selectedClassId, setSelectedClassId] = useState<string>("");

  const [timetableData, setTimetableData] = useState<MyTimetableResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const todayIndex = getPythonWeekday();

  // Load classes if Admin view
  useEffect(() => {
    if (!isAdmin || isTeacherPortal || isStudentPortal) return;
    academicApi
      .listClasses({ page_size: 100 })
      .then((res) => setClasses(res.items || []))
      .catch(() => {});
  }, [isAdmin, isTeacherPortal, isStudentPortal]);

  // Fetch timetable data
  useEffect(() => {
    let isMounted = true;
    setIsLoading(true);
    setError(null);

    const fetchPromise: Promise<MyTimetableResponse> =
      isAdmin && !isTeacherPortal && !isStudentPortal && selectedClassId
        ? timetableApi.getClassTimetable(selectedClassId).then((tt) => ({
            tenant_timezone: "",
            classes: [tt],
            teaching_entries: [],
          }))
        : timetableApi.getMyTimetable();

    fetchPromise
      .then((data) => {
        if (!isMounted) return;
        setTimetableData(data || null);
      })
      .catch((err) => {
        if (!isMounted) return;
        if (err.response?.status === 404) {
          setTimetableData({ tenant_timezone: "", classes: [], teaching_entries: [] });
        } else {
          setError(err.response?.data?.detail?.message || "Failed to load schedule.");
        }
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [isAdmin, isTeacherPortal, isStudentPortal, selectedClassId]);

  // Flatten all entries across published timetables defensively
  const allEntries: TimetableEntryItem[] = useMemo(() => {
    if (!timetableData) return [];

    const list: TimetableEntryItem[] = [];

    // For teacher: their teaching_entries has all sessions they instruct
    if (
      isTeacherPortal &&
      Array.isArray(timetableData.teaching_entries) &&
      timetableData.teaching_entries.length > 0
    ) {
      return timetableData.teaching_entries;
    }

    // Collect from classes
    if (Array.isArray(timetableData.classes)) {
      for (const tt of timetableData.classes) {
        if (Array.isArray(tt?.entries)) {
          list.push(...tt.entries);
        }
      }
    }

    // Also include any teaching_entries not already in list
    if (Array.isArray(timetableData.teaching_entries)) {
      for (const te of timetableData.teaching_entries) {
        if (!list.some((existing) => existing.id === te.id)) {
          list.push(te);
        }
      }
    }

    return list;
  }, [timetableData, isTeacherPortal]);

  // Today's lectures
  const todayEntries = useMemo(() => {
    return allEntries
      .filter((e) => e.day_of_week === todayIndex)
      .sort((a, b) => a.start_time.localeCompare(b.start_time));
  }, [allEntries, todayIndex]);

  const pageTitle = isTeacherPortal
    ? "My Teaching Schedule"
    : isStudentPortal
    ? "Class Timetable"
    : "Academic Timetable";

  const pageDesc = isTeacherPortal
    ? "View your weekly teaching schedule and upcoming lectures across assigned classes."
    : isStudentPortal
    ? "Your official enrolled weekly timetable, room numbers, and today's schedule."
    : "Organization-wide class schedule and lecture calendar.";

  return (
    <AppShell tenantName="Academic Workspace">
      <PageHeader
        title={pageTitle}
        description={pageDesc}
        breadcrumbs={[
          { label: "Workspace", href: isTeacherPortal ? "/teacher/dashboard" : isStudentPortal ? "/student/dashboard" : "/admin" },
          { label: "Schedule" },
        ]}
      />

      {/* Top Section: Real-time Next Lecture Intelligence Card */}
      <div className="mb-6">
        <NextLectureCard isAdmin={isAdmin && !isTeacherPortal && !isStudentPortal} />
      </div>

      {/* Admin Class Filter */}
      {isAdmin && !isTeacherPortal && !isStudentPortal && classes.length > 0 && (
        <div className="p-4 rounded-lg border border-border bg-surface mb-6 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <School className="w-4 h-4 text-primary" />
            <span className="text-sm font-semibold text-text">Class Schedule Filter</span>
          </div>
          <div className="w-full sm:w-64">
            <Select
              value={selectedClassId}
              onChange={(e) => setSelectedClassId(e.target.value)}
              options={[
                { value: "", label: "All Classes (Combined)" },
                ...classes.map((c) => ({ value: c.id, label: `${c.name} (${c.kind})` })),
              ]}
              className="text-xs"
            />
          </div>
        </div>
      )}

      {/* Today Section */}
      <div className="mb-8 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Clock className="w-5 h-5 text-primary" />
            <h3 className="text-base font-bold text-text">
              Today&rsquo;s Schedule ({WEEKDAYS[todayIndex]?.label})
            </h3>
          </div>
          <Badge variant={todayEntries.length > 0 ? "primary" : "default"} size="sm">
            {todayEntries.length} {todayEntries.length === 1 ? "lecture" : "lectures"}
          </Badge>
        </div>

        {isLoading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {[...Array(3)].map((_, i) => (
              <Skeleton key={i} className="h-28 w-full rounded-lg" />
            ))}
          </div>
        ) : todayEntries.length === 0 ? (
          <div className="p-5 border border-dashed border-border rounded-lg bg-surface/40 text-center">
            <CheckCircle2 className="w-6 h-6 text-success mx-auto mb-1.5 opacity-80" />
            <p className="text-xs font-medium text-text">No lectures scheduled for today.</p>
            <p className="text-[11px] text-text-muted mt-0.5">
              Enjoy your free day or prepare for upcoming classes!
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {todayEntries.map((entry) => (
              <Card key={entry.id} className="border-border hover:border-primary/40 transition-colors">
                <CardHeader className="pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="text-sm font-bold text-text">
                      {entry.subject_name}
                      {entry.subject_code && (
                        <span className="text-xs font-normal text-text-muted ml-1">
                          ({entry.subject_code})
                        </span>
                      )}
                    </CardTitle>
                    <span className="text-xs font-bold text-primary shrink-0">
                      {formatTimeRange(entry.start_time, entry.end_time)}
                    </span>
                  </div>
                </CardHeader>
                <CardContent className="pt-0 text-xs text-text-muted space-y-1">
                  {entry.teacher_name && (
                    <div className="flex items-center gap-1.5 truncate">
                      <User className="w-3.5 h-3.5 opacity-70 shrink-0" />
                      <span className="truncate">{entry.teacher_name}</span>
                    </div>
                  )}
                  {entry.room && (
                    <div className="flex items-center gap-1.5 truncate">
                      <DoorOpen className="w-3.5 h-3.5 opacity-70 shrink-0" />
                      <span className="truncate">{entry.room}</span>
                    </div>
                  )}
                  {entry.note && (
                    <p className="text-[11px] italic text-text-muted pt-1 border-t border-border/50 truncate">
                      {entry.note}
                    </p>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* Weekly Schedule Section */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Calendar className="w-5 h-5 text-primary" />
            <h3 className="text-base font-bold text-text">Weekly Timetable Overview</h3>
          </div>
          <span className="text-xs text-text-muted">
            {allEntries.length} total sessions
          </span>
        </div>

        {error ? (
          <div className="p-6 text-center border border-border rounded-lg bg-surface space-y-2">
            <p className="text-sm font-semibold text-danger">{error}</p>
          </div>
        ) : (
          <WeeklyTimetableGrid
            entries={allEntries}
            isLoading={isLoading}
            canEdit={false}
          />
        )}
      </div>
    </AppShell>
  );
};
