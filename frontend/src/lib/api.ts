import type {
  LaunchRunRequest,
  LaunchRunResponse,
  TargetDetailResponse,
  TargetListResponse,
} from "@/lib/types";

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore parse failure
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export function fetchTargets(): Promise<TargetListResponse> {
  return requestJson<TargetListResponse>("/api/targets");
}

export function fetchTargetDetail(runId: string): Promise<TargetDetailResponse> {
  return requestJson<TargetDetailResponse>(`/api/targets/${encodeURIComponent(runId)}`);
}

export function launchPracticeRun(req: LaunchRunRequest): Promise<LaunchRunResponse> {
  return requestJson<LaunchRunResponse>("/api/runs/launch", {
    method: "POST",
    body: JSON.stringify({ scenario: "practice", ...req }),
  });
}

export function stopPracticeRun(
  runId: string
): Promise<{ run_id: string; stopped: boolean }> {
  return requestJson<{ run_id: string; stopped: boolean }>(
    `/api/runs/${encodeURIComponent(runId)}/stop`,
    { method: "POST" }
  );
}
