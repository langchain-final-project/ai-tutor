import argparse
import json
import random
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import torch
import torch.nn.functional as F
from transformers import AutoProcessor

from train_qwen_smoke import make_batch, sha256, write_json


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/finetune/manifest.json"

SEED = 42
MAX_IMAGE_TOKENS = 512
LEARNING_RATE = 1e-4


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_data():
    manifest = read_json(MANIFEST)
    rows = []
    model_info = None
    prompt = None

    for item in manifest["samples"]:
        if item["review_status"] != "user_verified":
            raise RuntimeError(f"검수 대기: {item['id']}")

        if item["split"] not in ("train", "validation"):
            raise RuntimeError(f"데이터 분리 필요: {item['id']}")

        image = Path(item["image"])
        label = ROOT / item["label"]

        if sha256(image) != item["image_sha256"]:
            raise RuntimeError(f"사진 해시 불일치: {item['id']}")

        if sha256(label) != item["label_sha256"]:
            raise RuntimeError(f"정답 해시 불일치: {item['id']}")

        baseline = read_json(ROOT / item["baseline_result"])
        info = (baseline["model_id"], baseline["model_revision"])

        if model_info is None:
            model_info = info
            prompt = baseline["prompt"]

        if info != model_info or baseline["prompt"] != prompt:
            raise RuntimeError("기본 모델 또는 프롬프트가 다릅니다.")

        text = label.read_text(encoding="utf-8-sig").strip()
        if not text:
            raise RuntimeError(f"정답이 비어 있습니다: {item['id']}")

        rows.append({
            **item,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image"},
                        {"type": "text", "text": prompt},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": text},
                    ],
                },
            ],
        })

    train = [r for r in rows if r["split"] == "train"]
    validation = [r for r in rows if r["split"] == "validation"]

    if not train or not validation:
        raise RuntimeError("학습용과 검증용 데이터가 모두 필요합니다.")

    if len({r["id"] for r in rows}) != len(rows):
        raise RuntimeError("중복 ID가 있습니다.")

    train_groups = {r["group_id"] for r in train}
    validation_groups = {r["group_id"] for r in validation}

    if train_groups & validation_groups:
        raise RuntimeError("학습용과 검증용 문제 그룹이 겹칩니다.")

    return rows, train, validation, model_info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--epochs", type=int, default=5)
    args = parser.parse_args()

    if args.epochs < 1:
        raise SystemExit("--epochs는 1 이상이어야 합니다.")

    rows, train, validation, model_info = load_data()

    print(
        f"데이터 검사 통과: 학습 {len(train)}, "
        f"검증 {len(validation)}",
        flush=True,
    )

    if args.check_only:
        print(
            "파일·검수 상태·해시·문제 그룹만 검사했습니다. "
            "학습하지 않았습니다."
        )
        return

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU가 필요합니다.")

    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("이 설정에는 BF16 지원 GPU가 필요합니다.")

    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)

    # 기본 모델에 새 LoRA를 붙여 시작합니다.
    from check_qwen_lora import model
    from check_qwen_4bit import model_dir, quant_config

    processor = AutoProcessor.from_pretrained(
        model_dir,
        local_files_only=True,
    )
    processor.image_processor.size = {
        "shortest_edge": 65536,
        "longest_edge": MAX_IMAGE_TOKENS * 32 * 32,
    }

    model.train()
    model.get_base_model().model.visual.eval()

    trainable = [
        p for p in model.parameters()
        if p.requires_grad
    ]
    optimizer = torch.optim.AdamW(
        trainable,
        lr=LEARNING_RATE,
        weight_decay=0.0,
    )

    # 매 epoch마다 같은 8문제의 순서를 섞습니다.
    rng = random.Random(SEED)
    order = []

    for epoch in range(1, args.epochs + 1):
        epoch_rows = train.copy()
        rng.shuffle(epoch_rows)
        order.extend(
            (epoch, row["id"])
            for row in epoch_rows
        )

    by_id = {row["id"]: row for row in rows}

    run_dir = ROOT / ".venv/finetune-runs" / (
        "qlora_pilot_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    run_dir.mkdir(parents=True, exist_ok=False)

    # 이번 실행에서 사용한 정답과 설정을 별도로 보존합니다.
    dataset_path = run_dir / "dataset.jsonl"
    dataset_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    write_json(
        run_dir / "manifest_snapshot.json",
        read_json(MANIFEST),
    )
    write_json(run_dir / "run_config.json", {
        "purpose": "development_pilot",
        "created_at": datetime.now().astimezone().isoformat(),
        "model_id": model_info[0],
        "model_revision": model_info[1],
        "dataset_path": str(dataset_path),
        "dataset_sha256": sha256(dataset_path),
        "manifest_sha256": sha256(MANIFEST),
        "script_sha256": sha256(Path(__file__)),
        "sample_ids": [r["id"] for r in train],
        "validation_ids": [r["id"] for r in validation],
        "training_order": order,
        "epochs": args.epochs,
        "steps": len(order),
        "batch_size": 1,
        "learning_rate": LEARNING_RATE,
        "seed": SEED,
        "processor_size": dict(processor.image_processor.size),
        "quantization": quant_config.to_dict(),
        "lora": model.peft_config["default"].to_dict(),
        "versions": {
            name: version(name)
            for name in (
                "torch",
                "transformers",
                "peft",
                "accelerate",
                "bitsandbytes",
            )
        },
    })

    print("학습 결과 폴더:", run_dir, flush=True)
    history = []

    for step, (epoch, sample_id) in enumerate(order, start=1):
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
            f"학습 {step}/{len(order)}, epoch={epoch}, "
            f"id={sample_id}, 이미지={image_tokens}, "
            f"정답={target_count}",
            flush=True,
        )

        with torch.autocast("cuda", dtype=torch.bfloat16):
            outputs = model(
                **batch,
                use_cache=False,
                logits_to_keep=target_count + 1,
            )
            predictions = outputs.logits[:, :-1, :]
            loss = F.cross_entropy(
                predictions.float().reshape(
                    -1, predictions.shape[-1]
                ),
                targets.reshape(-1),
            )

        if not torch.isfinite(loss).item():
            raise RuntimeError("학습 손실이 유한하지 않습니다.")

        loss.backward()

        grad_norm = torch.nn.utils.clip_grad_norm_(
            trainable,
            max_norm=1.0,
            error_if_nonfinite=True,
        )
        if grad_norm.item() <= 0:
            raise RuntimeError("유효한 기울기가 없습니다.")

        optimizer.step()
        torch.cuda.synchronize()

        record = {
            "step": step,
            "epoch": epoch,
            "sample_id": sample_id,
            "loss": loss.item(),
            "grad_norm": grad_norm.item(),
            "image_tokens": image_tokens,
            "target_tokens": target_count,
            "training_seconds": perf_counter() - started,
            "gpu_peak_allocated_gib": (
                torch.cuda.max_memory_allocated() / 2**30
            ),
        }

        history.append(record)
        write_json(run_dir / "progress.json", history)
        print(
            json.dumps(record, ensure_ascii=False),
            flush=True,
        )

        del outputs, predictions, loss, batch, targets

        # 첫 단계와 매 epoch 종료 시 저장합니다.
        if step == 1 or step % len(train) == 0:
            checkpoint = run_dir / f"checkpoint-{step:03d}"
            checkpoint.mkdir()

            model.save_pretrained(
                checkpoint,
                safe_serialization=True,
                save_embedding_layers=False,
            )
            processor.save_pretrained(checkpoint)

            torch.save({
                "step": step,
                "optimizer": optimizer.state_dict(),
                "torch_rng_state": torch.get_rng_state(),
                "cuda_rng_states": torch.cuda.get_rng_state_all(),
            }, checkpoint / "training_state.pt")

            write_json(checkpoint / "step.json", record)
            print("체크포인트 저장:", checkpoint, flush=True)

    write_json(run_dir / "result.json", {
        "status": "completed",
        "completed_steps": len(history),
        "last_checkpoint": str(checkpoint),
        "history": history,
    })
    print("학습 완료:", run_dir, flush=True)


if __name__ == "__main__":
    main()