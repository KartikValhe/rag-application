import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Button,
  Badge,
  Table,
  Column,
  Input,
  Select,
} from "@/components/ui";
import { auditApi, AuditLogItem } from "./api/auditApi";
import { AuditDetailModal } from "./components/AuditDetailModal";
import { Eye, Filter, RotateCcw, Clock, ShieldCheck, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/utils/cn";

export const AuditLogPage: React.FC = () => {
  const [page, setPage] = useState(1);
  const pageSize = 15;

  // Filters
  const [actionFilter, setActionFilter] = useState("");
  const [targetTypeFilter, setTargetTypeFilter] = useState("");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [isMobileFiltersOpen, setIsMobileFiltersOpen] = useState(false);

  const [selectedLog, setSelectedLog] = useState<AuditLogItem | null>(null);
  const [isDetailModalOpen, setIsDetailModalOpen] = useState(false);

  const activeFilterCount = [actionFilter, targetTypeFilter, fromDate, toDate].filter(Boolean).length;

  // Fetch Audit Logs
  const { data, isLoading } = useQuery({
    queryKey: ["auditLogs", page, actionFilter, targetTypeFilter, fromDate, toDate],
    queryFn: () =>
      auditApi.listLogs({
        page,
        page_size: pageSize,
        action: actionFilter || undefined,
        target_type: targetTypeFilter || undefined,
        from_date: fromDate || undefined,
        to_date: toDate || undefined,
      }),
  });

  const handleResetFilters = () => {
    setActionFilter("");
    setTargetTypeFilter("");
    setFromDate("");
    setToDate("");
    setPage(1);
  };

  const getActionBadgeVariant = (action: string): "default" | "primary" | "success" | "danger" | "warning" => {
    if (action.includes("create") || action.includes("add") || action.includes("import")) return "success";
    if (action.includes("delete") || action.includes("remove") || action.includes("deactivate")) return "danger";
    if (action.includes("update") || action.includes("patch")) return "primary";
    return "default";
  };

  const columns: Column<AuditLogItem>[] = [
    {
      key: "created_at",
      header: "Timestamp",
      render: (log) => (
          <div className="flex items-center space-x-1.5 text-xs text-text-muted whitespace-nowrap">
            <Clock className="w-3.5 h-3.5 shrink-0 text-text-muted/80" aria-hidden="true" />
            <span>{new Date(log.created_at).toLocaleString()}</span>
          </div>
          ),
    },
          {
            key: "actor_email",
          header: "Actor",
      render: (log) => (
              <div className="min-w-0">
                <div
                  className="font-semibold text-text text-xs break-all"
                  title={log.actor_email || "System"}
                >
                  {log.actor_email || "System"}
                </div>
                  <div className="text-[11px] font-mono text-text-muted break-all">
                    {log.ip_address || "Internal IP"}
                  </div>
                </div>
                ),
    },
                {
                  key: "action",
                header: "Action",
      render: (log) => (
                <Badge variant={getActionBadgeVariant(log.action)} size="sm">
                  {log.action}
                </Badge>
                ),
    },
                {
                  key: "target_type",
                header: "Target Resource",
      render: (log) => (
                  <div className="min-w-0">
                    <span className="font-medium text-xs text-text capitalize">
                      {log.target_type}
                    </span>
                    {log.target_id && (
                        <div
                          className="text-[10px] font-mono text-text-muted truncate max-w-[140px] sm:max-w-[200px]"
                          title={log.target_id}
                        >
                          {log.target_id}
                        </div>
          )}
                      </div>
                    ),
    },
                    {
                      key: "actions",
                    header: "Details",
                    className: "text-right",
      render: (log) => (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-primary hover:bg-primary/10 min-h-[44px] sm:min-h-[32px] px-3"
                      onClick={() => {
                        setSelectedLog(log);
                        setIsDetailModalOpen(true);
                      }}
                      leftIcon={<Eye className="w-3.5 h-3.5" />}
                    >
                      Inspect
                    </Button>
                    ),
    },
                    ];

                    return (
                    <AppShell>
                      <PageHeader
                        title="Audit Logs"
                        description="Immutable administrative activity stream. Read-only audit trail with IP address and request tracking."
                        breadcrumbs={[
                          { label: "Admin", href: "/admin" },
                          { label: "Audit Logs" },
                        ]}
                        actions={
                          <div className="flex items-center space-x-2">
                            <Badge variant="primary" size="md">
                              <ShieldCheck className="w-3.5 h-3.5 mr-1" />
                              Append-Only & RLS Enforced
                            </Badge>
                          </div>
                        }
                      />

                      <div className="space-y-6">
                        {/* Filters Panel */}
                        <div className="p-4 rounded-xl border border-border bg-surface shadow-xs space-y-4">
                                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2.5">
                                  <button
                                    type="button"
                                    onClick={() => setIsMobileFiltersOpen((prev) => !prev)}
                                    className="flex items-center justify-between sm:justify-start space-x-2 text-left cursor-pointer md:cursor-default"
                                    aria-expanded={isMobileFiltersOpen}
                                  >
                                    <div className="flex items-center space-x-2">
                                      <Filter className="w-4 h-4 text-primary" aria-hidden="true" />
                                      <span className="text-xs font-semibold text-text uppercase tracking-wider">
                                        Filter Activity
                                      </span>
                                      {activeFilterCount > 0 && (
                                        <Badge variant="primary" size="sm">
                                          {activeFilterCount} active
                                        </Badge>
                                      )}
                                    </div>
                                    <span className="md:hidden text-text-muted">
                                      {isMobileFiltersOpen ? (
                                        <ChevronUp className="w-4 h-4" />
                                      ) : (
                                        <ChevronDown className="w-4 h-4" />
                                      )}
                                    </span>
                              </button>

                                { activeFilterCount > 0 && (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={handleResetFilters}
                                    leftIcon={<RotateCcw className="w-3.5 h-3.5" />}
                                    className="text-xs text-text-muted hover:text-text self-start sm:self-auto min-h-[44px] sm:min-h-[32px]"
                                  >
                                    Reset Filters
                                  </Button>
                                )}
                            </div>

                              <div
                                className={cn(
                                  "grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3",
                                  isMobileFiltersOpen ? "grid" : "hidden md:grid"
                                )}
                              >
                                <Input
                                  placeholder="Filter by action (e.g. role:create)"
                                  value={actionFilter}
                                  onChange={(e) => {
                                    setActionFilter(e.target.value);
                                    setPage(1);
                                  }}
                                />

                                <Select
                                  options={[
                                    { value: "", label: "All Target Types" },
                                    { value: "role", label: "Roles" },
                                    { value: "user_role", label: "User Role Assignments" },
                                    { value: "class", label: "Classes" },
                                    { value: "subject", label: "Subjects" },
                                    { value: "class_subject", label: "Class-Subject Links" },
                                    { value: "enrollment", label: "Enrollments" },
                                    { value: "teacher_assignment", label: "Teacher Assignments" },
                                    { value: "user", label: "Users" },
                                  ]}
                                  value={targetTypeFilter}
                                  onChange={(e) => {
                                    setTargetTypeFilter(e.target.value);
                                    setPage(1);
                                  }}
                                />

                                <div>
                                  <input
                                    type="date"
                                    value={fromDate}
                                    onChange={(e) => {
                                      setFromDate(e.target.value);
                                      setPage(1);
                                    }}
                                    className="input-base text-xs w-full min-h-[44px] sm:min-h-[38px]"
                                    title="From date"
                                    aria-label="From date"
                                  />
                                </div>

                                <div>
                                  <input
                                    type="date"
                                    value={toDate}
                                    onChange={(e) => {
                                      setToDate(e.target.value);
                                      setPage(1);
                                    }}
                                    className="input-base text-xs w-full min-h-[44px] sm:min-h-[38px]"
                                    title="To date"
                                    aria-label="To date"
                                  />
                                </div>
                              </div>
                            </div>

                            {/* Audit Logs Table */}
                            <Table<AuditLogItem>
                              columns={columns}
                              data={data?.items || []}
                              keyExtractor={(l) => l.id}
                              isLoading={isLoading}
                              emptyTitle="No Audit Logs Found"
                              emptyDescription="No administrative actions matching the selected filter criteria have been recorded yet."
                              pagination={{
                                page,
                                pageSize,
                                total: data?.total || 0,
                                onPageChange: setPage,
                              }}
                            />
                          </div>

                          {/* Audit Detail Modal */}
                          <AuditDetailModal
                            isOpen={isDetailModalOpen}
                            onClose={() => {
                              setIsDetailModalOpen(false);
                              setSelectedLog(null);
                            }}
                            log={selectedLog}
                          />
                        </AppShell>
                        );
};
