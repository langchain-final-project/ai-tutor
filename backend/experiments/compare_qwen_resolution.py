import argparse
import copy
import hashlib
import json
from contextlib import nullcontext
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import torch
from peft import PeftModel, prepare_model_for_kbit_training
from PIL import Image, ImageOps
from transformers import AutoProcessor, TextStreamer


ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / ".venv/finetune-models/Qwen3-VL-4B-Instruct"
SAMPLE_ID = "polygon_06_crop"
IMAGE_TOKEN_BUDGETS = (512, 2048)
GENERATION = {
    "max_new_tokens": 512,
    "do_sample": False,
    "use_cache": True,
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_inputs(processor, row, budget, image_token_id):
    processor.image_processor.size = {
        "shortest_edge": 65536,
        "longest_edge": budget * 32 * 32,
    }
    with Image.open(row["image"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    try:
        # 검수 정답은 보고서에만 보존하고 모델 입력에서 제외합니다.
        messages = copy.deepcopy(row["messages"][:1])
        if len(messages) != 1 or messages[0]["role"] != "user":
            raise RuntimeError("사진과 프롬프트로 된 사용자 입력이 필요합니다.")
        messages[0]["content"][0]["image"] = image
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        image_size = list(image.size)
    finally:
        image.close()

    inputs.pop("token_type_ids", None)
    details = {
        "image_token_budget": budget,
        "processor_size": dict(processor.image_processor.size),
        "image_size": image_size,
        "image_grid_thw": inputs["image_grid_thw"].tolist(),
        "image_tokens": int(
            (inputs["input_ids"] == image_token_id).sum().item()
        ),
        "prompt_tokens": inputs["input_ids"].shape[1],
    }
    if details["image_tokens"] <= 0:
        raise RuntimeError("이미지 토큰이 없습니다.")
    return inputs, details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    checkpoint = args.checkpoint.resolve()
    config = read_json(checkpoint.parent / "run_config.json")
    data_path = Path(config["dataset_path"])
    if digest(data_path) != config["dataset_sha256"]:
        raise RuntimeError("학습 당시와 데이터 파일이 다릅니다.")
    rows = {
        row["id"]: row
        for row in map(
            json.loads,
            filter(None, data_path.read_text(encoding="utf-8").splitlines()),
        )
    }
    row = rows[SAMPLE_ID]
    if SAMPLE_ID not in config["sample_ids"] or row["split"] != "train":
        raise RuntimeError("이 파일은 학습한 다각형 문제의 진단용입니다.")
    if digest(Path(row["image"])) != row["image_sha256"]:
        raise RuntimeError("사진이 변경됐습니다.")
    if digest(ROOT / row["label"]) != row["label_sha256"]:
        raise RuntimeError("정답 TXT가 변경됐습니다.")
    if row["review_status"] != "user_verified":
        raise RuntimeError("정답 검수가 필요합니다.")

    processor = AutoProcessor.from_pretrained(
        checkpoint, local_files_only=True,
    )
    if dict(processor.image_processor.size) != config["processor_size"]:
        raise RuntimeError("저장된 전처리 설정과 학습 기록이 다릅니다.")
    image_token_id = read_json(MODEL_DIR / "config.json")["image_token_id"]

    # 이 경로에서는 GPU 모델을 로딩하지 않고 CPU 전처리만 검사합니다.
    if args.check_only:
        for budget in IMAGE_TOKEN_BUDGETS:
            inputs, details = make_inputs(
                processor, row, budget, image_token_id,
            )
            print(json.dumps(details, ensure_ascii=False), flush=True)
            del inputs
        print("CPU 전처리 검사 완료. 모델 추론과 학습은 실행하지 않았습니다.")
        return

    from check_qwen_4bit import model, quant_config

    if quant_config.to_dict() != config["quantization"]:
        raise RuntimeError("학습 당시와 양자화 설정이 다릅니다.")
    if model.config.image_token_id != image_token_id:
        raise RuntimeError("이미지 토큰 설정이 다릅니다.")
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=False,
    )
    model = PeftModel.from_pretrained(
        model, checkpoint, is_trainable=False, local_files_only=True,
    )
    model.eval()
    model.config.use_cache = True

    report_path = checkpoint.parent / (
        "resolution_diagnostic_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + ".json"
    )
    report = {
        "status": "running",
        "purpose": "training_sample_resolution_diagnostic",
        "sample_id": SAMPLE_ID,
        "group_id": row["group_id"],
        "used_in_training": True,
        "checkpoint": str(checkpoint),
        "adapter_sha256": digest(checkpoint / "adapter_model.safetensors"),
        "model_revision": config["model_revision"],
        "dataset_path": str(data_path),
        "dataset_sha256": config["dataset_sha256"],
        "script_sha256": digest(Path(__file__)),
        "image": row["image"],
        "image_sha256": row["image_sha256"],
        "label_sha256": row["label_sha256"],
        "reference": row["messages"][1]["content"][0]["text"],
        "prompt": row["messages"][0]["content"][1]["text"],
        "training_processor_size": config["processor_size"],
        "generation": GENERATION,
        "quantization": quant_config.to_dict(),
        "gpu": torch.cuda.get_device_name(0),
        "versions": {
            name: version(name)
            for name in ("torch", "transformers", "peft", "bitsandbytes")
        },
        "timing_note": "Single pass; generation time includes prefill and decoding. No warmup or repeated timing trials.",
        "variants": [],
    }
    write_json(report_path, report)
    print("진단 기록 파일:", report_path, flush=True)
    active = {}

    try:
        for budget in IMAGE_TOKEN_BUDGETS:
            active = {"image_token_budget": budget, "phase": "preprocess"}
            inputs, details = make_inputs(
                processor, row, budget, image_token_id,
            )
            variant = {**details, "outputs": {}}
            report["variants"].append(variant)
            write_json(report_path, report)
            inputs = inputs.to("cuda:0")
            prompt_length = inputs["input_ids"].shape[1]

            for mode in ("base", "adapter"):
                active = {"image_token_budget": budget, "mode": mode}
                print(
                    f"\n상한={budget}, 실제 이미지 토큰={details['image_tokens']}, "
                    f"{mode} 인식 시작",
                    flush=True,
                )
                context = model.disable_adapter() if mode == "base" else nullcontext()
                streamer = TextStreamer(
                    processor.tokenizer,
                    skip_prompt=True,
                    skip_special_tokens=True,
                )
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
                torch.cuda.reset_peak_memory_stats()
                started = perf_counter()

                # 각 generate 호출이 새 캐시를 만듭니다. 조건 사이에 캐시를 공유하지 않습니다.
                with context:
                    with torch.inference_mode():
                        with torch.autocast("cuda", dtype=torch.bfloat16):
                            output = model.generate(
                                **inputs, **GENERATION, streamer=streamer,
                            )
                torch.cuda.synchronize()
                seconds = perf_counter() - started
                generated = output[:, prompt_length:]
                text = processor.batch_decode(
                    generated,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                )[0]
                token_count = generated.shape[1]
                eos_ids = model.generation_config.eos_token_id
                if isinstance(eos_ids, int):
                    eos_ids = [eos_ids]
                ended_with_eos = bool(
                    token_count and eos_ids
                    and generated[0, -1].item() in eos_ids
                )
                result = {
                    "text": text,
                    "generation_seconds": seconds,
                    "generated_tokens": token_count,
                    "ended_with_eos": ended_with_eos,
                    "length_limit_reached": (
                        token_count >= GENERATION["max_new_tokens"]
                        and not ended_with_eos
                    ),
                    "gpu_peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30,
                    "gpu_peak_reserved_gib": torch.cuda.max_memory_reserved() / 2**30,
                }
                variant["outputs"][mode] = result
                write_json(report_path, report)
                print(
                    f"완료: {seconds:.2f}초, 생성 {token_count}토큰, "
                    f"GPU 최대 텐서 {result['gpu_peak_allocated_gib']:.2f}GiB",
                    flush=True,
                )
                del output, generated
            del inputs
            torch.cuda.empty_cache()
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = {
            **active,
            "type": type(error).__name__,
            "message": str(error),
        }
        write_json(report_path, report)
        print("완료된 조건과 중단 기록 저장:", report_path, flush=True)
        raise

    report["status"] = "completed"
    write_json(report_path, report)
    print("\n해상도 진단 결과 저장:", report_path, flush=True)


if __name__ == "__main__":
    main()
