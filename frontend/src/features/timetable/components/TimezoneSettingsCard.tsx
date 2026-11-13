import React, { useEffect, useState } from "react";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/Card";
import { Select } from "@/components/ui/Select";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { useToast } from "@/components/ui/Toast";
import { Can } from "@/components/auth/Can";
import { getFriendlyError, isForbidden } from "@/api/errorMessages";
import { timetableApi } from "../api/timetableApi";
import { Globe, Clock, Check } from "lucide-react";

const COMMON_TIMEZONES = [
  { value: "Asia/Kolkata", label: "Asia/Kolkata (IST, UTC+5:30)" },
  { value: "UTC", label: "UTC (Coordinated Universal Time, UTC+0:00)" },
  { value: "Europe/London", label: "Europe/London (GMT/BST, UTC+0/+1)" },
  { value: "Europe/Paris", label: "Europe/Paris (CET/CEST, UTC+1/+2)" },
  { value: "Europe/Berlin", label: "Europe/Berlin (CET/CEST, UTC+1/+2)" },
  { value: "Asia/Dubai", label: "Asia/Dubai (GST, UTC+4:00)" },
  { value: "Asia/Singapore", label: "Asia/Singapore (SGT, UTC+8:00)" },
  { value: "Asia/Tokyo", label: "Asia/Tokyo (JST, UTC+9:00)" },
  { value: "Australia/Sydney", label: "Australia/Sydney (AEST/AEDT, UTC+10/+11)" },
  { value: "America/New_York", label: "America/New_York (EST/EDT, UTC-5/-4)" },
  { value: "America/Chicago", label: "America/Chicago (CST/CDT, UTC-6/-5)" },
  { value: "America/Denver", label: "America/Denver (MST/MDT, UTC-7/-6)" },
  { value: "America/Los_Angeles", label: "America/Los_Angeles (PST/PDT, UTC-8/-7)" },
];

const TimezoneSettingsCardInner: React.FC = () => {
  const toast = useToast();
  const [currentTimezone, setCurrentTimezone] = useState<string>("Asia/Kolkata");
  const [selectedTimezone, setSelectedTimezone] = useState<string>("Asia/Kolkata");
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;
    setIsLoading(true);
    timetableApi
      .getTimezone()
      .then((res) => {
        if (!isMounted) return;
        setCurrentTimezone(res.timezone || "Asia/Kolkata");
        setSelectedTimezone(res.timezone || "Asia/Kolkata");
      })
      .catch((err) => {
        if (!isMounted) return;
        setError(
          isForbidden(err)
            ? "You do not have permission to view or change the organization timezone."
            : getFriendlyError(err, "Failed to load the timezone setting.")
        );
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, []);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsSaving(true);

    try {
      const res = await timetableApi.updateTimezone(selectedTimezone);
      setCurrentTimezone(res.timezone);
      setSelectedTimezone(res.timezone);
      toast.success("Tenant timezone successfully updated.");
    } catch (err: unknown) {
      const msg = isForbidden(err)
        ? "You do not have permission to change the organization timezone."
        : getFriendlyError(err, "Failed to update timezone.");
      setError(msg);
      toast.error(msg);
    } finally {
      setIsSaving(false);
    }
  };

  const hasChanged = selectedTimezone !== currentTimezone;

  return (
    <Card className="border border-border bg-surface">
      <CardHeader>
        <div className="flex items-center gap-2.5">
          <div className="p-2 bg-primary/10 rounded-lg text-primary">
            <Globe className="w-5 h-5" />
          </div>
          <div>
            <CardTitle className="text-base font-semibold text-text">
              Tenant Academic Timezone
            </CardTitle>
            <CardDescription className="text-xs text-text-muted mt-0.5">
              Determines real-time lecture intelligence (&ldquo;Next Lecture&rdquo;) and schedule calculations.
            </CardDescription>
          </div>
        </div>
      </CardHeader>

      <CardContent>
        {error && (
          <div className="mb-4">
            <Alert variant="danger" title="Error">
              {error}
            </Alert>
          </div>
        )}

        <form onSubmit={handleSave} className="space-y-4 max-w-lg">
          <div>
            <Select
              label="Select Standard IANA Timezone"
              value={selectedTimezone}
              onChange={(e) => setSelectedTimezone(e.target.value)}
              options={COMMON_TIMEZONES}
              disabled={isLoading || isSaving}
            />
            <p className="text-xs text-text-muted mt-1.5 flex items-center gap-1.5">
              <Clock className="w-3.5 h-3.5 opacity-70" />
              <span>Current saved timezone: <strong className="text-text font-medium">{currentTimezone}</strong></span>
            </p>
          </div>

          <div className="pt-1">
            <Button
              type="submit"
              variant="primary"
              disabled={!hasChanged || isSaving || isLoading}
              isLoading={isSaving}
              className="min-h-[44px]"
            >
              <Check className="w-4 h-4 mr-1.5" />
              Save Timezone Setting
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
};

export const TimezoneSettingsCard: React.FC = () => (
  <Can permission="settings:manage">
    <TimezoneSettingsCardInner />
  </Can>
);
