import React from "react";
import { PermissionItem } from "../api/rolesApi";
import { Checkbox } from "@/components/ui/Checkbox";
import { Badge } from "@/components/ui/Badge";

export interface PermissionMatrixProps {
  permissions: PermissionItem[];
  selectedPermissions: string[];
  onChange?: (newSelected: string[]) => void;
  readOnly?: boolean;
}

export const PermissionMatrix: React.FC<PermissionMatrixProps> = ({
  permissions,
  selectedPermissions,
  onChange,
  readOnly = false,
}) => {
  // Group permissions by category
  const categories = React.useMemo(() => {
    const map = new Map<string, PermissionItem[]>();
    for (const p of permissions) {
      const cat = p.category || "General";
      if (!map.has(cat)) {
        map.set(cat, []);
      }
      map.get(cat)!.push(p);
    }
    return Array.from(map.entries());
  }, [permissions]);

  const handleToggle = (permId: string) => {
    if (readOnly || !onChange) return;
    if (selectedPermissions.includes(permId)) {
      onChange(selectedPermissions.filter((id) => id !== permId));
    } else {
      onChange([...selectedPermissions, permId]);
    }
  };

  const handleToggleCategory = (catPerms: PermissionItem[]) => {
    if (readOnly || !onChange) return;
    const catIds = catPerms.map((p) => p.id);
    const allSelected = catIds.every((id) => selectedPermissions.includes(id));

    if (allSelected) {
      onChange(selectedPermissions.filter((id) => !catIds.includes(id)));
    } else {
      const set = new Set([...selectedPermissions, ...catIds]);
      onChange(Array.from(set));
    }
  };

  return (
    <div className="space-y-6">
      {categories.map(([category, items]) => {
        const catIds = items.map((p) => p.id);
        const selectedCount = catIds.filter((id) => selectedPermissions.includes(id)).length;
        const allSelected = selectedCount === items.length && items.length > 0;

        return (
          <div
            key={category}
            className="rounded-lg border border-border bg-surface-muted/30 overflow-hidden"
          >
            {/* Category Header */}
            <div className="flex items-center justify-between px-4 py-2.5 bg-surface-muted/60 border-b border-border">
              <div className="flex items-center space-x-2">
                <span className="font-semibold text-xs uppercase tracking-wider text-text">
                  {category}
                </span>
                <Badge variant={selectedCount > 0 ? "primary" : "default"} size="sm">
                  {selectedCount} / {items.length}
                </Badge>
              </div>

              {!readOnly && (
                <button
                  type="button"
                  onClick={() => handleToggleCategory(items)}
                  className="text-xs text-primary hover:text-primary-hover font-medium transition-colors"
                >
                  {allSelected ? "Deselect All" : "Select All"}
                </button>
              )}
            </div>

            {/* Category Items Grid */}
            <div className="p-3 grid grid-cols-1 md:grid-cols-2 gap-3 bg-surface">
              {items.map((perm) => {
                const isChecked = selectedPermissions.includes(perm.id);
                return (
                  <div
                    key={perm.id}
                    className="p-2.5 rounded-md border border-border/60 hover:bg-surface-hover/30 transition-colors"
                  >
                    <Checkbox
                      label={<code className="font-mono text-xs font-semibold text-text">{perm.id}</code>}
                      description={perm.description}
                      checked={isChecked}
                      disabled={readOnly}
                      onChange={() => handleToggle(perm.id)}
                    />
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
};

