import { useEffect, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { ApiError } from "../auth";
import { tutorApi } from "../api/tutorSession";
import type { Step } from "../api/solutionFlow";

const supportedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const accept = [...supportedTypes].join(",");

type Props = {
  sessionId: number;
  loginUrl: string;
  disabled: boolean;
  reviewing: boolean;
  onReading: (reading: boolean) => void;
  onRead: (steps: Step[]) => void;
};

export function TutorWorkPhoto({ sessionId, loginUrl, disabled, reviewing, onReading, onRead }: Props) {
  const picker = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  const [photoUrl, setPhotoUrl] = useState("");
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [recognition, setRecognition] = useState<number | null>(null);
  const [error, setError] = useState("");

  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => () => { if (photoUrl) URL.revokeObjectURL(photoUrl); }, [photoUrl]);
  useEffect(() => {
    if (startedAt === null) return;
    const timer = window.setInterval(() => setElapsed((performance.now() - startedAt) / 1000), 100);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  async function select(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || disabled || reviewing || request.current) return;
    if (file.size > 30 * 1024 * 1024 || file.type && !supportedTypes.has(file.type)) {
      setError("30MB 이하의 JPEG·PNG·WebP 사진을 선택해 주세요.");
      return;
    }

    const controller = new AbortController();
    const started = performance.now();
    request.current = controller;
    setPhotoUrl(URL.createObjectURL(file));
    setStartedAt(started);
    setElapsed(0);
    setRecognition(null);
    setError("");
    onReading(true);
    try {
      const result = await tutorApi.workPhoto(sessionId, file, controller.signal);
      if (controller.signal.aborted) return;
      setRecognition(result.recognition_seconds);
      onRead(result.lines.map((line) => ({ ...line, id: crypto.randomUUID() })));
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (reason instanceof ApiError && reason.status === 401) {
        location.replace(loginUrl);
        return;
      }
      setError(reason instanceof ApiError && typeof reason.detail === "string"
        ? reason.detail : "풀이 사진을 읽지 못했어요. 사진을 다시 선택하거나 직접 입력해 주세요.");
    } finally {
      if (!controller.signal.aborted) {
        request.current = null;
        setStartedAt(null);
        setElapsed((performance.now() - started) / 1000);
        onReading(false);
      }
    }
  }

  const reading = startedAt !== null;
  const locked = disabled || reading || reviewing;
  return <div className="tutor-work-photo" role="group" aria-label="풀이 사진">
    <div className="tutor-actions">
      <button type="button" className="solution-button is-secondary" disabled={locked}
        onClick={() => picker.current?.click()}>풀이 사진 올리기</button>
      <button type="button" className="solution-button is-secondary" disabled={locked}
        onClick={() => camera.current?.click()}>풀이 사진 찍기</button>
    </div>
    <input ref={picker} type="file" accept={accept} aria-label="풀이 사진 파일 선택" hidden disabled={locked} onChange={(event) => void select(event)} />
    <input ref={camera} type="file" accept={accept} capture="environment" aria-label="풀이 사진 촬영" hidden disabled={locked} onChange={(event) => void select(event)} />
    {photoUrl && <details className="tutor-details" open={reviewing || undefined}>
      <summary>올린 풀이 사진 보기</summary>
      <img className="tutor-source-photo tutor-work-photo-image" src={photoUrl} alt="확인할 손글씨 풀이 사진" />
    </details>}
    {photoUrl && <p className="solution-help tutor-timing">
      {reading ? "풀이 사진을 읽는 중 · " : ""}사진 선택부터 {elapsed.toFixed(1)}초
      {recognition !== null && ` · 서버 인식 ${recognition.toFixed(1)}초`}
    </p>}
    {reading && <p className="solution-help" role="status">사진에 쓴 풀이를 줄별로 읽고 있어요.</p>}
    {error && <p className="solution-error" role="alert">{error}</p>}
  </div>;
}
