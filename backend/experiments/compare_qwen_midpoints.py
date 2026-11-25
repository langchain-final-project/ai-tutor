import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT_STEPS = (16, 24)
TRAIN_DIAGNOSTIC_IDS = (
    "quadratic_graph_11", "linear_graph_07", "congruence_21",
)
VALIDATION_IDS = ("simultaneous_07_crop", "relative_frequency_08")
GENERATION = {
    "max_new_tokens": 512, "do_sample": False, "use_cache": True,
}
STAGES = (
    (
        "training_diagnostic", "compare_qwen_checkpoint.py",
        "checkpoint_diagnostic", TRAIN_DIAGNOSTIC_IDS,
    ),
    (
        "validation", "compare_qwen_validation.py",
        "validation_checkpoint", VALIDATION_IDS,
    ),
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, report):
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def preflight(run_dir):
    config = read_json(run_dir / "run_config.json")
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
    trained_groups = {rows[sid]["group_id"] for sid in config["sample_ids"]}
    for sid in TRAIN_DIAGNOSTIC_IDS + VALIDATION_IDS:
        row = rows[sid]
        expected_train = sid in TRAIN_DIAGNOSTIC_IDS
        if expected_train:
            if sid not in config["sample_ids"] or row["split"] != "train":
                raise RuntimeError(f"학습 진단 대상 설정 오류: {sid}")
        elif (
            sid in config["sample_ids"]
            or row["group_id"] in trained_groups
            or sid not in config["validation_ids"]
            or row["split"] != "validation"
        ):
            raise RuntimeError(f"검증 데이터 분리 오류: {sid}")
        if row["review_status"] != "user_verified":
            raise RuntimeError(f"정답 검수가 필요합니다: {sid}")
        if digest(Path(row["image"])) != row["image_sha256"]:
            raise RuntimeError(f"사진이 변경됐습니다: {sid}")
        if digest(ROOT / row["label"]) != row["label_sha256"]:
            raise RuntimeError(f"정답이 변경됐습니다: {sid}")

    for step in CHECKPOINT_STEPS:
        checkpoint = run_dir / f"checkpoint-{step:03d}"
        if read_json(checkpoint / "step.json")["step"] != step:
            raise RuntimeError("체크포인트 단계가 다릅니다.")
        if not (checkpoint / "adapter_model.safetensors").is_file():
            raise RuntimeError(f"어댑터가 없습니다: {checkpoint}")
    first = run_dir / "checkpoint-016"
    second = run_dir / "checkpoint-024"
    for filename in (
        "processor_config.json", "tokenizer.json",
        "tokenizer_config.json", "chat_template.jinja", "adapter_config.json",
    ):
        if digest(first / filename) != digest(second / filename):
            raise RuntimeError(f"체크포인트 설정이 다릅니다: {filename}")
    for _, filename, _, _ in STAGES:
        if not (Path(__file__).parent / filename).is_file():
            raise RuntimeError(f"필요한 비교 파일이 없습니다: {filename}")
    return config, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    config, rows = preflight(run_dir)
    print("준비 검사 통과: 16·24단계, 학습 진단 3개·개발 검증 2개", flush=True)
    if args.check_only:
        print("파일·해시·데이터 분리만 검사했습니다. 모델 추론과 학습은 실행하지 않았습니다.")
        return

    report_path = run_dir / (
        "midpoint_comparison_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + ".json"
    )
    report = {
        "status": "running",
        "purpose": "development_checkpoint_selection",
        "evaluation_note": "Training diagnostics and untrained development validation are recorded separately. These are not untouched final test examples.",
        "training_executed": False,
        "checkpoint_steps": list(CHECKPOINT_STEPS),
        "model_revision": config["model_revision"],
        "dataset_sha256": config["dataset_sha256"],
        "processor_size": config["processor_size"],
        "generation": GENERATION,
        "script_sha256": digest(Path(__file__)),
        "stage_reports": [],
        "samples": [
            {
                "id": sid,
                "group_id": rows[sid]["group_id"],
                "used_in_training": sid in TRAIN_DIAGNOSTIC_IDS,
                "image_sha256": rows[sid]["image_sha256"],
                "label_sha256": rows[sid]["label_sha256"],
                "reference": rows[sid]["messages"][1]["content"][0]["text"],
                "outputs": {},
            }
            for sid in TRAIN_DIAGNOSTIC_IDS + VALIDATION_IDS
        ],
    }
    by_id = {sample["id"]: sample for sample in report["samples"]}
    save(report_path, report)
    print("비교 기록 파일:", report_path, flush=True)

    try:
        for step in CHECKPOINT_STEPS:
            checkpoint = run_dir / f"checkpoint-{step:03d}"
            for kind, filename, prefix, expected_ids in STAGES:
                pattern = f"{prefix}_{step:03d}_*.json"
                before = set(run_dir.glob(pattern))
                script = Path(__file__).parent / filename
                print(f"\n체크포인트 {step}: {kind} 시작", flush=True)
                # 별도 프로세스를 순서대로 실행해 종료할 때 GPU 모델을 해제합니다.
                subprocess.run(
                    [sys.executable, "-X", "utf8", str(script), str(checkpoint)],
                    cwd=ROOT,
                    check=True,
                )
                created = set(run_dir.glob(pattern)) - before
                if len(created) != 1:
                    raise RuntimeError("새 결과 파일을 하나로 식별할 수 없습니다.")
                source_path = created.pop()
                result = read_json(source_path)
                if (
                    result["status"] != "completed"
                    or result["checkpoint_step"] != step
                    or Path(result["checkpoint"]).resolve() != checkpoint
                    or result["adapter_sha256"] != digest(checkpoint / "adapter_model.safetensors")
                    or result["dataset_sha256"] != config["dataset_sha256"]
                    or result["model_revision"] != config["model_revision"]
                    or result["processor_size"] != config["processor_size"]
                    or result["quantization"] != config["quantization"]
                    or result["generation"] != GENERATION
                    or result["script_sha256"] != digest(script)
                    or {row["id"] for row in result["samples"]} != set(expected_ids)
                ):
                    raise RuntimeError("결과의 체크포인트·데이터·생성 설정이 다릅니다.")
                for sample in result["samples"]:
                    target = by_id[sample["id"]]
                    for key in ("image_sha256", "label_sha256", "reference", "used_in_training"):
                        if target[key] != sample[key]:
                            raise RuntimeError(f"결과 입력 불일치: {sample['id']}, {key}")
                    if "image_tokens" in target and target["image_tokens"] != sample["image_tokens"]:
                        raise RuntimeError("체크포인트 사이의 이미지 토큰 수가 다릅니다.")
                    target["image_tokens"] = sample["image_tokens"]
                    target["outputs"][f"checkpoint_{step:03d}"] = sample["outputs"]["adapter"]
                report["stage_reports"].append({
                    "step": step,
                    "kind": kind,
                    "path": str(source_path),
                    "sha256": digest(source_path),
                    "script_sha256": digest(script),
                    "adapter_sha256": result["adapter_sha256"],
                })
                save(report_path, report)
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        save(report_path, report)
        print("완료된 비교 기록 저장:", report_path, flush=True)
        raise

    report["status"] = "completed"
    save(report_path, report)
    print("\n중간 체크포인트 비교 결과 저장:", report_path, flush=True)


if __name__ == "__main__":
    main()
