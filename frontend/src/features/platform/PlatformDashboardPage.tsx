import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { platformApi, TenantCreatePayload } from "@/api/platform";
import { Tenant } from "@/api/types";
import {
  Button,
  Input,
  Select,
  Modal,
  Badge,
  Table,
  Column,
  Alert,
  ThemeToggle,
} from "@/components/ui";
import { Plus, UserPlus, LogOut, ShieldCheck } from "lucide-react";

const tenantSchema = z.object({
  slug: z
    .string()
    .min(3, "Must be at least 3 characters")
    .max(63)
    .regex(/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/, "Must be lowercase letters, numbers, or hyphens"),
  name: z.string().min(2, "Name is required"),
  kind: z.enum(["school", "college", "institute", "class", "company", "other"]),
  registration_mode: z.enum(["open", "approval", "invite_only"]),
});

const adminSchema = z.object({
  email: z.string().email("Valid email required"),
  full_name: z.string().min(2, "Full name required"),
});

export const PlatformDashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [isCreateTenantOpen, setIsCreateTenantOpen] = useState(false);
  const [selectedTenantForAdmin, setSelectedTenantForAdmin] = useState<Tenant | null>(null);
  const [actionSuccessMsg, setActionSuccessMsg] = useState<string | null>(null);

  // Load tenants
  const { data, isLoading } = useQuery({
    queryKey: ["platformTenants"],
    queryFn: () => platformApi.listTenants({ limit: 100 }),
  });

  // Create tenant mutation
  const createTenantMutation = useMutation({
    mutationFn: (payload: TenantCreatePayload) => platformApi.createTenant(payload),
    onSuccess: (newTenant) => {
      queryClient.invalidateQueries({ queryKey: ["platformTenants"] });
      setIsCreateTenantOpen(false);
      setActionSuccessMsg(`Tenant "${newTenant.name}" created successfully.`);
    },
  });

  // Create admin mutation
  const createAdminMutation = useMutation({
    mutationFn: ({
      tenantId,
      email,
      fullName,
    }: {
      tenantId: string;
      email: string;
      fullName: string;
    }) =>
      platformApi.createTenantAdmin(tenantId, {
        email,
        full_name: fullName,
      }),
    onSuccess: (res) => {
      setSelectedTenantForAdmin(null);
      setActionSuccessMsg(res.message);
    },
  });

  // Update tenant status mutation
  const updateTenantMutation = useMutation({
    mutationFn: ({ tenantId, status }: { tenantId: string; status: "active" | "suspended" }) =>
      platformApi.updateTenant(tenantId, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["platformTenants"] });
    },
  });

  const tenantForm = useForm<z.infer<typeof tenantSchema>>({
    resolver: zodResolver(tenantSchema),
    defaultValues: {
      slug: "",
      name: "",
      kind: "college",
      registration_mode: "invite_only",
    },
  });

  const adminForm = useForm<z.infer<typeof adminSchema>>({
    resolver: zodResolver(adminSchema),
    defaultValues: {
      email: "",
      full_name: "",
    },
  });

  const handleCreateTenant = (values: z.infer<typeof tenantSchema>) => {
    createTenantMutation.mutate(values);
  };

  const handleCreateAdmin = (values: z.infer<typeof adminSchema>) => {
    if (selectedTenantForAdmin) {
      createAdminMutation.mutate({
        tenantId: selectedTenantForAdmin.id,
        email: values.email,
        fullName: values.full_name,
      });
    }
  };

  const handleLogout = () => {
    platformApi.logout();
    navigate("/platform/login");
  };

  const columns: Column<Tenant>[] = [
    {
      key: "name",
      header: "Organization",
      render: (t) => (
        <div>
          <div className="font-semibold text-text">{t.name}</div>
          <div className="text-xs text-text-muted break-all" title={`${t.slug}.platform.local`}>{t.slug}.platform.local</div>
        </div>
      ),
    },
    {
      key: "kind",
      header: "Kind",
      render: (t) => (
        <Badge variant="secondary" size="sm">
          {t.kind}
        </Badge>
      ),
    },
    {
      key: "registration_mode",
      header: "Registration Mode",
      render: (t) => (
        <span className="text-xs font-medium text-text-muted capitalize">
          {t.registration_mode.replace("_", " ")}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (t) => {
        const variants: Record<string, "success" | "warning" | "danger"> = {
          active: "success",
          suspended: "warning",
          archived: "danger",
        };
        return <Badge variant={variants[t.status] || "secondary"}>{t.status}</Badge>;
      },
    },
    {
      key: "actions",
      header: "Actions",
      className: "text-right whitespace-nowrap w-52",
      render: (t) => (
        <div className="flex items-center justify-end gap-2 flex-wrap sm:flex-nowrap w-full">
          <Button
            size="sm"
            variant="outline"
            leftIcon={<UserPlus className="h-3.5 w-3.5" />}
            className="whitespace-nowrap px-2.5 sm:px-3 text-xs"
            onClick={() => setSelectedTenantForAdmin(t)}
          >
            Create Admin
          </Button>

          {t.status === "active" ? (
            <Button
              size="sm"
              variant="ghost"
              className="whitespace-nowrap px-2.5 sm:px-3 text-xs text-text-muted hover:text-text"
              onClick={() =>
                updateTenantMutation.mutate({
                  tenantId: t.id,
                  status: "suspended",
                })
              }
            >
              Suspend
            </Button>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              className="whitespace-nowrap px-2.5 sm:px-3 text-xs text-primary hover:text-primary-hover"
              onClick={() =>
                updateTenantMutation.mutate({
                  tenantId: t.id,
                  status: "active",
                })
              }
            >
              Activate
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="min-h-dvh bg-background">
      {/* Top Navbar */}
      <header className="border-b border-border bg-surface px-4 sm:px-6 py-3.5 sm:py-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="p-2 bg-primary-light rounded-lg text-primary shrink-0">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-lg font-bold text-text truncate">Platform Superadmin</h1>
            <p className="text-xs text-text-muted">Global Tenant Management</p>
          </div>
        </div>

        <div className="flex items-center gap-2.5 sm:gap-3 w-full sm:w-auto justify-end flex-wrap">
          <ThemeToggle />
          <Button
            onClick={() => setIsCreateTenantOpen(true)}
            leftIcon={<Plus className="w-4 h-4" />}
            className="flex-1 sm:flex-initial"
          >
            Create Tenant
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={handleLogout}
            className="text-text-muted hover:text-danger hover:bg-danger/10 min-w-[44px] min-h-[44px] sm:min-w-[36px] sm:min-h-[36px] p-2 flex items-center justify-center shrink-0"
            title="Log Out"
            aria-label="Log Out"
          >
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </header>

          {/* Main Body */}
          <main className="max-w-7xl mx-auto p-4 sm:p-6 space-y-6">
            {actionSuccessMsg && (
              <Alert variant="success" title="Success">
                {actionSuccessMsg}
              </Alert>
            )}

            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-base font-semibold text-text">
                  Provisioned Tenants ({data?.total || 0})
                </h2>
              </div>

              <Table<Tenant>
                columns={columns}
                data={data?.items || []}
                keyExtractor={(t) => t.id}
                isLoading={isLoading}
                emptyMessage="No tenants created yet. Click 'Create Tenant' to provision one."
              />
            </div>
          </main>

          {/* Create Tenant Modal */}
          <Modal
            isOpen={isCreateTenantOpen}
            onClose={() => setIsCreateTenantOpen(false)}
            title="Provision New Tenant"
            description="Create an organization workspace on the platform"
          >
            <form
              onSubmit={tenantForm.handleSubmit(handleCreateTenant)}
              className="space-y-4"
            >
              <Input
                label="Organization Name"
                placeholder="e.g. Oxford Institute"
                error={tenantForm.formState.errors.name?.message}
                {...tenantForm.register("name")}
              />

              <Input
                label="Subdomain Slug"
                placeholder="oxford"
                helperText="Used as {slug}.platform.local subdomain"
                error={tenantForm.formState.errors.slug?.message}
                {...tenantForm.register("slug")}
              />

              <Select
                label="Organization Kind"
                options={[
                  { value: "college", label: "College" },
                  { value: "school", label: "School" },
                  { value: "institute", label: "Institute" },
                  { value: "class", label: "Coaching / Class" },
                  { value: "company", label: "Company" },
                  { value: "other", label: "Other" },
                ]}
                error={tenantForm.formState.errors.kind?.message}
                {...tenantForm.register("kind")}
              />

              <Select
                label="Registration Mode"
                options={[
                  { value: "invite_only", label: "Invite Only (Recommended)" },
                  { value: "approval", label: "Approval Required" },
                  { value: "open", label: "Open Self-Registration" },
                ]}
                error={tenantForm.formState.errors.registration_mode?.message}
                {...tenantForm.register("registration_mode")}
              />

              <div className="flex justify-end gap-2 pt-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setIsCreateTenantOpen(false)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  isLoading={createTenantMutation.isPending}
                >
                  Create Tenant
                </Button>
              </div>
            </form>
          </Modal>

          {/* Create Tenant Admin Modal */}
          <Modal
            isOpen={Boolean(selectedTenantForAdmin)}
            onClose={() => setSelectedTenantForAdmin(null)}
            title={`Create Admin for ${selectedTenantForAdmin?.name}`}
            description="Creates the first administrator account and emails a temporary password"
          >
            <form
              onSubmit={adminForm.handleSubmit(handleCreateAdmin)}
              className="space-y-4"
            >
              <Input
                label="Admin Full Name"
                placeholder="Prof. John Smith"
                error={adminForm.formState.errors.full_name?.message}
                {...adminForm.register("full_name")}
              />

              <Input
                label="Admin Email"
                type="email"
                placeholder="admin@institution.edu"
                error={adminForm.formState.errors.email?.message}
                {...adminForm.register("email")}
              />

              <div className="flex justify-end gap-2 pt-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setSelectedTenantForAdmin(null)}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  isLoading={createAdminMutation.isPending}
                >
                  Provision Admin
                </Button>
              </div>
            </form>
          </Modal>
        </div>
        );
};

