import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Button,
  Badge,
  Table,
  Column,
  ConfirmDialog,
  Alert,
  Tabs,
  TabList,
  TabTrigger,
  TabContent,
  Input,
} from "@/components/ui";
import { academicApi, ClassItem, SubjectItem } from "./api/academicApi";
import { ClassFormModal } from "./components/ClassFormModal";
import { SubjectFormModal } from "./components/SubjectFormModal";
import { LinkSubjectModal } from "./components/LinkSubjectModal";
import { School, BookOpen, Plus, Edit2, Trash2, Layers, Search } from "lucide-react";

export const AcademicPage: React.FC = () => {
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<string>("classes");
  const [feedback, setFeedback] = useState<{ type: "success" | "danger"; message: string } | null>(null);

  // Search & Pagination
  const [classSearch, setClassSearch] = useState("");
  const [classPage, setClassPage] = useState(1);
  const classPageSize = 10;

  const [subjectSearch, setSubjectSearch] = useState("");
  const [subjectPage, setSubjectPage] = useState(1);
  const subjectPageSize = 10;

  // Modals state
  const [isClassModalOpen, setIsClassModalOpen] = useState(false);
  const [classToEdit, setClassToEdit] = useState<ClassItem | null>(null);

  const [isSubjectModalOpen, setIsSubjectModalOpen] = useState(false);
  const [subjectToEdit, setSubjectToEdit] = useState<SubjectItem | null>(null);

  const [targetClassForSubjects, setTargetClassForSubjects] = useState<ClassItem | null>(null);
  const [isLinkSubjectOpen, setIsLinkSubjectOpen] = useState(false);

  const [classToDelete, setClassToDelete] = useState<ClassItem | null>(null);
  const [isConfirmDeleteClassOpen, setIsConfirmDeleteClassOpen] = useState(false);

  const [subjectToDelete, setSubjectToDelete] = useState<SubjectItem | null>(null);
  const [isConfirmDeleteSubjectOpen, setIsConfirmDeleteSubjectOpen] = useState(false);

  // Queries
  const { data: classesData, isLoading: isClassesLoading } = useQuery({
    queryKey: ["academicClasses", classPage, classSearch],
    queryFn: () =>
      academicApi.listClasses({
        page: classPage,
        page_size: classPageSize,
        search: classSearch || undefined,
      }),
  });

  const { data: subjectsData, isLoading: isSubjectsLoading } = useQuery({
    queryKey: ["academicSubjects", subjectPage, subjectSearch],
    queryFn: () =>
      academicApi.listSubjects({
        page: subjectPage,
        page_size: subjectPageSize,
        search: subjectSearch || undefined,
      }),
  });

  // Client-side filtering when backend search isn't present
  const filteredClasses = (classesData?.items || []).filter((c) =>
    classSearch ? c.name.toLowerCase().includes(classSearch.toLowerCase()) : true
  );

  const filteredSubjects = (subjectsData?.items || []).filter((s) => {
    if (!subjectSearch) return true;
    const q = subjectSearch.toLowerCase();
    return s.name.toLowerCase().includes(q) || (s.code && s.code.toLowerCase().includes(q));
  });

  // Delete Mutations
  const deleteClassMutation = useMutation({
    mutationFn: (classId: string) => academicApi.deleteClass(classId),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["academicClasses"] });
      setFeedback({ type: "success", message: res.message || "Class deleted successfully." });
      setIsConfirmDeleteClassOpen(false);
      setClassToDelete(null);
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to delete class.";
      setFeedback({ type: "danger", message });
      setIsConfirmDeleteClassOpen(false);
    },
  });

  const deleteSubjectMutation = useMutation({
    mutationFn: (subjectId: string) => academicApi.deleteSubject(subjectId),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["academicSubjects"] });
      setFeedback({ type: "success", message: res.message || "Subject deleted successfully." });
      setIsConfirmDeleteSubjectOpen(false);
      setSubjectToDelete(null);
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to delete subject.";
      setFeedback({ type: "danger", message });
      setIsConfirmDeleteSubjectOpen(false);
    },
  });

  const classColumns: Column<ClassItem>[] = [
    {
      key: "name",
      header: "Class / Cohort",
      render: (c) => (
        <div className="flex items-center space-x-2.5">
          <div className="p-2 bg-primary/10 rounded-lg text-primary shrink-0">
            <School className="w-4 h-4" />
          </div>
          <div>
            <div className="font-semibold text-text">{c.name}</div>
            {c.description && <div className="text-xs text-text-muted">{c.description}</div>}
          </div>
        </div>
      ),
    },
    {
      key: "kind",
      header: "Cohort Type",
      render: (c) => (
        <Badge variant="primary" size="sm" className="capitalize">
          {c.kind}
        </Badge>
      ),
    },
    {
      key: "created_at",
      header: "Created",
      render: (c) => (
        <span className="text-xs text-text-muted">
          {new Date(c.created_at).toLocaleDateString()}
        </span>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      className: "text-right",
      render: (c) => (
        <div className="flex items-center justify-end space-x-2">
          <Button
            size="sm"
            variant="outline"
            leftIcon={<Layers className="w-3.5 h-3.5" />}
            onClick={() => {
              setTargetClassForSubjects(c);
              setIsLinkSubjectOpen(true);
            }}
          >
            Subjects
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setClassToEdit(c);
              setIsClassModalOpen(true);
            }}
            title="Edit class"
            aria-label={`Edit ${c.name}`}
          >
            <Edit2 className="w-3.5 h-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="text-danger hover:bg-danger/10"
            onClick={() => {
              setClassToDelete(c);
              setIsConfirmDeleteClassOpen(true);
            }}
            title="Delete class"
            aria-label={`Delete ${c.name}`}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </Button>
        </div>
      ),
    },
  ];

  const subjectColumns: Column<SubjectItem>[] = [
    {
      key: "name",
      header: "Subject Title",
      render: (s) => (
        <div className="flex items-center space-x-2.5">
          <div className="p-2 bg-primary/10 rounded-lg text-primary shrink-0">
            <BookOpen className="w-4 h-4" />
          </div>
          <div>
            <div className="font-semibold text-text">{s.name}</div>
            {s.description && <div className="text-xs text-text-muted">{s.description}</div>}
          </div>
        </div>
      ),
    },
    {
      key: "code",
      header: "Course Code",
      render: (s) => (
        s.code ? (
          <code className="font-mono font-bold text-xs text-text bg-surface-muted px-2 py-0.5 rounded border border-border">
            {s.code}
          </code>
        ) : (
          <span className="text-xs text-text-muted">—</span>
        )
      ),
    },
    {
      key: "created_at",
      header: "Created",
      render: (s) => (
        <span className="text-xs text-text-muted">
          {new Date(s.created_at).toLocaleDateString()}
        </span>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      className: "text-right",
      render: (s) => (
        <div className="flex items-center justify-end space-x-1.5">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setSubjectToEdit(s);
              setIsSubjectModalOpen(true);
            }}
            title="Edit subject"
            aria-label={`Edit ${s.name}`}
          >
            <Edit2 className="w-3.5 h-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="text-danger hover:bg-danger/10"
            onClick={() => {
              setSubjectToDelete(s);
              setIsConfirmDeleteSubjectOpen(true);
            }}
            title="Delete subject"
            aria-label={`Delete ${s.name}`}
          >
            <Trash2 className="w-3.5 h-3.5" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <AppShell>
      <PageHeader
        title="Academic Structure"
        description="Organize educational batches, cohorts, curriculum subjects, and syllabus mappings."
        breadcrumbs={[
          { label: "Admin", href: "/admin" },
          { label: "Classes & Subjects" },
        ]}
        actions={
          activeTab === "classes" ? (
            <Button
              onClick={() => {
                setClassToEdit(null);
                setIsClassModalOpen(true);
              }}
              leftIcon={<Plus className="w-4 h-4" />}
            >
              Add Class
            </Button>
          ) : (
            <Button
              onClick={() => {
                setSubjectToEdit(null);
                setIsSubjectModalOpen(true);
              }}
              leftIcon={<Plus className="w-4 h-4" />}
            >
              Add Subject
            </Button>
          )
        }
      />

      <div className="space-y-6">
        {feedback && (
          <Alert variant={feedback.type} title="Notification" onClose={() => setFeedback(null)}>
            {feedback.message}
          </Alert>
        )}

        <Tabs defaultValue="classes" value={activeTab} onValueChange={setActiveTab}>
          <TabList>
            <TabTrigger value="classes">Classes & Batches ({classesData?.total || 0})</TabTrigger>
            <TabTrigger value="subjects">Subjects Catalog ({subjectsData?.total || 0})</TabTrigger>
          </TabList>

          {/* TAB 1: CLASSES */}
          <TabContent value="classes" className="space-y-4 pt-4">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
              <div className="w-full sm:w-80">
                <Input
                  placeholder="Search classes by name..."
                  value={classSearch}
                  onChange={(e) => {
                    setClassSearch(e.target.value);
                    setClassPage(1);
                  }}
                  leftIcon={<Search className="w-4 h-4 text-text-muted" />}
                />
              </div>
            </div>

            <Table<ClassItem>
              columns={classColumns}
              data={filteredClasses}
              keyExtractor={(c) => c.id}
              isLoading={isClassesLoading}
              emptyTitle="No Classes Configured"
              emptyDescription="Define academic classes or student batches using 'Add Class'."
              pagination={{
                page: classPage,
                pageSize: classPageSize,
                total: classesData?.total || 0,
                onPageChange: setClassPage,
              }}
            />
          </TabContent>

          {/* TAB 2: SUBJECTS */}
          <TabContent value="subjects" className="space-y-4 pt-4">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-3">
              <div className="w-full sm:w-80">
                <Input
                  placeholder="Search subjects by name or code..."
                  value={subjectSearch}
                  onChange={(e) => {
                    setSubjectSearch(e.target.value);
                    setSubjectPage(1);
                  }}
                  leftIcon={<Search className="w-4 h-4 text-text-muted" />}
                />
              </div>
            </div>

            <Table<SubjectItem>
              columns={subjectColumns}
              data={filteredSubjects}
              keyExtractor={(s) => s.id}
              isLoading={isSubjectsLoading}
              emptyTitle="No Subjects Registered"
              emptyDescription="Register educational courses or subjects using 'Add Subject'."
              pagination={{
                page: subjectPage,
                pageSize: subjectPageSize,
                total: subjectsData?.total || 0,
                onPageChange: setSubjectPage,
              }}
            />
          </TabContent>
        </Tabs>
      </div>

      {/* Class Form Modal */}
      <ClassFormModal
        isOpen={isClassModalOpen}
        onClose={() => {
          setIsClassModalOpen(false);
          setClassToEdit(null);
        }}
        classToEdit={classToEdit}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* Subject Form Modal */}
      <SubjectFormModal
        isOpen={isSubjectModalOpen}
        onClose={() => {
          setIsSubjectModalOpen(false);
          setSubjectToEdit(null);
        }}
        subjectToEdit={subjectToEdit}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* Link Subject to Class Modal */}
      <LinkSubjectModal
        isOpen={isLinkSubjectOpen}
        onClose={() => {
          setIsLinkSubjectOpen(false);
          setTargetClassForSubjects(null);
        }}
        targetClass={targetClassForSubjects}
        allSubjects={subjectsData?.items || []}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* Confirm Delete Class */}
      <ConfirmDialog
        isOpen={isConfirmDeleteClassOpen}
        onClose={() => setIsConfirmDeleteClassOpen(false)}
        title="Delete Academic Class"
        message={`Are you sure you want to permanently delete class "${classToDelete?.name}"? All associated rosters and subject mappings will be removed.`}
        confirmText="Delete Class"
        variant="danger"
        isLoading={deleteClassMutation.isPending}
        onConfirm={() => {
          if (classToDelete) {
            deleteClassMutation.mutate(classToDelete.id);
          }
        }}
      />

      {/* Confirm Delete Subject */}
      <ConfirmDialog
        isOpen={isConfirmDeleteSubjectOpen}
        onClose={() => setIsConfirmDeleteSubjectOpen(false)}
        title="Delete Subject"
        message={`Are you sure you want to delete subject "${subjectToDelete?.name}"?`}
        confirmText="Delete Subject"
        variant="danger"
        isLoading={deleteSubjectMutation.isPending}
        onConfirm={() => {
          if (subjectToDelete) {
            deleteSubjectMutation.mutate(subjectToDelete.id);
          }
        }}
      />
    </AppShell>
  );
};
