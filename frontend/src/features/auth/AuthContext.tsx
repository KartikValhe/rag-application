import React, { createContext, useContext, useEffect, useState } from "react";
import { authApi, LoginPayload, RegisterPayload } from "@/api/auth";
import { getAccessToken, setAccessToken } from "@/api/client";

export interface AuthUser {
  id: string;
  isAdmin: boolean;
  tenantId?: string;
  email?: string;
  roles?: string[];
  permissions?: string[];
}

export interface AuthContextType {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (payload: LoginPayload) => Promise<{ access_token: string; user_id: string; is_admin: boolean; roles?: string[] }>;
  register: (payload: RegisterPayload) => Promise<{ status: string; message: string }>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);

  // Initialize session on boot via rotating refresh token (stored in HttpOnly cookie)
  useEffect(() => {
    const initAuth = async () => {
      try {
        const data = await authApi.refresh();
        if (data.access_token) {
          // Decode payload claims safely
          const parts = data.access_token.split(".");
          if (parts.length === 3) {
            const payload = JSON.parse(atob(parts[1]));
            setUser({
              id: payload.sub,
              isAdmin: Boolean(payload.is_admin),
              tenantId: payload.tenant_id,
              email: payload.email,
              roles: payload.roles || (payload.is_admin ? ["tenant_admin"] : []),
              permissions: payload.permissions || (payload.is_admin ? ["*"] : []),
            });
          }
        }
      } catch {
        // Not logged in or expired refresh token; user stays null
        setAccessToken(null);
        setUser(null);
      } finally {
        setIsLoading(false);
      }
    };

    initAuth();
  }, []);

  const login = async (payload: LoginPayload) => {
    setIsLoading(true);
    try {
      const res = await authApi.login(payload);
      let tenantId: string | undefined;
      let email: string | undefined;
      let roles: string[] | undefined;
      let permissions: string[] | undefined;

      if (res.access_token) {
        const parts = res.access_token.split(".");
        if (parts.length === 3) {
          try {
            const tokenPayload = JSON.parse(atob(parts[1]));
            tenantId = tokenPayload.tenant_id;
            email = tokenPayload.email;
            roles = tokenPayload.roles;
            permissions = tokenPayload.permissions;
          } catch {
            // ignore decode error
          }
        }
      }

      const resolvedRoles = roles || (res.is_admin ? ["tenant_admin"] : []);
      setUser({
        id: res.user_id,
        isAdmin: res.is_admin,
        tenantId,
        email: email || payload.email,
        roles: resolvedRoles,
        permissions: permissions || (res.is_admin ? ["*"] : []),
      });
      return {
        ...res,
        roles: resolvedRoles,
      };
    } finally {
      setIsLoading(false);
    }
  };

  const register = async (payload: RegisterPayload) => {
    const res = await authApi.register(payload);
    return { status: res.status, message: res.message };
  };

  const logout = async () => {
    setIsLoading(true);
    try {
      await authApi.logout();
    } finally {
      setUser(null);
      setIsLoading(false);
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: Boolean(user && getAccessToken()),
        isLoading,
        login,
        register,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
};

