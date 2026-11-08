import React from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { AuditLogItem } from "../api/auditApi";
import { Clock, Network, Globe } from "lucide-react";

export interface AuditDetailModalProps {
  isOpen: boolean;
  onClose: () => void;
  log: AuditLogItem | null;
}

export const AuditDetailModal: React.FC<AuditDetailModalProps> = ({
  isOpen,
  onClose,
  log,
}) => {
  if (!log) return null;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Audit Event Details"
      description={`Audit Record ID: ${log.id}`}
      size="lg"
    >
      <div className="space-y-4">
        {/* Event Header Card */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 p-4 rounded-lg bg-surface-muted/40 border border-border text-xs">
          <div>
            <span className="font-semibold text-text-muted block">Timestamp</span>
            <div className="flex items-center space-x-1.5 mt-0.5 text-text font-medium">
              <Clock className="w-3.5 h-3.5 text-text-muted" />
              <span>{new Date(log.created_at).toLocaleString()}</span>
            </div>
          </div>

          <div>
            <span className="font-semibold text-text-muted block">Actor Identity</span>
            <span className="font-medium text-text mt-0.5 block truncate">
              {log.actor_email || log.actor_id || "System / Anonymous"}
            </span>
          </div>

          <div>
            <span className="font-semibold text-text-muted block">Action Performed</span>
            <span className="font-mono font-bold text-primary mt-0.5 block">
              {log.action}
            </span>
          </div>

          <div>
            <span className="font-semibold text-text-muted block">Target Resource</span>
            <span className="font-mono text-text mt-0.5 block">
              {log.target_type}: {log.target_id || "N/A"}
            </span>
          </div>

          <div>
            <span className="font-semibold text-text-muted block">IP Address</span>
            <div className="flex items-center space-x-1.5 mt-0.5 text-text">
              <Globe className="w-3.5 h-3.5 text-text-muted" />
              <span className="font-mono">{log.ip_address || "Internal"}</span>
            </div>
          </div>

          <div>
            <span className="font-semibold text-text-muted block">Request Trace ID</span>
            <div className="flex items-center space-x-1.5 mt-0.5 text-text">
              <Network className="w-3.5 h-3.5 text-text-muted" />
              <span className="font-mono truncate">{log.request_id || "N/A"}</span>
            </div>
          </div>
        </div>

        {/* Change Details Payload */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">
              Change Payload Details
            </span>
            <Badge variant="default" size="sm">
              JSON Data
            </Badge>
          </div>

          {log.details ? (
            <pre className="font-mono text-xs bg-surface-muted/80 p-3.5 rounded-lg border border-border text-text overflow-x-auto whitespace-pre-wrap max-h-72">
              {JSON.stringify(log.details, null, 2)}
            </pre>
          ) : (
            <div className="p-4 rounded-lg bg-surface border border-dashed border-border text-center text-xs text-text-muted">
              No additional payload details stored with this event.
            </div>
          )}
        </div>

        <div className="flex justify-end pt-2 border-t border-border">
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </Modal>
  );
};
