import type {
  ContentPart,
  ProblemReview,
  ReviewBlock,
} from "../types/problemReview";
import type { PhotoRegion } from "../types/photoRegion";

export type OcrPhase =
  | "idle"
  | "restoring"
  | "uploading"
  | "quality_review"
  | "region_review"
  | "queued"
  | "running"
  | "completed"
  | "failed";

export type PhotoQuality = {
  policy_version: string;
  status: "warning" | "no_warning";
  warnings: {
    code: string;
    message: string;
  }[];
};

export type Submission = {
  id: number;
  photo_quality?: PhotoQuality;
  selected_region?: PhotoRegion | null;
  image_rotation_ccw?: number;
  image_orientation?: {
    status: "corrected" | "unchanged" | "uncertain" | "unavailable" | "skipped";
    rotation_ccw: number;
    score?: number | null;
  } | null;
  review_status: "awaiting_input" | "needs_review" | "confirmed";
  latest_input: {
    content_text: string;
    version_number: number;
    confirmed_at: string | null;
  } | null;
};

export class ReviewApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ReviewApiError";
  }
}

type OcrJob = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  error_message: string | null;
  selected_region?: PhotoRegion | null;
};

async function request<T>(
  path: string,
  options: RequestInit,
): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, credentials: "same-origin" });
  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    if (response.status === 401) window.location.replace("/login");
    throw new ReviewApiError(
      response.status,
      typeof payload?.detail === "string"
        ? payload.detail
        : `요청에 실패했습니다. (${response.status})`,
    );
  }

  if (payload === null) {
    throw new Error("사진 API의 응답 형식을 확인해 주세요.");
  }

  return payload as T;
}

export function saveReview(
  submissionId: number,
  contentText: string,
  expectedVersionNumber: number,
  confirm: boolean,
  signal: AbortSignal,
): Promise<Submission> {
  return request<Submission>(
    `/submissions/${submissionId}/${confirm ? "confirm" : "input"}`,
    {
      method: confirm ? "POST" : "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        content_text: contentText,
        expected_version_number: expectedVersionNumber,
      }),
      signal,
    },
  );
}

export async function loadReview(submissionId: number, signal: AbortSignal) {
  const path = `/submissions/${submissionId}`;
  const [submission, history, photo] = await Promise.all([
    request<Submission>(path, { signal }),
    request<{ origin: string; content_text: string }[]>(
      `${path}/input/history`,
      { signal },
    ),
    readReviewPhoto(submissionId, signal),
  ]);

  if (!submission.latest_input) {
    throw new Error("아직 문제 인식이 완료되지 않았습니다.");
  }

  const original = history.find((version) => version.origin === "ocr");
  if (!original) throw new Error("OCR 원문을 불러오지 못했습니다.");

  return {
    submission,
    originalText: original.content_text,
    photo,
  };
}

export async function readReviewPhoto(submissionId: number, signal: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/submissions/${submissionId}/photo?corrected=true&selected=true`, { signal, credentials: "same-origin" });
  if (!response.ok) {
    if (response.status === 401) window.location.replace("/login");
    throw new ReviewApiError(response.status, "문제 사진을 불러오지 못했습니다.");
  }
  return response.blob();
}

function waitForPoll(signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();

  return new Promise((resolve, reject) => {
    const abort = () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    };

    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, 1000);

    signal.addEventListener("abort", abort, { once: true });
  });
}

export async function uploadPhoto(
  file: File,
  signal: AbortSignal,
): Promise<Submission> {
  const form = new FormData();
  form.append("files", file);

  return request<Submission>("/submissions/photo", {
    method: "POST",
    body: form,
    signal,
  });
}

export async function recognizeSubmission(
  submission: Submission,
  signal: AbortSignal,
  onStatus: (phase: OcrPhase) => void,
  region: PhotoRegion,
): Promise<Submission> {
  onStatus("queued");

  let job = await request<OcrJob>(`/submissions/${submission.id}/ocr`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ region }),
    signal,
  });

  const accepted = job.selected_region;
  if (!accepted || accepted.coordinate_space !== region.coordinate_space ||
    accepted.image_size.some((value, index) => value !== region.image_size[index]) ||
    accepted.bbox.some((value, index) => value !== region.bbox[index])) {
    throw new Error("선택한 문제 범위를 서버에서 확인하지 못했습니다. API 서버를 다시 시작해 주세요.");
  }

  while (true) {
    signal.throwIfAborted();

    if (job.status === "failed") {
      throw new Error(job.error_message || "문제 인식에 실패했습니다.");
    }

    if (job.status === "completed") {
      const result = await request<Submission>(
        `/submissions/${submission.id}`,
        { signal },
      );

      if (!result.latest_input?.content_text.trim()) {
        throw new Error("완료된 인식 내용을 가져오지 못했습니다.");
      }

      return result;
    }

    if (job.status !== "queued" && job.status !== "running") {
      throw new Error("OCR 작업 상태를 확인해 주세요.");
    }

    onStatus(job.status);
    await waitForPoll(signal);
    job = await request<OcrJob>(`/ocr-jobs/${job.id}`, { signal });
  }
}

// 구분 기호가 있는 수식만 분리하고, 나머지 본문과 줄바꿈은 보존합니다.
function parseReviewContent(text: string): ReviewBlock[] {
  const parts: ContentPart[] = [];
  const mathPattern =
    /\\\(([\s\S]*?)\\\)|\\\[([\s\S]*?)\\\]|(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$|(?<!\\)\$([^$\n]+)(?<!\\)\$/g;
  let cursor = 0;

  function addText(value: string) {
    if (!value) return;
    parts.push({ id: `part-${parts.length}`, kind: "text", text: value });
  }

  for (const match of text.matchAll(mathPattern)) {
    const index = match.index!;
    addText(text.slice(cursor, index));

    const latex = match[1] ?? match[2] ?? match[3] ?? match[4];
    if (latex.trim()) {
      parts.push({
        id: `part-${parts.length}`,
        kind: "math",
        latex: latex.trim(),
        source: { latex: latex.trim(), raw: match[0] },
      });
    } else {
      addText(match[0]);
    }

    cursor = index + match[0].length;
  }

  addText(text.slice(cursor));

  return [
    { id: "paragraph-1", kind: "paragraph", parts },
  ];
}

export function serializeReviewContent(blocks: ReviewBlock[]): string {
  return blocks.map((block) => {
    if (block.kind !== "paragraph") {
      throw new Error("도형 조건을 포함한 문제의 저장은 아직 연결되지 않았습니다.");
    }

    return block.parts.map((part) => {
      if (part.kind === "text") return part.text;
      if (part.source?.latex === part.latex) return part.source.raw;
      return `\\(${part.latex}\\)`;
    }).join("");
  }).join("\n\n");
}

export function createReviewProblem(
  submission: Submission,
  originalText = submission.latest_input?.content_text ?? "",
  imageUrl: string | null = null,
): ProblemReview {
  const text = submission.latest_input?.content_text ?? "";

  return {
    id: String(submission.id),
    imageUrl,
    original: parseReviewContent(originalText),
    draft: parseReviewContent(text),
  };
}
