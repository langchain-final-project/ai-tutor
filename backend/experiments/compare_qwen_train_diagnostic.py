import copy
import hashlib
import json
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from time import perf_counter

import torch
from peft import PeftModel, prepare_model_for_kbit_training
from PIL import Image, ImageOps
from transformers import AutoProcessor


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_IDS = [
    "polygon_06_crop",
    "triangle_angles_07",
    "quadratic_graph_11",
    "linear_graph_07",
    "congruence_21",
]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("체크포인트 폴더 경로를 넣어주세요.")

    checkpoint = Path(sys.argv[1]).resolve()
    config = read_json(checkpoint.parent / "run_config.json")

    data_path = Path(
        config.get(
            "dataset_path",
            str(ROOT / "data/finetune/pilot_messages.jsonl"),
        )
    )
    if digest(data_path) != config["dataset_sha256"]:
        raise RuntimeError("학습 당시와 데이터 파일이 다릅니다.")

    rows = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {row["id"]: row for row in rows}
    trained_groups = {
        by_id[sample_id]["group_id"]
        for sample_id in config["sample_ids"]
    }

    for sample_id in SAMPLE_IDS:
        row = by_id[sample_id]
        if (
            sample_id not in config["sample_ids"]
            or row["group_id"] not in trained_groups
        ):
            raise RuntimeError("학습에 사용한 문제만 진단할 수 있습니다.")
        if digest(Path(row["image"])) != row["image_sha256"]:
            raise RuntimeError(f"사진이 변경됐습니다: {sample_id}")

    # 기본 모델을 한 번 로딩하고, 어댑터를 끄거나 켜서 비교합니다.
    from check_qwen_4bit import model, quant_config

    if quant_config.to_dict() != config["quantization"]:
        raise RuntimeError("학습 당시와 양자화 설정이 다릅니다.")

    model = prepare_model_for_kbit_training(
        model,
        use_gradient_checkpointing=False,
    )
    model = PeftModel.from_pretrained(
        model,
        checkpoint,
        is_trainable=False,
        local_files_only=True,
    )
    model.eval()

    processor = AutoProcessor.from_pretrained(
        checkpoint,
        local_files_only=True,
    )
    if dict(processor.image_processor.size) != config["processor_size"]:
        raise RuntimeError("이미지 처리 설정이 다릅니다.")

    generation = {
        "max_new_tokens": 512,
        "do_sample": False,
        "use_cache": False,
    }

    report = {
        "status": "running",
        "purpose": "training_sample_diagnostic",
        "checkpoint": str(checkpoint),
        "adapter_sha256": digest(
            checkpoint / "adapter_model.safetensors"
        ),
        "model_revision": config["model_revision"],
        "dataset_sha256": config["dataset_sha256"],
        "processor_size": dict(processor.image_processor.size),
        "quantization": quant_config.to_dict(),
        "generation": generation,
        "samples": [],
    }
    report_path = checkpoint.parent / (
        "train_diagnostic_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".json"
    )

    def save_report():
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    save_report()

    for sample_id in SAMPLE_IDS:
        row = by_id[sample_id]

        with Image.open(row["image"]) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")

        # 검수 정답은 모델 입력에서 제외합니다.
        messages = copy.deepcopy(row["messages"][:1])
        messages[0]["content"][0]["image"] = image

        try:
            inputs = processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            ).to("cuda:0")
        finally:
            image.close()

        inputs.pop("token_type_ids", None)
        prompt_length = inputs["input_ids"].shape[1]

        item = {
            "id": sample_id,
            "group_id": row["group_id"],
            "used_in_training": True,
            "image": row["image"],
            "image_sha256": row["image_sha256"],
            "image_tokens": int(
                (
                    inputs["input_ids"]
                    == model.config.image_token_id
                ).sum().item()
            ),
            "reference": row["messages"][1]["content"][0]["text"],
            "outputs": {},
        }
        report["samples"].append(item)

        for mode in ("base", "adapter"):
            context = (
                model.disable_adapter()
                if mode == "base"
                else nullcontext()
            )

            print(f"\n{sample_id}: {mode} 인식 시작", flush=True)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            started = perf_counter()

            with context:
                with torch.inference_mode():
                    with torch.autocast("cuda", dtype=torch.bfloat16):
                        output = model.generate(
                            **inputs,
                            **generation,
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
            item["outputs"][mode] = {
                "text": text,
                "generation_seconds": seconds,
                "generated_tokens": token_count,
                "length_limit_reached": (
                    token_count >= generation["max_new_tokens"]
                ),
                "gpu_peak_allocated_gib": (
                    torch.cuda.max_memory_allocated() / 2**30
                ),
            }
            save_report()

            print(text, flush=True)
            print(f"생성 시간: {seconds:.2f}초", flush=True)

            del output, generated

        del inputs

    report["status"] = "completed"
    save_report()
    print("\n진단 결과 저장:", report_path, flush=True)


if __name__ == "__main__":
    main()