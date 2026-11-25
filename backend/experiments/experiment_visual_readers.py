import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import torch
from PIL import Image, ImageOps
from transformers import (
    AutoProcessor,
    BitsAndBytesConfig,
    Qwen3VLForConditionalGeneration,
    Qwen3_5ForConditionalGeneration,
    TextStreamer,
)


ROOT = Path(__file__).resolve().parents[2]
READERS = {
    "omniedu": {
        "model_id": "lhpku20010120/Omni-Edu-4B",
        "revision": "bf842b196ea0f91fe98b5464878b78e0c690fdea",
        "directory": ".venv/finetune-models/Omni-Edu-4B",
        "class": Qwen3_5ForConditionalGeneration,
    },
    "qwen": {
        "model_id": "Qwen/Qwen3-VL-4B-Instruct",
        "revision": "ebb281ec70b05090aa6165b016eac8ec08e71b17",
        "directory": ".venv/finetune-models/Qwen3-VL-4B-Instruct",
        "class": Qwen3VLForConditionalGeneration,
    },
}
SAMPLE_IDS = (
    "triangle_angles_07",
    "quadratic_graph_11",
    "linear_graph_07",
    "congruence_21",
)
PROMPT = """사진의 도형·그래프에 직접 표시된 정보만 읽으세요.
문제를 풀거나 각도·길이·좌표를 계산하지 마세요.
본문·풀이에만 나온 내용을 그림에 직접 표시된 것으로 옮기지 마세요.
모양만 보고 직각·평행·같은 길이 등의 관계를 추측하지 마세요.
다음 네 항목에 간결하게 답하세요. 같은 표시는 한 번만 적으세요.

[문자·축·식]
점 이름, 축 이름, 원점의 문자, 그래프에 적힌 식을 옮기세요.

[수치와 위치]
표시된 수치·단위와 연결된 점·선분·각·축의 위치를 적으세요.
각은 가능하면 어느 두 선분 사이인지 구분하세요.

[명시적 관계]
직각·같은 길이 등 실제 기호로 표시된 관계만 적으세요.
해당 표시가 없으면 '없음'이라고 적으세요.

[불확실]
표시가 있지만 읽거나 연결할 수 없는 부분만 적으세요.
불확실한 숫자나 관계를 만들지 마세요. 해당 부분이 없으면 '없음'이라고 적으세요.
네 항목 뒤에는 풀이·정답·힌트를 추가하지 말고 출력을 마치세요."""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def encode(processor, row):
    with Image.open(row["image"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    try:
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": PROMPT},
            ],
        }]
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            enable_thinking=False,
        )
    finally:
        image.close()
    inputs.pop("token_type_ids", None)
    return inputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reader", choices=READERS, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--image-token-budget", type=int, default=1024)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--limit", type=int, default=len(SAMPLE_IDS))
    parser.add_argument("--max-time", type=float, default=180.0)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= len(SAMPLE_IDS):
        raise SystemExit("샘플 수는 1~4개여야 합니다.")
    if args.image_token_budget < 64 or args.max_new_tokens < 1 or args.max_time <= 0:
        raise SystemExit("이미지 토큰·출력 토큰·시간 설정이 잘못됐습니다.")

    manifest = read_json(ROOT / "data/finetune/manifest.json")
    rows = {row["id"]: row for row in manifest["samples"]}
    selected = [rows[sid] for sid in SAMPLE_IDS[:args.limit]]
    for row in selected:
        if digest(Path(row["image"])) != row["image_sha256"]:
            raise RuntimeError(f"사진이 변경됐습니다: {row['id']}")
        if digest(ROOT / row["label"]) != row["label_sha256"]:
            raise RuntimeError(f"정답이 변경됐습니다: {row['id']}")
        if row["review_status"] != "user_verified":
            raise RuntimeError(f"검수되지 않은 정답입니다: {row['id']}")

    reader = READERS[args.reader]
    model_dir = ROOT / reader["directory"]
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    processor.image_processor.size = {
        "shortest_edge": 65536,
        "longest_edge": args.image_token_budget * 32 * 32,
    }
    image_token_id = read_json(model_dir / "config.json")["image_token_id"]
    if args.check_only:
        for row in selected:
            inputs = encode(processor, row)
            print(json.dumps({
                "id": row["id"],
                "image_tokens": int((inputs["input_ids"] == image_token_id).sum()),
                "prompt_tokens": inputs["input_ids"].shape[1],
            }), flush=True)
            del inputs
        print("CPU 전처리 검사 완료. 모델 추론·학습은 실행하지 않았습니다.")
        return

    output_dir = args.output_dir.resolve()
    if not output_dir.is_relative_to(ROOT):
        raise RuntimeError("실험 결과는 프로젝트 안에 저장해야 합니다.")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{args.reader}.json"
    if report_path.exists():
        raise RuntimeError("기존 결과가 있습니다. 다른 결과 폴더를 사용하세요.")
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        llm_int8_skip_modules=["visual", "lm_head", "in_proj_a", "in_proj_b"],
    )
    generation = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": False,
        "use_cache": True,
        "max_time": args.max_time,
    }
    report = {
        "status": "loading",
        "purpose": "development_visual_condition_extraction",
        "reader": args.reader,
        "model_id": reader["model_id"],
        "model_revision": reader["revision"],
        "model_dir": str(model_dir),
        "model_config_sha256": digest(model_dir / "config.json"),
        "quantization": quantization.to_dict(),
        "processor_size": dict(processor.image_processor.size),
        "enable_thinking": False,
        "generation": generation,
        "prompt": PROMPT,
        "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
        "created_at": datetime.now().astimezone().isoformat(),
        "script_sha256": digest(Path(__file__)),
        "training_executed": False,
        "adapter_used": False,
        "evaluation_note": "Previously inspected development images. References are never fed to the model. This local NF4 experiment does not reproduce official BF16 benchmark settings.",
        "versions": {
            name: version(name)
            for name in ("torch", "transformers", "bitsandbytes", "accelerate")
        },
        "samples": [],
    }
    save(report_path, report)
    phase = "model_load"
    try:
        started = perf_counter()
        model = reader["class"].from_pretrained(
            model_dir,
            local_files_only=True,
            quantization_config=quantization,
            dtype=torch.bfloat16,
            device_map={"": 0},
            attn_implementation="sdpa",
        ).eval()
        model.config.use_cache = True
        if hasattr(model.config, "text_config"):
            model.config.text_config.use_cache = True
        torch.cuda.synchronize()
        report["load_seconds"] = perf_counter() - started
        report["gpu"] = torch.cuda.get_device_name(0)
        report["loaded_gpu_allocated_gib"] = torch.cuda.memory_allocated() / 2**30
        report["parameter_devices"] = sorted({str(p.device) for p in model.parameters()})
        report["status"] = "running"
        save(report_path, report)
        print("모델 로딩 완료:", args.reader, report["loaded_gpu_allocated_gib"], flush=True)

        for row in selected:
            phase = row["id"]
            prep_started = perf_counter()
            inputs = encode(processor, row).to("cuda:0")
            torch.cuda.synchronize()
            prep_seconds = perf_counter() - prep_started
            prompt_length = inputs["input_ids"].shape[1]
            image_tokens = int((inputs["input_ids"] == image_token_id).sum().item())
            print(f"\n{args.reader}: {row['id']}, 이미지 토큰={image_tokens}", flush=True)
            streamer = TextStreamer(processor.tokenizer, skip_prompt=True, skip_special_tokens=True)
            torch.cuda.reset_peak_memory_stats()
            started = perf_counter()
            with torch.inference_mode():
                output = model.generate(**inputs, **generation, streamer=streamer)
            torch.cuda.synchronize()
            seconds = perf_counter() - started
            generated = output[:, prompt_length:]
            text = processor.batch_decode(
                generated, skip_special_tokens=True, clean_up_tokenization_spaces=False,
            )[0]
            raw_text = processor.batch_decode(generated, skip_special_tokens=False)[0]
            token_count = generated.shape[1]
            eos_ids = model.generation_config.eos_token_id
            if isinstance(eos_ids, int):
                eos_ids = [eos_ids]
            ended_with_eos = bool(token_count and eos_ids and generated[0, -1].item() in eos_ids)
            repeated_lines = [line for line, count in Counter(text.splitlines()).items() if len(line.strip()) >= 8 and count >= 3]
            reference = (ROOT / row["label"]).read_text(encoding="utf-8-sig").strip()
            report["samples"].append({
                "id": row["id"],
                "group_id": row["group_id"],
                "image": row["image"],
                "image_sha256": row["image_sha256"],
                "label_sha256": row["label_sha256"],
                "reference": reference,
                "image_tokens": image_tokens,
                "image_grid_thw": inputs["image_grid_thw"].tolist(),
                "prompt_tokens": prompt_length,
                "preprocess_seconds": prep_seconds,
                "generation_seconds": seconds,
                "generated_tokens": token_count,
                "ended_with_eos": ended_with_eos,
                "length_limit_reached": token_count >= args.max_new_tokens and not ended_with_eos,
                "time_limit_reached": seconds >= args.max_time and not ended_with_eos,
                "repeated_lines": repeated_lines,
                "gpu_peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30,
                "text": text,
                "raw_generated_text": raw_text,
            })
            save(report_path, report)
            print(f"생성 {seconds:.2f}초 / {token_count}토큰", flush=True)
            del output, generated, inputs
            torch.cuda.empty_cache()

    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = {"phase": phase, "type": type(error).__name__, "message": str(error)}
        save(report_path, report)
        print("실험 중단 기록:", report_path, flush=True)
        raise

    report["status"] = "completed"
    save(report_path, report)
    print("\n시각 조건 실험 결과 저장:", report_path, flush=True)


if __name__ == "__main__":
    main()
