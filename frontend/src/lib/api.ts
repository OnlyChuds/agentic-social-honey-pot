import type {
  EvalScenariosResponse,
  EvalStatusResponse,
  LaunchEvalRequest,
  LaunchEvalResponse,
  LaunchRunRequest,
  LaunchRunResponse,
  SnapshotJSON,
  TargetDetailResponse,
  TargetListResponse,
} from "@/lib/types";

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // ignore non-JSON body
    }
    throw new Error(`HTTP ${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export function fetchTargets(): Promise<TargetListResponse> {
  return fetchJson<TargetListResponse>("/api/targets");
}

export function createOfflineReplay(): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>("/api/offline-replays", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fixture: "acknowledgment" }),
  });
}

export function fetchTargetDetail(runId: string): Promise<TargetDetailResponse> {
  return fetchJson<TargetDetailResponse>(`/api/targets/${encodeURIComponent(runId)}`);
}

export function fetchSnapshotAtVersion(runId: string, version: number): Promise<SnapshotJSON> {
  return fetchJson<SnapshotJSON>(
    `/api/targets/${encodeURIComponent(runId)}/snapshot/${version}`
  );
}

export function launchPracticeRun(req: LaunchRunRequest): Promise<LaunchRunResponse> {
  return fetchJson<LaunchRunResponse>("/api/runs/launch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...req, scenario: "practice" }),
  });
}

export function stopPracticeRun(runId: string): Promise<{ run_id: string; stopped: boolean }> {
  return fetchJson<{ run_id: string; stopped: boolean }>(
    `/api/runs/${encodeURIComponent(runId)}/stop`,
    { method: "POST" }
  );
}

export function fetchEvalStatus(): Promise<EvalStatusResponse> {
  return fetchJson<EvalStatusResponse>("/api/eval/status");
}

export function fetchEvalScenarios(): Promise<EvalScenariosResponse> {
  return fetchJson<EvalScenariosResponse>("/api/eval/scenarios");
}

export function launchEvalRuns(req: LaunchEvalRequest): Promise<LaunchEvalResponse> {
  return fetchJson<LaunchEvalResponse>("/api/eval/launch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}
