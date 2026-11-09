import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";

// In-memory token storage (XSS safe; refresh token is in HttpOnly cookie)
let accessToken: string | null = null;
let platformToken: string | null = null;

export const setAccessToken = (token: string | null) => {
  accessToken = token;
};

export const getAccessToken = (): string | null => accessToken;

export const setPlatformToken = (token: string | null) => {
  platformToken = token;
  if (token) {
    sessionStorage.setItem("platform_token", token);
  } else {
    sessionStorage.removeItem("platform_token");
  }
};

export const getPlatformToken = (): string | null => {
  if (!platformToken) {
    platformToken = sessionStorage.getItem("platform_token");
  }
  return platformToken;
};

/**
 * Resolves current tenant slug from:
 * 1. Subdomain: {slug}.platform.local
 * 2. URL search param: ?tenant={slug}
 * 3. Local storage cached value
 */
export const getTenantSlug = (): string => {
  const host = window.location.hostname.toLowerCase();
  const parts = host.split(".");
  if (parts.length > 2 && parts[0] !== "www") {
    return parts[0];
  }
  const searchParams = new URLSearchParams(window.location.search);
  const paramSlug = searchParams.get("tenant") || searchParams.get("tenant_slug");
  if (paramSlug) {
    localStorage.setItem("tenant_slug", paramSlug);
    return paramSlug;
  }
  return localStorage.getItem("tenant_slug") || "acme";
};

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "http://localhost:8000/api/v1",
  withCredentials: true, // Sends and receives HttpOnly cookies (refresh_token)
  headers: {
    "X-Requested-With": "XMLHttpRequest",
  },
});
apiClient.defaults.headers.common["X-Requested-With"] = "XMLHttpRequest";

// Request interceptor: attach bearer token and tenant header
apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  config.headers.set("X-Requested-With", "XMLHttpRequest");
  // If request is directed to /platform, use platformToken
  const isPlatform = config.url?.includes("/platform");
  const token = isPlatform ? getPlatformToken() : getAccessToken();

  if (token) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }

  // Attach tenant slug header in development or cross-origin scenarios
  const slug = getTenantSlug();
  if (slug && !isPlatform) {
    config.headers.set("X-Tenant-Slug", slug);
  }

  return config;
});

// Refresh token concurrency queue
let isRefreshing = false;
let failedQueue: Array<{
  resolve: (token: string) => void;
  reject: (error: unknown) => void;
}> = [];

const processQueue = (error: unknown, token: string | null = null) => {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else if (token) {
      prom.resolve(token);
    }
  });
  failedQueue = [];
};

// Response interceptor: auto-refresh access token on 401
apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    if (!error.response || error.response.status !== 401 || !originalRequest) {
      return Promise.reject(error);
    }

    // Do not attempt refresh on auth entry routes
    if (
      originalRequest.url?.includes("/auth/login") ||
      originalRequest.url?.includes("/auth/register") ||
      originalRequest.url?.includes("/auth/refresh") ||
      originalRequest.url?.includes("/platform/auth/login")
    ) {
      return Promise.reject(error);
    }

    if (originalRequest._retry) {
      return Promise.reject(error);
    }

    if (isRefreshing) {
      return new Promise<string>((resolve, reject) => {
        failedQueue.push({ resolve, reject });
      })
        .then((token) => {
          originalRequest.headers.set("Authorization", `Bearer ${token}`);
          return apiClient(originalRequest);
        })
        .catch((err) => Promise.reject(err));
    }

    originalRequest._retry = true;
    isRefreshing = true;

    try {
      const response = await apiClient.post<{ access_token: string }>("/auth/refresh");
      const newToken = response.data.access_token;
      setAccessToken(newToken);
      processQueue(null, newToken);
      originalRequest.headers.set("Authorization", `Bearer ${newToken}`);
      return apiClient(originalRequest);
    } catch (refreshErr) {
      processQueue(refreshErr, null);
      setAccessToken(null);
      // Let AuthContext handle redirecting to login
      return Promise.reject(refreshErr);
    } finally {
      isRefreshing = false;
    }
  }
);

