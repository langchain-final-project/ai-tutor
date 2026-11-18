"""실행 중인 llama.cpp 서버 또는 지정한 GGUF 모델에 연결한다."""

import base64
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class OCRError(RuntimeError):
    """모델 연결 또는 응답 오류."""

    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _repeating(text: str) -> bool:
    paragraphs = Counter(p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= 20)
    return any(n >= 3 for n in paragraphs.values()) or (
        len(text) >= 250 and text.count(text[-50:]) >= 3
    )


def _events(response):
    """SSE의 여러 data 행도 하나의 이벤트로 합친다."""
    lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif not line and lines:
            yield "\n".join(lines)
            lines = []
    if lines:
        yield "\n".join(lines)


class LlamaClient:
    def __init__(self, url: str, timeout: float = 90):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("서버 주소는 http(s)://호스트:포트 또는 /v1 주소여야 합니다.")
        self.url = url.rstrip("/")
        if not self.url.endswith("/v1"):
            self.url += "/v1"
        self.timeout = timeout

    def stream(self, image: bytes, prompt: str, max_tokens: int = 1024):
        """delta 이벤트 뒤에 complete 이벤트를 반환한다. 중단한 출력은 stop이 아니다."""
        payload = {
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}},
                {"type": "text", "text": prompt},
            ]}],
            "temperature": 0, "seed": 42, "max_tokens": max_tokens,
            "stream": True, "stream_options": {"include_usage": True}, "cache_prompt": False,
        }
        request = Request(self.url + "/chat/completions", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        text, finish, usage = "", "incomplete", None
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for data in _events(response):
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if not isinstance(event, dict) or not isinstance(event.get("choices", []), list):
                        raise OCRError("모델 스트림의 형식이 올바르지 않습니다.")
                    if "error" in event:
                        raise OCRError("모델 서버가 오류 응답을 반환했습니다.")
                    usage = event.get("usage") or usage
                    for choice in event.get("choices", []):
                        if not isinstance(choice, dict) or not isinstance(choice.get("delta", {}), dict):
                            raise OCRError("모델 스트림의 토큰 형식이 올바르지 않습니다.")
                        delta = choice.get("delta", {}).get("content") or ""
                        if not isinstance(delta, str):
                            raise OCRError("문자열이 아닌 OCR 토큰을 받았습니다.")
                        text += delta
                        if delta:
                            yield {"type": "delta", "text": delta}
                        finish = choice.get("finish_reason") or finish
                    if _repeating(text):
                        finish = "repetition"
                        break
                    if len(text) > 24000 or time.monotonic() - started > self.timeout:
                        finish = "limit"
                        break
        except HTTPError as error:
            raise OCRError(
                "OCR 서버의 응답을 확인해 주세요.",
                retryable=error.code in {408, 429, 500, 502, 503, 504},
            ) from error
        except URLError as error:
            raise OCRError(
                "OCR 서버 연결을 확인해 주세요.",
                retryable=not isinstance(error.reason, ConnectionRefusedError),
            ) from error
        except OSError as error:
            raise OCRError("OCR 서버 연결 또는 스트림을 확인해 주세요.") from error
        except ValueError as error:
            raise OCRError("OCR 서버 스트림 형식을 확인해 주세요.", retryable=False) from error
        yield {"type": "complete", "text": text, "finish_reason": finish,
               "usage": usage, "seconds": round(time.monotonic() - started, 3)}


@dataclass(frozen=True)
class Backend:
    """url 또는 model+mmproj 중 하나를 지정한다. 두 로컬 모델은 순서대로 실행한다."""

    url: str | None = None
    model: Path | None = None
    mmproj: Path | None = None
    executable: str = "llama-server"
    timeout: float = 90
    image_tokens: tuple[int, int] | None = None

    def __post_init__(self):
        if self.timeout <= 0:
            raise ValueError("제한 시간은 양수여야 합니다.")
        if self.url:
            if self.model or self.mmproj:
                raise ValueError("서버 주소와 모델 파일을 동시에 지정할 수 없습니다.")
            LlamaClient(self.url, self.timeout)
        elif not self.model or not self.mmproj:
            raise ValueError("서버 주소 또는 모델·mmproj 파일을 지정해 주세요.")
        if self.image_tokens and not (0 < self.image_tokens[0] <= self.image_tokens[1]):
            raise ValueError("이미지 토큰 범위가 올바르지 않습니다.")

    @contextmanager
    def connect(self):
        if self.url:
            yield LlamaClient(self.url, self.timeout)
            return
        for path in (self.model, self.mmproj):
            if not Path(path).is_file():
                raise OCRError(f"모델 파일이 없습니다: {path}")
        executable = shutil.which(self.executable)
        if not executable:
            raise OCRError("llama-server 실행 파일을 찾을 수 없습니다.")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        args = [executable, "-m", str(self.model), "--mmproj", str(self.mmproj),
                "-ngl", "all", "--ctx-size", "8192", "--parallel", "1",
                "--batch-size", "512", "--ubatch-size", "128",
                "--host", "127.0.0.1", "--port", str(port)]
        if self.image_tokens:
            args += ["--image-min-tokens", str(self.image_tokens[0]),
                     "--image-max-tokens", str(self.image_tokens[1])]
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(args, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise OCRError("모델 서버가 종료됐습니다. 모델과 GPU 메모리를 확인해 주세요.")
                    try:
                        with urlopen(url + "/health", timeout=1) as response:
                            if response.status == 200:
                                break
                    except (URLError, OSError):
                        pass
                    time.sleep(0.2)
                else:
                    raise OCRError("모델 서버 준비 시간이 초과됐습니다.")
                yield LlamaClient(url, self.timeout)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
