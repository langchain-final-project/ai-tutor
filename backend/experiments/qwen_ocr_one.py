import hashlib
import json
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import torch
from PIL import Image, ImageOps
from transformers import AutoProcessor


# 1. 실행 명령에서 사진 경로를 받아 확인합니다.
if len(sys.argv) != 2:
    raise SystemExit("실행 명령 뒤에 사진 경로를 넣어주세요.")

image_path = Path(sys.argv[1]).resolve()

if not image_path.is_file():
    raise SystemExit(f"사진을 찾을 수 없습니다: {image_path}")


# 2. 기존 검사 파일을 실행하고 로딩된 모델과 설정을 가져옵니다.
from check_qwen_4bit import model, model_dir, quant_config, root


# 3. 다운로드한 전처리기를 불러옵니다.
processor = AutoProcessor.from_pretrained(
    model_dir,
    local_files_only=True,
)

# 이미지 총면적을 약 210만 픽셀로 제한합니다.
processor.image_processor.size = {
    "shortest_edge": 256 * 256,
    "longest_edge": 2048 * 32 * 32,
}


# 4. 사진의 EXIF 방향 정보를 적용하고 RGB로 읽습니다.
with Image.open(image_path) as source:
    image = ImageOps.exif_transpose(source).convert("RGB")


# 5. 학습 전 비교에 사용할 프롬프트입니다.
prompt = r"""
사진에 인쇄된 수학 문제를 그대로 전사하세요.
풀이, 정답, 요약, 문장 교정은 하지 마세요.
수식은 LaTeX로 표현하세요.

다음 네 항목으로 출력하세요.

[본문과 선택지]
사진에 보이는 문장을 읽는 순서대로 옮기세요.

[표]
실제 표가 있을 때만 행과 열의 대응을 유지하여 옮기세요.
[빈칸]은 표 안에 실제로 비어 있는 칸에만 사용하세요.
표가 없으면 '없음'이라고 적으세요.

[그림에 표시된 정보]
도형·그래프에 실제로 표시된 문자, 수치, 단위, 기호와
명시적으로 표시된 관계만 적으세요.
서로 다른 도형의 표시는 구분하세요.
같은 도형의 같은 표시에 대한 설명은 한 번만 작성하세요.
표시가 없는 항목을 새로 만들거나 [빈칸]으로 채우지 마세요.
모양만 보고 수치나 기하 관계를 추측하지 마세요.

[판독 불가]
실제로 표시가 있지만 읽을 수 없는 부분만 적으세요.
그 부분은 본문이나 그림 정보에서도 [판독 불가]로 표시하세요.
해당하는 부분이 없으면 '없음'이라고 적으세요.

네 항목을 작성한 뒤 출력을 마치세요.
""".strip()

messages = [
    {
        "role": "user",
        "content": [
            {"type": "image", "image": image},
            {"type": "text", "text": prompt},
        ],
    }
]


# 6. 사진과 프롬프트를 모델 입력으로 변환합니다.
torch.cuda.synchronize()
torch.cuda.reset_peak_memory_stats()
started = perf_counter()

inputs = processor.apply_chat_template(
    messages,
    tokenize=True,
    add_generation_prompt=True,
    return_dict=True,
    return_tensors="pt",
).to("cuda:0")

torch.cuda.synchronize()
preprocess_seconds = perf_counter() - started

input_length = inputs["input_ids"].shape[1]
image_tokens = (
    inputs["input_ids"] == model.config.image_token_id
).sum().item()

print("사진:", image_path, flush=True)
print("이미지 크기:", image.size, flush=True)
print("이미지 토큰 수:", image_tokens, flush=True)
print("OCR 시작", flush=True)


# 7. OCR을 실행하고 생성 시간을 측정합니다.
generation_settings = {
    "max_new_tokens": 1024,
    "do_sample": False,
}

started = perf_counter()

with torch.inference_mode():
    outputs = model.generate(
        **inputs,
        **generation_settings,
    )

torch.cuda.synchronize()
generation_seconds = perf_counter() - started


# 8. 입력 부분을 제외한 새 출력만 문자로 변환합니다.
generated = outputs[:, input_length:]

text = processor.batch_decode(
    generated,
    skip_special_tokens=True,
    clean_up_tokenization_spaces=False,
)[0]

generated_tokens = generated.shape[1]
length_limit_reached = (
    generated_tokens >= generation_settings["max_new_tokens"]
)
gpu_peak_allocated_gib = (
    torch.cuda.max_memory_allocated() / 2**30
)

print("\n--- OCR 결과 ---")
print(text)

print("\n--- 실행 기록 ---")
print(f"전처리 시간: {preprocess_seconds:.2f}초")
print(f"생성 시간: {generation_seconds:.2f}초")
print("생성 토큰 수:", generated_tokens)
print(f"GPU 최대 텐서 메모리: {gpu_peak_allocated_gib:.2f} GiB")

if length_limit_reached:
    print("주의: 출력 길이 제한에 도달했습니다. 결과가 잘렸을 수 있습니다.")


# 9. OCR 출력과 실행 조건을 JSON 파일로 저장합니다.
now = datetime.now().astimezone()

record = {
    "created_at": now.isoformat(),
    "stage": "baseline_untrained",
    "model_id": "Qwen/Qwen3-VL-4B-Instruct",
    "model_revision": "ebb281ec70b05090aa6165b016eac8ec08e71b17",
    "image_path": str(image_path),
    "image_sha256": hashlib.sha256(
        image_path.read_bytes()
    ).hexdigest(),
    "image_size": list(image.size),
    "processor_size": dict(processor.image_processor.size),
    "image_processor": type(processor.image_processor).__name__,
    "image_grid_thw": inputs["image_grid_thw"].cpu().tolist(),
    "image_tokens": image_tokens,
    "prompt": prompt,
    "generation": generation_settings,
    "quantization": quant_config.to_dict(),
    "versions": {
        name: version(name)
        for name in [
            "torch",
            "torchvision",
            "transformers",
            "bitsandbytes",
            "pillow",
        ]
    },
    "preprocess_seconds": preprocess_seconds,
    "generation_seconds": generation_seconds,
    "generated_tokens": generated_tokens,
    "length_limit_reached": length_limit_reached,
    "gpu_peak_allocated_gib": gpu_peak_allocated_gib,
    "ocr_text": text,
}

output_dir = root / ".venv/finetune-results"
output_dir.mkdir(parents=True, exist_ok=True)

output_path = output_dir / (
    f"{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
)

output_path.write_text(
    json.dumps(record, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print("결과 저장:", output_path)