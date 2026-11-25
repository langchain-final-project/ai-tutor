import copy
import hashlib
import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import torch
import torch.nn.functional as F
from PIL import Image, ImageOps
from transformers import AutoProcessor


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data/finetune/pilot_messages.jsonl"
METADATA = ROOT / "data/finetune/pilot_messages.meta.json"

MAX_IMAGE_TOKENS = 512
LEARNING_RATE = 1e-4
SEED = 42

# 첫 시험에서는 이 세 문제를 한 번씩 학습합니다.
TRAIN_IDS = [
    "radicals_12_crop",
    "polygon_06_crop",
    "quadratic_graph_11",
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_batch(processor, row, image_token_id):
    # 사진 방향과 색상 처리는 기존 OCR 실행과 동일합니다.
    with Image.open(row["image"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    try:
        messages = copy.deepcopy(row["messages"])
        messages[0]["content"][0]["image"] = image

        full = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=False,
            return_dict=True,
            return_tensors="pt",
        )

        prefix = processor.apply_chat_template(
            messages[:1],
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
    finally:
        image.close()

    prompt_length = prefix["input_ids"].shape[1]

    if not torch.equal(
        full["input_ids"][:, :prompt_length],
        prefix["input_ids"],
    ):
        raise RuntimeError("프롬프트와 정답의 경계가 일치하지 않습니다.")

    target_count = full["input_ids"].shape[1] - prompt_length
    if target_count <= 0:
        raise RuntimeError("학습할 정답 토큰이 없습니다.")

    image_tokens = int(
        (full["input_ids"] == image_token_id).sum().item()
    )

    full.pop("token_type_ids", None)
    batch = full.to("cuda:0")
    targets = batch["input_ids"][:, prompt_length:]

    return batch, targets, image_tokens


def main():
    # 1. 학습 전에 데이터가 바뀌지 않았는지 검사합니다.
    metadata = json.loads(METADATA.read_text(encoding="utf-8"))

    if sha256(DATASET) != metadata["jsonl_sha256"]:
        raise RuntimeError("학습 데이터 파일의 해시가 달라졌습니다.")

    rows = [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sources = {
        item["id"]: item
        for item in metadata["sample_sources"]
    }

    for row in rows:
        if sha256(Path(row["image"])) != row["image_sha256"]:
            raise RuntimeError(f"사진이 변경됐습니다: {row['id']}")

        label = ROOT / sources[row["id"]]["label"]
        if sha256(label) != row["label_sha256"]:
            raise RuntimeError(f"정답이 변경됐습니다: {row['id']}")

    by_id = {row["id"]: row for row in rows}

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU가 필요합니다.")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("현재 설정에는 BF16 지원 GPU가 필요합니다.")

    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)

    # 2. 방금 검사한 4비트 모델과 LoRA 구성을 불러옵니다.
    from check_qwen_lora import model
    from check_qwen_4bit import model_dir, quant_config

    processor = AutoProcessor.from_pretrained(
        model_dir,
        local_files_only=True,
    )
    processor.image_processor.size = {
        "shortest_edge": 256 * 256,
        "longest_edge": MAX_IMAGE_TOKENS * 32 * 32,
    }

    model.train()
    model.get_base_model().model.visual.eval()

    trainable = [
        parameter
        for parameter in model.parameters()
        if parameter.requires_grad
    ]
    optimizer = torch.optim.AdamW(
        trainable,
        lr=LEARNING_RATE,
        weight_decay=0.0,
    )

    # 실제로 LoRA 값이 바뀌었는지 확인할 파라미터입니다.
    probe_name, probe = next(
        (name, parameter)
        for name, parameter in model.named_parameters()
        if parameter.requires_grad and "lora_B" in name
    )
    initial_probe = probe.detach().cpu().clone()

    run_dir = ROOT / ".venv/finetune-runs" / (
        "qlora_smoke_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    run_dir.mkdir(parents=True, exist_ok=False)

    write_json(
        run_dir / "run_config.json",
        {
            "purpose": "training_smoke_test_only",
            "created_at": datetime.now().astimezone().isoformat(),
            "model_id": metadata["model_id"],
            "model_revision": metadata["model_revision"],
            "dataset_sha256": sha256(DATASET),
            "script_sha256": sha256(Path(__file__)),
            "sample_ids": TRAIN_IDS,
            "steps": len(TRAIN_IDS),
            "batch_size": 1,
            "learning_rate": LEARNING_RATE,
            "seed": SEED,
            "processor_size": dict(processor.image_processor.size),
            "quantization": quant_config.to_dict(),
            "lora": model.peft_config["default"].to_dict(),
            "versions": {
                name: version(name)
                for name in (
                    "torch", "transformers", "peft",
                    "accelerate", "bitsandbytes",
                )
            },
        },
    )

    print("\n학습 결과 폴더:", run_dir, flush=True)
    history = []

    # 3. 배치 1로 세 번 학습합니다.
    for step, sample_id in enumerate(TRAIN_IDS, start=1):
        optimizer.zero_grad(set_to_none=True)

        batch, targets, image_tokens = make_batch(
            processor,
            by_id[sample_id],
            model.config.image_token_id,
        )
        target_count = targets.shape[1]

        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        started = perf_counter()

        print(
            f"\n학습 {step}/3 시작: {sample_id}, "
            f"이미지 토큰={image_tokens}, 정답 토큰={target_count}",
            flush=True,
        )

        with torch.autocast("cuda", dtype=torch.bfloat16):
            outputs = model(
                **batch,
                use_cache=False,
                logits_to_keep=target_count + 1,
            )

            # 정답의 첫 토큰을 예측하는 위치부터 손실을 계산합니다.
            # 사진과 프롬프트 자체는 학습 정답에 포함하지 않습니다.
            predictions = outputs.logits[:, :-1, :]
            loss = F.cross_entropy(
                predictions.float().reshape(-1, predictions.shape[-1]),
                targets.reshape(-1),
            )

        if not torch.isfinite(loss).item():
            raise RuntimeError("학습 손실이 유한한 값이 아닙니다.")

        loss.backward()

        grad_norm = torch.nn.utils.clip_grad_norm_(
            trainable,
            max_norm=1.0,
            error_if_nonfinite=True,
        )
        if grad_norm.item() <= 0:
            raise RuntimeError("LoRA에 유효한 기울기가 없습니다.")

        optimizer.step()
        torch.cuda.synchronize()

        changed = not torch.equal(
            initial_probe,
            probe.detach().cpu(),
        )
        if not changed:
            raise RuntimeError("LoRA 파라미터가 갱신되지 않았습니다.")

        record = {
            "step": step,
            "sample_id": sample_id,
            "loss": loss.item(),
            "grad_norm": grad_norm.item(),
            "image_tokens": image_tokens,
            "target_tokens": target_count,
            "training_seconds": perf_counter() - started,
            "gpu_peak_allocated_gib": (
                torch.cuda.max_memory_allocated() / 2**30
            ),
            "tracked_parameter": probe_name,
            "adapter_changed": changed,
        }

        # 4. 완료한 단계마다 어댑터와 학습 상태를 보존합니다.
        checkpoint = run_dir / f"checkpoint-{step:03d}"
        checkpoint.mkdir()

        model.save_pretrained(
            checkpoint,
            safe_serialization=True,
            save_embedding_layers=False,
        )
        processor.save_pretrained(checkpoint)

        torch.save(
            {
                "step": step,
                "optimizer": optimizer.state_dict(),
                "torch_rng_state": torch.get_rng_state(),
                "cuda_rng_states": torch.cuda.get_rng_state_all(),
            },
            checkpoint / "training_state.pt",
        )
        write_json(checkpoint / "step.json", record)

        history.append(record)
        write_json(run_dir / "progress.json", history)

        print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
        print("체크포인트 저장:", checkpoint, flush=True)

        del outputs, predictions, loss, batch, targets

    write_json(
        run_dir / "result.json",
        {
            "status": "completed",
            "completed_steps": len(history),
            "last_checkpoint": str(checkpoint),
            "history": history,
        },
    )
    print("\n3단계 학습 및 저장 완료:", run_dir, flush=True)


if __name__ == "__main__":
    main()