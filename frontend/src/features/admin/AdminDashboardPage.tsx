import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { adminApi, InviteUserPayload } from "@/api/admin";
import { User, ImportResponse } from "@/api/types";
import { useTheme } from "@/theme/ThemeProvider";
import {
  Button,
  Input,
  Select,
  Modal,
  Badge,
  Table,
  Column,
  Alert,
  FileUpload,
} from "@/components/ui";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Users,
  UserPlus,
  Upload,
  CheckCircle,
  XCircle,
  Settings,
  Search,
} from "lucide-react";

const inviteSchema = z.object({
  email: z.string().email("Please enter a valid email"),
  role_name: z.enum(["student", "teacher", "admin"]),
});

export const AdminDashboardPage: React.FC = () => {
  const { tenantSlug } = useTheme();
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<"users" | "import" | "approvals" | "settings">("users");
  const [searchTerm, setSearchTerm] = useState("");
  const [isInviteOpen, setIsInviteOpen] = useState(false);
  const [selectedCsvFile, setSelectedCsvFile] = useState<File | null>(null);
  const [importResult, setImportResult] = useState<ImportResponse | null>(null);
  const [feedbackMsg, setFeedbackMsg] = useState<{
    type: "success" | "danger";
    text: string;
  } | null>(null);

  // 1. Fetch Users
  const { data: usersData, isLoading: isUsersLoading } = useQuery({
    queryKey: ["adminUsers"],
    queryFn: () => adminApi.listUsers({ limit: 100 }),
  });

  // 2. Fetch Pending Approvals
  const { data: pendingData, isLoading: isPendingLoading } = useQuery({
    queryKey: ["pendingApprovals"],
    queryFn: () => adminApi.listPendingApprovals(),
  });

  // 3. Fetch Registration Mode
  const { data: regModeData } = useQuery({
    queryKey: ["registrationMode"],
    queryFn: () => adminApi.getRegistrationMode(),
  });

  // Invite Mutation
  const inviteMutation = useMutation({
    mutationFn: (payload: InviteUserPayload) => adminApi.inviteUser(payload),
    onSuccess: (res) => {
      setIsInviteOpen(false);
      setFeedbackMsg({ type: "success", text: res.message });
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const msg =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to send invitation.";
      setFeedbackMsg({ type: "danger", text: msg });
    },
  });

  // Deactivate User Mutation
  const deactivateMutation = useMutation({
    mutationFn: (userId: string) => adminApi.deactivateUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      setFeedbackMsg({ type: "success", text: "User deactivated." });
    },
  });

  // Approve User Mutation
  const approveMutation = useMutation({
    mutationFn: (userId: string) => adminApi.approveUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pendingApprovals"] });
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      setFeedbackMsg({ type: "success", text: "User registration approved." });
    },
  });

  // Reject User Mutation
  const rejectMutation = useMutation({
    mutationFn: (userId: string) => adminApi.rejectUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pendingApprovals"] });
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      setFeedbackMsg({ type: "success", text: "User registration rejected." });
    },
  });

  // CSV Import Mutation
  const importMutation = useMutation({
    mutationFn: (file: File) => adminApi.importUsers(file),
    onSuccess: (data) => {
      setImportResult(data);
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      setFeedbackMsg({
        type: "success",
        text: `CSV processed: ${data.succeeded} invited, ${data.failed} failed.`,
      });
    },
    onError: () => {
      setFeedbackMsg({
        type: "danger",
        text: "CSV upload failed. Verify file format and columns.",
      });
    },
  });

  // Registration Mode Mutation
  const setModeMutation = useMutation({
    mutationFn: (mode: "open" | "approval" | "invite_only") =>
      adminApi.setRegistrationMode(mode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["registrationMode"] });
      setFeedbackMsg({
        type: "success",
        text: "Registration mode updated.",
      });
    },
  });

  const inviteForm = useForm<z.infer<typeof inviteSchema>>({
    resolver: zodResolver(inviteSchema),
    defaultValues: {
      email: "",
      role_name: "student",
    },
  });

  const handleInvite = (values: z.infer<typeof inviteSchema>) => {
    inviteMutation.mutate(values);
  };

  const filteredUsers = (usersData?.items || []).filter(
    (u) =>
      u.full_name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      u.email.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const userColumns: Column<User>[] = [
    {
      key: "full_name",
      header: "Name & Email",
      render: (u) => (
        <div>
          <div className="font-semibold text-text flex items-center gap-1.5">
            {u.full_name}
            {u.is_admin && (
              <Badge variant="primary" size="sm">
                Admin
              </Badge>
            )}
          </div>
          <div className="text-xs text-text-muted break-all" title={u.email}>{u.email}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (u) => {
        const variants: Record<string, "success" | "warning" | "danger"> = {
          active: "success",
          pending: "warning",
          deactivated: "danger",
        };
        return <Badge variant={variants[u.status] || "secondary"}>{u.status}</Badge>;
      },
    },
    {
      key: "created_at",
      header: "Joined",
      render: (u) => (
        <span className="text-xs text-text-muted">
          {new Date(u.created_at).toLocaleDateString()}
        </span>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      className: "text-right",
      render: (u) =>
        u.status === "active" && !u.is_admin ? (
          <div className="flex items-center justify-end w-full">
            <Button
              size="sm"
              variant="ghost"
              className="text-danger hover:bg-danger-surface min-h-[44px] sm:min-h-[32px] px-3"
              onClick={() => deactivateMutation.mutate(u.id)}
            >
              Deactivate
            </Button>
          </div>
        ) : null,
    },
  ];

  const tenantTitle = tenantSlug
    ? `${tenantSlug.charAt(0).toUpperCase() + tenantSlug.slice(1)} Administration`
    : "Academic Administration";

  return (
    <AppShell tenantName={tenantSlug ? `${tenantSlug.toUpperCase()} Academic` : "Academic Admin"}>
      <PageHeader
        title={tenantTitle}
        description="Tenant & Academic User Management"
        breadcrumbs={[
          { label: "Admin", href: "/admin" },
          { label: "Overview" },
        ]}
        actions={
          <Button
            onClick={() => setIsInviteOpen(true)}
            leftIcon={<UserPlus className="w-4 h-4" />}
            className="w-full sm:w-auto"
          >
            Invite User
          </Button>
        }
      />

      <div className="space-y-6">
        {feedbackMsg && (
          <Alert variant={feedbackMsg.type} title="Notification">
            {feedbackMsg.text}
          </Alert>
        )}

        {/* Tab Navigation */}
        <div className="w-full overflow-x-auto scrollbar-none pb-0.5">
          <div className="flex border-b border-border space-x-4 sm:space-x-6 text-sm font-medium min-w-max">
          <button
            onClick={() => setActiveTab("users")}
            className={`pb-3 flex items-center gap-2 border-b-2 transition-colors ${
              activeTab === "users"
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <Users className="h-4 w-4" />
            <span>Users Directory ({usersData?.total || 0})</span>
          </button>

          <button
            onClick={() => setActiveTab("import")}
            className={`pb-3 flex items-center gap-2 border-b-2 transition-colors ${
              activeTab === "import"
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <Upload className="h-4 w-4" />
            <span>Bulk CSV Import</span>
          </button>

          <button
            onClick={() => setActiveTab("approvals")}
            className={`pb-3 flex items-center gap-2 border-b-2 transition-colors ${
              activeTab === "approvals"
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <CheckCircle className="h-4 w-4" />
            <span>
              Pending Approvals ({pendingData?.items.length || 0})
            </span>
          </button>

          <button
            onClick={() => setActiveTab("settings")}
            className={`pb-3 flex items-center gap-2 border-b-2 transition-colors ${
              activeTab === "settings"
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <Settings className="h-4 w-4" />
            <span>Registration Mode</span>
          </button>
        </div>
      </div>

        {/* Tab 1: Users Directory */}
        {activeTab === "users" && (
          <div className="space-y-4">
            <div className="flex items-center justify-between gap-4">
              <div className="relative w-full sm:w-72">
                <Search className="absolute left-3 top-2.5 h-4 w-4 text-text-subtle" />
                <input
                  type="text"
                  placeholder="Search by name or email..."
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="w-full pl-9 pr-3 py-2 text-sm bg-surface border border-border rounded focus:outline-none focus:ring-2 focus:ring-primary text-text placeholder:text-text-subtle"
                />
              </div>
            </div>

            <Table<User>
              columns={userColumns}
              data={filteredUsers}
              keyExtractor={(u) => u.id}
              isLoading={isUsersLoading}
              emptyMessage="No users found."
            />
          </div>
        )}

        {/* Tab 2: Bulk CSV Import */}
        {activeTab === "import" && (
          <div className="space-y-6 max-w-2xl bg-surface p-6 rounded-lg border border-border">
            <div>
              <h3 className="text-base font-semibold text-text">
                Bulk Onboard Users via CSV
              </h3>
              <p className="text-xs text-text-muted mt-1">
                Upload a CSV file containing columns: <code>email</code>,{" "}
                <code>full_name</code>, and optional <code>role_name</code> (student / teacher).
              </p>
            </div>

            <FileUpload
              accept=".csv"
              selectedFile={selectedCsvFile}
              onFileSelect={setSelectedCsvFile}
              helperText="Format: email, full_name, role_name"
            />

            <Button
              disabled={!selectedCsvFile}
              isLoading={importMutation.isPending}
              onClick={() => selectedCsvFile && importMutation.mutate(selectedCsvFile)}
            >
              Upload & Process CSV
            </Button>

            {importResult && (
              <div className="pt-4 border-t border-border space-y-3">
                <h4 className="text-sm font-semibold text-text">
                  Import Summary: {importResult.succeeded} succeeded, {importResult.failed} failed
                </h4>

                <div className="max-h-64 overflow-y-auto border border-border rounded divide-y divide-border text-xs">
                  {importResult.rows.map((row) => (
                    <div
                      key={row.row_number}
                      className="p-2.5 flex items-center justify-between bg-surface-muted"
                    >
                      <div className="flex items-center gap-2">
                        {row.success ? (
                          <CheckCircle className="h-4 w-4 text-success" />
                        ) : (
                          <XCircle className="h-4 w-4 text-danger" />
                        )}
                        <span>
                          Row {row.row_number}: <strong>{row.email}</strong> ({row.full_name})
                        </span>
                      </div>
                      <span className={row.success ? "text-success" : "text-danger"}>
                        {row.success ? "Invited" : row.error}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Tab 3: Pending Approvals */}
        {activeTab === "approvals" && (
          <div className="space-y-4">
            <h3 className="text-base font-semibold text-text">
              Registrations Awaiting Approval
            </h3>
            <p className="text-xs text-text-muted">
              Users who signed up while the organization is in 'Approval Required' mode.
            </p>

            <Table<User>
              columns={[
                {
                  key: "full_name",
                  header: "Applicant",
                  render: (u) => (
                    <div>
                      <div className="font-semibold text-text">{u.full_name}</div>
                      <div className="text-xs text-text-muted break-all" title={u.email}>{u.email}</div>
                    </div>
                  ),
                },
                {
                  key: "created_at",
                  header: "Applied On",
                  render: (u) => (
                    <span className="text-xs text-text-muted">
                      {new Date(u.created_at).toLocaleString()}
                    </span>
                  ),
                },
                {
                  key: "actions",
                  header: "Decision",
                  className: "text-right",
                  render: (u) => (
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        size="sm"
                        variant="primary"
                        className="min-h-[44px] sm:min-h-[32px] px-3"
                        onClick={() => approveMutation.mutate(u.id)}
                      >
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="text-danger hover:bg-danger-surface min-h-[44px] sm:min-h-[32px] px-3"
                        onClick={() => rejectMutation.mutate(u.id)}
                      >
                        Reject
                      </Button>
                    </div>
                  ),
                },
              ]}
              data={pendingData?.items || []}
              keyExtractor={(u) => u.id}
              isLoading={isPendingLoading}
              emptyMessage="No pending registrations at this time."
            />
          </div>
        )}

        {/* Tab 4: Settings (Registration Mode) */}
        {activeTab === "settings" && (
          <div className="space-y-6 max-w-lg bg-surface p-6 rounded-lg border border-border">
            <div>
              <h3 className="text-base font-semibold text-text">
                Organization Registration Policy
              </h3>
              <p className="text-xs text-text-muted mt-1">
                Configure how students and faculty join your workspace.
              </p>
            </div>

            <div className="space-y-3">
              {[
                {
                  mode: "invite_only",
                  title: "Invite Only",
                  desc: "Only administrators can invite users. Self-registration is completely disabled.",
                },
                {
                  mode: "approval",
                  title: "Approval Required",
                  desc: "Anyone can self-register, but accounts remain pending until approved by an admin.",
                },
                {
                  mode: "open",
                  title: "Open Self-Registration",
                  desc: "Anyone can register and instantly access the workspace (classroom enrollment still requires class permissions).",
                },
              ].map(({ mode, title, desc }) => (
                <label
                  key={mode}
                  className={`flex items-start gap-3 p-3.5 border rounded-lg cursor-pointer transition-colors ${
                    regModeData?.registration_mode === mode
                      ? "border-primary bg-primary-light/10"
                      : "border-border hover:bg-surface-hover"
                  }`}
                >
                  <input
                    type="radio"
                    name="registration_mode"
                    value={mode}
                    checked={regModeData?.registration_mode === mode}
                    onChange={() =>
                      setModeMutation.mutate(
                        mode as "open" | "approval" | "invite_only"
                      )
                    }
                    className="mt-1 text-primary focus:ring-primary"
                  />
                  <div>
                    <div className="font-semibold text-sm text-text">{title}</div>
                    <div className="text-xs text-text-muted leading-relaxed mt-0.5">
                      {desc}
                    </div>
                  </div>
                </label>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Invite User Modal */}
      <Modal
        isOpen={isInviteOpen}
        onClose={() => setIsInviteOpen(false)}
        title="Invite New User"
        description="Send an invitation email to a student, teacher, or co-administrator"
      >
        <form onSubmit={inviteForm.handleSubmit(handleInvite)} className="space-y-4">
          <Input
            label="Email Address"
            type="email"
            placeholder="colleague@institution.edu"
            error={inviteForm.formState.errors.email?.message}
            {...inviteForm.register("email")}
          />

          <Select
            label="Workspace Role"
            options={[
              { value: "student", label: "Student" },
              { value: "teacher", label: "Teacher / Faculty" },
              { value: "admin", label: "Administrator" },
            ]}
            error={inviteForm.formState.errors.role_name?.message}
            {...inviteForm.register("role_name")}
          />

          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setIsInviteOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit" isLoading={inviteMutation.isPending}>
              Send Invitation
            </Button>
          </div>
        </form>
      </Modal>
    </AppShell>
  );
};

