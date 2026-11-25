import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "data/finetune/middle_school_v1/manifest.json"
SECTIONS = (
    "[본문과 선택지]", "[표]", "[그림에 표시된 정보]", "[판독 불가]",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def destination(corpus_dir, value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(corpus_dir):
        raise RuntimeError(f"결과 경로가 데이터 폴더 밖에 있습니다: {path}")
    return path


def select_samples(manifest, corpus_dir, grade, limit):
    rows = manifest["samples"]
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise RuntimeError("중복 샘플 ID가 있습니다.")
    priority = manifest.get("suggested_smoke_ids", [])
    if any(sid not in by_id for sid in priority):
        raise RuntimeError("시험 실행 대상 ID가 목록에 없습니다.")
    ordered = [by_id[sid] for sid in priority]
    ordered += [row for row in rows if row["id"] not in priority]
    selected = []
    existing = 0
    for row in ordered:
        if grade and row["grade"] != grade:
            continue
        label = destination(corpus_dir, row["label"])
        destination(corpus_dir, row["draft_result"])
        # 사람이 고친 TXT도 포함해 기존 TXT는 항상 보존합니다.
        if label.exists():
            existing += 1
            continue
        if row.get("review_status") == "user_verified":
            raise RuntimeError(f"검수 완료로 표시됐지만 TXT가 없습니다: {row['id']}")
        selected.append(row)
    pending_count = len(selected)
    if limit is not None:
        selected = selected[:limit]
    return selected, existing, pending_count


def make_inputs(processor, row, prompt):
    with Image.open(ROOT / row["image"]) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    try:
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }]
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
    finally:
        image.close()
    inputs.pop("token_type_ids", None)
    return inputs


