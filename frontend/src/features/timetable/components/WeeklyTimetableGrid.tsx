import React, { useState } from "react";
import { TimetableEntryItem } from "../api/timetableApi";
import {
  WEEKDAYS,
  formatTimeRange,
  groupEntriesByDay,
  getPythonWeekday,
} from "../utils/timetableHelpers";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import {
  Clock,
  User,
  DoorOpen,
  Edit2,
  Trash2,
  Plus,
  Calendar,
  FileText,
} from "lucide-react";

export interface WeeklyTimetableGridProps {
  entries: TimetableEntryItem[];
  isLoading?: boolean;
  canEdit?: boolean;
  onAddEntry?: (defaultDay?: number) => void;
  onEditEntry?: (entry: TimetableEntryItem) => void;
  onDeleteEntry?: (entry: TimetableEntryItem) => void;
  className?: string;
}

export const WeeklyTimetableGrid: React.FC<WeeklyTimetableGridProps> = ({
  entries,
  isLoading = false,
  canEdit = false,
  onAddEntry,
  onEditEntry,
  onDeleteEntry,
  className = "",
}) => {
  const currentDayOfWeek = getPythonWeekday();
  const [selectedMobileDay, setSelectedMobileDay] = useState<number>(currentDayOfWeek);

  const grouped = React.useMemo(() => groupEntriesByDay(entries), [entries]);

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="hidden md:grid md:grid-cols-5 lg:grid-cols-7 gap-4">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="p-3 border border-border rounded-lg space-y-3 bg-surface">
              <Skeleton className="h-6 w-24" />
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
            </div>
          ))}
        </div>
        <div className="md:hidden space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="p-6 border border-border rounded-lg bg-surface">
        <EmptyState
          icon={Calendar}
          title="No Scheduled Lectures"
          description={
            canEdit
              ? "This timetable has no sessions scheduled yet. Click '+ Add Lecture' to begin scheduling."
              : "No lectures are scheduled for this cohort."
          }
          action={
            canEdit && onAddEntry ? (
              <Button
                variant="primary"
                onClick={() => onAddEntry(0)}
                className="min-h-[44px]"
              >
                <Plus className="w-4 h-4 mr-2" />
                Add First Lecture
              </Button>
            ) : undefined
          }
        />
      </div>
    );
  }

  return (
    <div className={`space-y-4 ${className}`}>
      {/* ── MOBILE VIEW (< md): Day-by-Day List ──────────────────────────── */}
      <div className="block md:hidden space-y-4">
        {/* Day Selector Tabs (44px touch targets) */}
        <div className="flex items-center gap-1.5 overflow-x-auto pb-2 scrollbar-none -mx-1 px-1">
          {WEEKDAYS.map((day) => {
            const count = grouped[day.index]?.length || 0;
            const isSelected = selectedMobileDay === day.index;
            const isToday = currentDayOfWeek === day.index;

            return (
              <button
                key={day.index}
                type="button"
                onClick={() => setSelectedMobileDay(day.index)}
                className={`flex-1 min-w-[70px] min-h-[44px] px-2.5 py-2 rounded-lg text-xs font-semibold flex flex-col items-center justify-center border transition-colors ${
                  isSelected
                    ? "bg-primary text-primary-foreground border-primary shadow-sm"
                    : isToday
                    ? "bg-surface-hover text-primary border-primary/40"
                    : "bg-surface text-text-muted border-border hover:bg-surface-hover"
                }`}
                aria-pressed={isSelected}
              >
                <span>{day.short}</span>
                <span
                  className={`text-[10px] mt-0.5 px-1.5 py-0.2 rounded-full ${
                    isSelected
                      ? "bg-surface/20 text-primary-foreground"
                      : "bg-surface-hover text-text-muted"
                  }`}
                >
                  {count}
                </span>
              </button>
            );
          })}
        </div>

        {/* Selected Day Header & Add Button */}
        <div className="flex items-center justify-between pt-1">
          <div className="flex items-center gap-2">
            <h4 className="text-sm font-semibold text-text">
              {WEEKDAYS[selectedMobileDay]?.label}
            </h4>
            {currentDayOfWeek === selectedMobileDay && (
              <Badge variant="primary" size="sm">
                Today
              </Badge>
            )}
          </div>
          {canEdit && onAddEntry && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onAddEntry(selectedMobileDay)}
              className="min-h-[44px] text-xs"
            >
              <Plus className="w-3.5 h-3.5 mr-1" />
              Add
            </Button>
          )}
        </div>

        {/* Selected Day Lectures List */}
        {grouped[selectedMobileDay]?.length === 0 ? (
          <div className="p-6 text-center border border-dashed border-border rounded-lg bg-surface/50">
            <Clock className="w-8 h-8 text-text-muted mx-auto mb-2 opacity-50" />
            <p className="text-xs font-medium text-text-muted">
              No lectures scheduled for {WEEKDAYS[selectedMobileDay]?.label}.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {grouped[selectedMobileDay].map((entry) => (
              <div
                key={entry.id}
                className="p-3.5 rounded-lg border border-border bg-surface space-y-2 shadow-sm"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h5 className="text-sm font-semibold text-text leading-snug">
                      {entry.subject_name}
                      {entry.subject_code && (
                        <span className="text-xs font-normal text-text-muted ml-1.5">
                          ({entry.subject_code})
                        </span>
                      )}
                    </h5>
                    <div className="flex items-center gap-1.5 text-xs text-primary font-medium mt-1">
                      <Clock className="w-3.5 h-3.5" />
                      <span>{formatTimeRange(entry.start_time, entry.end_time)}</span>
                    </div>
                  </div>

                  {canEdit && (
                    <div className="flex items-center gap-1 shrink-0">
                      {onEditEntry && (
                        <button
                          type="button"
                          onClick={() => onEditEntry(entry)}
                          aria-label={`Edit ${entry.subject_name}`}
                          className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center rounded-md text-text-muted hover:text-text hover:bg-surface-hover"
                        >
                          <Edit2 className="w-4 h-4" />
                        </button>
                      )}
                      {onDeleteEntry && (
                        <button
                          type="button"
                          onClick={() => onDeleteEntry(entry)}
                          aria-label={`Delete ${entry.subject_name}`}
                          className="min-h-[44px] min-w-[44px] inline-flex items-center justify-center rounded-md text-danger hover:bg-danger/10"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  )}
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5 pt-1 border-t border-border/50 text-xs text-text-muted">
                  {entry.teacher_name && (
                    <div className="flex items-center gap-1.5">
                      <User className="w-3.5 h-3.5 shrink-0 opacity-70" />
                      <span className="truncate">{entry.teacher_name}</span>
                    </div>
                  )}
                  {entry.room && (
                    <div className="flex items-center gap-1.5">
                      <DoorOpen className="w-3.5 h-3.5 shrink-0 opacity-70" />
                      <span className="truncate">{entry.room}</span>
                    </div>
                  )}
                  {entry.note && (
                    <div className="flex items-center gap-1.5 sm:col-span-2">
                      <FileText className="w-3.5 h-3.5 shrink-0 opacity-70" />
                      <span className="truncate italic">{entry.note}</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── DESKTOP & TABLET VIEW (md and up): Weekly Grid ──────────────── */}
      <div className="hidden md:block overflow-x-auto pb-2">
        <div className="grid grid-cols-5 lg:grid-cols-7 gap-3 min-w-[760px]">
          {WEEKDAYS.map((day) => {
            const dayEntries = grouped[day.index] || [];
            const isToday = currentDayOfWeek === day.index;

            return (
              <div
                key={day.index}
                className={`flex flex-col rounded-lg border bg-surface transition-colors ${
                  isToday ? "border-primary/60 shadow-sm" : "border-border"
                }`}
              >
                {/* Column Day Header */}
                <div
                  className={`p-3 border-b flex items-center justify-between ${
                    isToday
                      ? "bg-primary/10 border-primary/30"
                      : "bg-surface-hover/50 border-border"
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-semibold text-text">{day.label}</span>
                    {isToday && (
                      <span className="w-1.5 h-1.5 rounded-full bg-primary" title="Today" />
                    )}
                  </div>
                  {canEdit && onAddEntry && (
                    <button
                      type="button"
                      onClick={() => onAddEntry(day.index)}
                      title={`Add lecture on ${day.label}`}
                      aria-label={`Add lecture on ${day.label}`}
                      className="p-1 rounded text-text-muted hover:text-primary hover:bg-surface"
                    >
                      <Plus className="w-4 h-4" />
                    </button>
                  )}
                </div>

                {/* Day Entries List */}
                <div className="p-2 space-y-2 flex-1 min-h-[140px]">
                  {dayEntries.length === 0 ? (
                    <div className="h-full flex items-center justify-center p-3 text-center">
                      <span className="text-xs text-text-muted italic opacity-60">
                        No lectures
                      </span>
                    </div>
                  ) : (
                    dayEntries.map((entry) => (
                      <div
                        key={entry.id}
                        className="p-2.5 rounded-md border border-border/80 bg-surface-hover/30 hover:bg-surface-hover transition-colors group relative space-y-1.5"
                      >
                        <div className="flex items-start justify-between gap-1">
                          <p className="text-xs font-semibold text-text leading-tight line-clamp-2">
                            {entry.subject_name}
                          </p>
                          {canEdit && (
                            <div className="flex items-center gap-0.5 opacity-80 group-hover:opacity-100 transition-opacity">
                              {onEditEntry && (
                                <button
                                  type="button"
                                  onClick={() => onEditEntry(entry)}
                                  title="Edit session"
                                  aria-label={`Edit ${entry.subject_name}`}
                                  className="p-1 rounded hover:bg-surface text-text-muted hover:text-text"
                                >
                                  <Edit2 className="w-3 h-3" />
                                </button>
                              )}
                              {onDeleteEntry && (
                                <button
                                  type="button"
                                  onClick={() => onDeleteEntry(entry)}
                                  title="Delete session"
                                  aria-label={`Delete ${entry.subject_name}`}
                                  className="p-1 rounded hover:bg-danger/10 text-danger"
                                >
                                  <Trash2 className="w-3 h-3" />
                                </button>
                              )}
                            </div>
                          )}
                        </div>

                        <div className="flex items-center gap-1 text-[11px] font-medium text-primary">
                          <Clock className="w-3 h-3" />
                          <span>{formatTimeRange(entry.start_time, entry.end_time)}</span>
                        </div>

                        {(entry.teacher_name || entry.room) && (
                          <div className="pt-1 border-t border-border/40 space-y-0.5 text-[11px] text-text-muted">
                            {entry.teacher_name && (
                              <div className="flex items-center gap-1 truncate" title={entry.teacher_name}>
                                <User className="w-3 h-3 shrink-0 opacity-70" />
                                <span className="truncate">{entry.teacher_name}</span>
                              </div>
                            )}
                            {entry.room && (
                              <div className="flex items-center gap-1 truncate" title={entry.room}>
                                <DoorOpen className="w-3 h-3 shrink-0 opacity-70" />
                                <span className="truncate">{entry.room}</span>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

