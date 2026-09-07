import { useQuery } from "@tanstack/react-query";
import type { components } from "./generated/schema";

type Schemas = components["schemas"];
export type Project = Schemas["Project"];
export type Workspace = Schemas["Workspace"];
export type ReadModels = {
  projects: Schemas["Projects"];
  overview: Schemas["Overview"];
  workspace: Workspace;
  code: Schemas["Code"];
  tasks: Schemas["Tasks"];
  task: Schemas["Task"];
  review: Schemas["Review"];
  settings: Schemas["Settings"];
  session: Schemas["Session"];
};

const storageKey = "reposteward-local-session";
let token = "";

export function legacyRoute(hash: string): string | undefined {
  const match = /^(today|projects|tasks|review|settings)(?:\/([0-9a-f]{32})(?:\/([0-9a-f]{32}))?)?$/.exec(hash.slice(1));
  if (!match) return;
  const [, view, project, item] = match;
  if (view === "today") return "/";
  if (view === "settings") return "/settings";
  if (!project) return view === "projects" ? "/projects" : "/tasks";
  if (view === "projects") return `/projects/${project}${item ? `/workspaces/${item}` : ""}`;
  return `/projects/${project}/tasks${item ? `/${item}${view === "review" ? "/review" : ""}` : ""}`;
}

export function connectSession() {
  try {
    token = sessionStorage.getItem(storageKey) || "";
  } catch {
    /* memory fallback */
  }
  if (location.hash.startsWith("#session=")) {
    const candidate = location.hash.slice(9);
    if (/^[A-Za-z0-9_-]{43}$/.test(candidate)) {
      token = candidate;
      try {
        sessionStorage.setItem(storageKey, token);
      } catch {
        /* memory fallback */
      }
    }
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    const route = legacyRoute(location.hash);
    if (route) history.replaceState(null, "", route);
  }
  return Boolean(token);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export async function read<K extends keyof ReadModels>(
  name: K,
  params: Record<string, string> = {},
  signal?: AbortSignal,
): Promise<ReadModels[K]> {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(`/api/v1/${name}${query ? "?" + query : ""}`, {
    headers: { Authorization: "Bearer " + token },
    signal,
    cache: "no-store",
    credentials: "omit",
  });
  const value = await response.json();
  if (!response.ok)
    throw new ApiError(
      response.status,
      value.error?.code || "unavailable",
      value.error?.message || "读取失败，请重试。",
    );
  return value.data;
}

export function useRead<K extends keyof ReadModels>(
  name: K,
  params: Record<string, string> = {},
  enabled = true,
) {
  return useQuery({
    queryKey: [name, params],
    queryFn: ({ signal }) => read(name, params, signal),
    enabled,
    staleTime: name === "code" ? 0 : 15_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
}