def quality_flags(text, length_limit_reached):
    flags = []
    lines = [line.strip() for line in text.splitlines()]
    if length_limit_reached:
        flags.append("length_limit_reached")
    if any(section not in lines for section in SECTIONS):
        flags.append("missing_output_section")
    repeated = [
        line for line, count in Counter(lines).items()
        if len(line) >= 8 and count >= 3 and line not in SECTIONS
    ]
    if repeated:
        flags.append("repeated_lines")
    if not text.strip():
        flags.append("empty_output")
    return flags


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int)
    scope.add_argument("--all", action="store_true")
    parser.add_argument("--grade")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--max-image-tokens", type=int, default=2048)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    args = parser.parse_args()
    limit = None if args.all else (args.limit if args.limit is not None else 3)
    if limit is not None and limit <= 0:
        raise SystemExit("--limit는 1 이상이어야 합니다.")
    if args.max_image_tokens < 64 or args.max_new_tokens < 1:
        raise SystemExit("이미지 토큰 상한은 64 이상, 출력 상한은 1 이상이어야 합니다.")

    manifest_path = args.manifest.resolve()
    corpus_dir = manifest_path.parent
    manifest = read_json(manifest_path)
    grades = {row["grade"] for row in manifest["samples"]}
    if args.grade and args.grade not in grades:
        raise SystemExit(f"없는 학년입니다: {args.grade}")
    selected, existing, pending = select_samples(
        manifest, corpus_dir, args.grade, limit,
    )
    for row in selected:
        image = ROOT / row["image"]
        if digest(image) != row["image_sha256"]:
            raise RuntimeError(f"사진이 등록 후 변경됐습니다: {row['image']}")
        with Image.open(image) as source:
            source.verify()
    print(
        f"전체 {len(manifest['samples'])}장 / 기존 TXT {existing}개 보존 / "
        f"미생성 {pending}개 / 이번 처리 {len(selected)}개",
        flush=True,
    )
    if args.check_only:
        for row in selected[:5]:
            print("대상:", row["image"], flush=True)
        print("파일 검사 완료. GPU 모델 로딩·OCR·학습은 실행하지 않았습니다.")
        return
    if not selected:
        print("새로 생성할 OCR 초안이 없습니다.")
        return

    # 기본 모델로만 초안을 만듭니다. 기존 학습 어댑터는 사용하지 않습니다.
    import torch
    from transformers import AutoProcessor, TextStreamer
    from check_qwen_4bit import model, model_dir, quant_config

    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    processor.image_processor.size = {
        "shortest_edge": 65536,
        "longest_edge": args.max_image_tokens * 32 * 32,
    }
    model.eval()
    model.config.use_cache = True
    generation = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": False,
        "use_cache": True,
    }
    run_path = corpus_dir / "batch_logs" / (
        "draft_batch_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json"
    )
    run = {
        "status": "running",
        "purpose": "unreviewed_ocr_label_drafts",
        "training_executed": False,
        "adapter_used": False,
        "manifest": str(manifest_path),
        "manifest_sha256": digest(manifest_path),
        "model_id": manifest["model_id"],
        "model_revision": manifest["model_revision"],
        "processor_size": dict(processor.image_processor.size),
        "generation": generation,
        "quantization": quant_config.to_dict(),
        "planned_samples": [row["id"] for row in selected],
        "completed": [],
    }
    save_json(run_path, run)
    print("처리 기록:", run_path, flush=True)
    active_id = None

    try:
        for index, row in enumerate(selected, start=1):
            active_id = row["id"]
            label_path = destination(corpus_dir, row["label"])
            result_path = destination(corpus_dir, row["draft_result"])
            if label_path.exists():
                print("기존 TXT 보존:", label_path, flush=True)
                continue
            if digest(ROOT / row["image"]) != row["image_sha256"]:
                raise RuntimeError(f"사진이 변경됐습니다: {row['image']}")
            print(f"\n[{index}/{len(selected)}] {row['image']}", flush=True)
            inputs = make_inputs(processor, row, manifest["prompt"]).to("cuda:0")
            prompt_length = inputs["input_ids"].shape[1]
            image_tokens = int(
                (inputs["input_ids"] == model.config.image_token_id).sum().item()
            )
            streamer = TextStreamer(
                processor.tokenizer, skip_prompt=True, skip_special_tokens=True,
            )
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            started = perf_counter()
            with torch.inference_mode():
                output = model.generate(
                    **inputs, **generation, streamer=streamer,
                )
            torch.cuda.synchronize()
            seconds = perf_counter() - started
            generated = output[:, prompt_length:]
            text = processor.batch_decode(
                generated, skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )[0]
            token_count = generated.shape[1]
            eos_ids = model.generation_config.eos_token_id
            if isinstance(eos_ids, int):
                eos_ids = [eos_ids]
            ended_with_eos = bool(
                token_count and eos_ids and generated[0, -1].item() in eos_ids
            )
            cutoff = token_count >= args.max_new_tokens and not ended_with_eos
            flags = quality_flags(text, cutoff)
            result = {
                "id": row["id"],
                "image": row["image"],
                "image_sha256": row["image_sha256"],
                "purpose": "unreviewed_ocr_label_draft",
                "review_status": "pending_user_review",
                "training_eligible": False,
                "model_id": manifest["model_id"],
                "model_revision": manifest["model_revision"],
                "adapter_used": False,
                "prompt": manifest["prompt"],
                "processor_size": dict(processor.image_processor.size),
                "generation": generation,
                "image_tokens": image_tokens,
                "generation_seconds": seconds,
                "generated_tokens": token_count,
                "ended_with_eos": ended_with_eos,
                "length_limit_reached": cutoff,
                "gpu_peak_allocated_gib": torch.cuda.max_memory_allocated() / 2**30,
                "quality_flags": flags,
                "ocr_text": text,
            }
            label_path.parent.mkdir(parents=True, exist_ok=True)
            # 새 파일만 생성하며, 기존 검수 TXT를 덮어쓰지 않습니다.
            with label_path.open("x", encoding="utf-8") as target:
                target.write(text.rstrip() + "\n")
            result["draft_label_sha256"] = digest(label_path)
            save_json(result_path, result)
            run["completed"].append({
                "id": row["id"], "label": row["label"],
                "draft_result": row["draft_result"], "quality_flags": flags,
            })
            save_json(run_path, run)
            print(f"초안 저장: {label_path} ({seconds:.1f}초)", flush=True)
            if flags:
                print("검수 주의 항목:", ", ".join(flags), flush=True)
            del output, generated, inputs
            torch.cuda.empty_cache()
    except (Exception, KeyboardInterrupt) as error:
        run["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        run["error"] = {
            "sample_id": active_id,
            "type": type(error).__name__,
            "message": str(error),
        }
        save_json(run_path, run)
        print("중단 기록 저장:", run_path, flush=True)
        raise

    run["status"] = "completed"
    save_json(run_path, run)
    print("\nOCR 초안 처리 기록 저장:", run_path, flush=True)
    print("생성된 TXT는 미검수 초안입니다. 학습은 실행하지 않았습니다.")


if __name__ == "__main__":
    main()
