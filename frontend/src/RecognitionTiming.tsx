import { useCallback, useEffect, useState } from "react";
import "./recognition-timing.css";

type TimingEvent = "photo-selected" | "upload-started" | "upload-finished" |
  "ocr-started" | "ocr-finished" | "completed" | "failed" | "cancelled" | "restored";
type Clock = {
  selectedAt: number | null;
  uploadAt: number | null;
  uploadEnd: number | null;
  ocrAt: number | null;
  ocrEnd: number | null;
  endedAt: number | null;
  outcome: "completed" | "failed" | "cancelled" | null;
  restored: boolean;
};

const emptyClock: Clock = {
  selectedAt: null, uploadAt: null, uploadEnd: null, ocrAt: null, ocrEnd: null,
  endedAt: null, outcome: null, restored: false,
};

export function useRecognitionTiming() {
  const [clock, setClock] = useState<Clock>(emptyClock);

  const mark = useCallback((event: TimingEvent, at = performance.now()) => {
    setClock((current) => {
      if (event === "photo-selected") return { ...emptyClock, selectedAt: at };
      if (event === "restored") return { ...emptyClock, restored: true };
      if (current.selectedAt === null || current.endedAt !== null) return current;
      if (event === "upload-started") return { ...current, uploadAt: at };
      if (event === "upload-finished") return { ...current, uploadEnd: at };
      if (event === "ocr-started") return { ...current, ocrAt: at };
      if (event === "ocr-finished") return { ...current, ocrEnd: at };
      return {
        ...current, endedAt: at, outcome: event,
        uploadEnd: current.uploadAt === null ? null : current.uploadEnd ?? at,
        ocrEnd: current.ocrAt === null ? null : current.ocrEnd ?? at,
      };
    });
  }, []);

  return { clock, mark };
}

function elapsed(start: number | null, end: number | null, now: number) {
  if (start === null) return "—";
  const milliseconds = Math.max(0, (end ?? now) - start);
  if (end !== null && milliseconds > 0 && milliseconds < 100) return "0.1초 미만";
  const tenths = Math.floor(milliseconds / 100);
  const minutes = Math.floor(tenths / 600);
  const seconds = ((tenths % 600) / 10).toFixed(1);
  return minutes > 0 ? `${minutes}분 ${seconds}초` : `${seconds}초`;
}

export function RecognitionTiming({ timing }: {
  timing: ReturnType<typeof useRecognitionTiming>;
}) {
  const { clock } = timing;
  const [now, setNow] = useState(0);
  const running = clock.selectedAt !== null && clock.endedAt === null;
  useEffect(() => {
    if (!running) return;
    setNow(performance.now());
    const timer = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(timer);
  }, [running]);
  const status = clock.restored ? "저장된 문제" : clock.outcome === "completed" ? "완료" :
    clock.outcome === "failed" ? "실패 시점에서 정지" : clock.outcome === "cancelled" ? "중단 시점에서 정지" :
      clock.selectedAt === null ? "사진 선택 전" : clock.ocrEnd !== null ? "표시용 사진을 불러오는 중" :
        clock.ocrAt !== null ? "인식 중" : clock.uploadEnd !== null ? "사진 상태 확인 대기" :
          clock.uploadAt === null ? "범위 확인 대기" : "업로드 중";

  return <section className="recognition-timing" aria-label="사진 업로드와 인식 진행 시간">
    <div className="recognition-timing-heading"><h2>진행 시간</h2><span>{status}</span></div>
    <dl>
      <div><dt>사진 선택부터 전체 경과</dt><dd>{elapsed(clock.selectedAt, clock.endedAt, now)}</dd></div>
      <div><dt>사진 업로드</dt><dd>{elapsed(clock.uploadAt, clock.uploadEnd, now)}</dd></div>
      <div><dt>인식 요청 → 결과 수신</dt><dd>{elapsed(clock.ocrAt, clock.ocrEnd, now)}</dd></div>
    </dl>
    <p>전체 경과에는 사진 상태·범위를 확인하는 시간이 포함돼요. 인식 시간은 대기·서버 처리·결과 수신을 합친 요청 시간이에요.</p>
    {clock.restored && <p>저장된 문제의 이전 경과 시간은 알 수 없어요. 이 화면에서 새로 진행한 시간만 측정해요.</p>}
  </section>;
}
