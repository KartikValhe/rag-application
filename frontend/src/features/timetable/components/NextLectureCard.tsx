import React, { useEffect, useState } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Skeleton } from "@/components/ui/Skeleton";
import { Select } from "@/components/ui/Select";
import {
  timetableApi,
  NextLectureResponse,
  NextLectureItem,
} from "../api/timetableApi";
import { formatTimeRange } from "../utils/timetableHelpers";
import { Clock, User, DoorOpen, Calendar, AlertCircle, Sparkles } from "lucide-react";
import { academicApi, ClassItem } from "@/features/academic/api/academicApi";

export interface NextLectureCardProps {
  isAdmin?: boolean;
  className?: string;
}

export const NextLectureCard: React.FC<NextLectureCardProps> = ({
  isAdmin = false,
  className = "",
}) => {
  const [selectedClassId, setSelectedClassId] = useState<string>("");
  const [classes, setClasses] = useState<ClassItem[]>([]);
  const [data, setData] = useState<NextLectureResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load class list for admin picker
  useEffect(() => {
    if (!isAdmin) return;
    academicApi
      .listClasses({ page_size: 100 })
      .then((res) => setClasses(res.items || []))
      .catch(() => {});
  }, [isAdmin]);

  // Fetch next lecture
  useEffect(() => {
    let isMounted = true;
    setIsLoading(true);
    setError(null);

    timetableApi
      .getMyNextLecture(selectedClassId || undefined)
      .then((res) => {
        if (!isMounted) return;
        setData(res);
      })
      .catch((err) => {
        if (!isMounted) return;
        setError(err.response?.data?.detail?.message || "Failed to load schedule intelligence.");
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [selectedClassId]);

  const renderLectureItem = (lecture: NextLectureItem, isCurrent: boolean) => (
    <div
      key={lecture.entry_id}
      className={`p-4 rounded-lg border transition-colors ${
        isCurrent
          ? "bg-primary/5 border-primary/30"
          : "bg-surface border-border"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant={isCurrent ? "success" : "primary"} size="sm">
              {isCurrent ? "Happening Now" : "Next Up"}
            </Badge>
            <span className="text-xs font-semibold text-text-muted">
              {lecture.class_name}
            </span>
          </div>
          <h4 className="text-base font-bold text-text">
            {lecture.subject_name}
            {lecture.subject_code && (
              <span className="text-xs font-normal text-text-muted ml-1.5">
                ({lecture.subject_code})
              </span>
            )}
          </h4>
        </div>

        <div className="text-right shrink-0">
          <div className="flex items-center gap-1.5 text-xs font-bold text-primary">
            <Clock className="w-3.5 h-3.5" />
            <span>{formatTimeRange(lecture.start_time, lecture.end_time)}</span>
          </div>
          <span className="text-[11px] text-text-muted block mt-0.5 font-medium">
            {lecture.day_name}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-3 pt-3 border-t border-border/50 text-xs text-text-muted">
        {lecture.teacher_name && (
          <div className="flex items-center gap-1.5 truncate">
            <User className="w-3.5 h-3.5 shrink-0 opacity-70" />
            <span className="truncate">Faculty: {lecture.teacher_name}</span>
          </div>
        )}
        {lecture.room && (
          <div className="flex items-center gap-1.5 truncate">
            <DoorOpen className="w-3.5 h-3.5 shrink-0 opacity-70" />
            <span className="truncate">Room: {lecture.room}</span>
          </div>
        )}
        {lecture.note && (
          <div className="sm:col-span-2 text-xs italic text-text-muted">
            Note: {lecture.note}
          </div>
        )}
      </div>
    </div>
  );

  return (
    <Card className={className}>
      <CardHeader className="pb-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="p-2 bg-primary/10 rounded-lg text-primary">
              <Sparkles className="w-5 h-5" />
            </div>
            <div>
              <CardTitle className="text-lg">Next Lecture</CardTitle>
              <p className="text-xs text-text-muted mt-0.5">
                Deterministic real-time schedule lookup
              </p>
            </div>
          </div>

          {isAdmin && classes.length > 0 && (
            <div className="w-full sm:w-56">
              <Select
                value={selectedClassId}
                onChange={(e) => setSelectedClassId(e.target.value)}
                options={[
                  { value: "", label: "All Classes" },
                  ...classes.map((c) => ({ value: c.id, label: c.name })),
                ]}
                className="text-xs"
              />
            </div>
          )}
        </div>
      </CardHeader>

      <CardContent>
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-6 w-36" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <div className="p-4 rounded-lg bg-danger/10 border border-danger/20 text-xs text-danger flex items-center gap-2">
            <AlertCircle className="w-4 h-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : !data?.has_timetable ? (
          <div className="p-6 text-center border border-dashed border-border rounded-lg bg-surface/50 space-y-2">
            <Calendar className="w-8 h-8 text-text-muted mx-auto opacity-50" />
            <h5 className="text-sm font-semibold text-text">No Published Timetable</h5>
            <p className="text-xs text-text-muted max-w-sm mx-auto">
              No official timetable has been published yet for this cohort. Once published by administration, your schedule will appear here.
            </p>
          </div>
        ) : !data.current_lecture && !data.next_lecture ? (
          <div className="p-6 text-center border border-dashed border-border rounded-lg bg-surface/50 space-y-2">
            <Clock className="w-8 h-8 text-primary mx-auto opacity-60" />
            <h5 className="text-sm font-semibold text-text">All Lectures Completed Today</h5>
            <p className="text-xs text-text-muted max-w-sm mx-auto">
              No upcoming lectures scheduled for today or subsequent days this week.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {data.current_lecture && renderLectureItem(data.current_lecture, true)}
            {data.next_lecture && renderLectureItem(data.next_lecture, false)}
          </div>
        )}
      </CardContent>
    </Card>
  );
};
