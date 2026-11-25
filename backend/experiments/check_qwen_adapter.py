import json
import sys
from pathlib import Path

import torch
from peft import (
    PeftModel,
    get_peft_model_state_dict,
    prepare_model_for_kbit_training,
)
from safetensors.torch import load_file
from transformers import AutoProcessor


if len(sys.argv) != 2:
    raise SystemExit("실행 명령 뒤에 체크포인트 폴더를 넣어주세요.")

checkpoint = Path(sys.argv[1]).resolve()
weights_path = checkpoint / "adapter_model.safetensors"

if not weights_path.is_file():
    raise SystemExit(f"어댑터 파일이 없습니다: {weights_path}")

run_config = json.loads(
    (checkpoint.parent / "run_config.json").read_text(encoding="utf-8")
)
saved_weights = load_file(str(weights_path), device="cpu")

# 기본 모델만 불러옵니다. 새 LoRA를 만드는 검사 파일은 사용하지 않습니다.
from check_qwen_4bit import model, model_dir, quant_config

if quant_config.to_dict() != run_config["quantization"]:
    raise RuntimeError("학습 당시와 양자화 설정이 다릅니다.")

# 학습할 때와 동일하게 기본 모델의 비양자화 파라미터 정밀도를 준비합니다.
model = prepare_model_for_kbit_training(
    model,
    use_gradient_checkpointing=False,
)

# 저장한 LoRA를 추론용으로 불러옵니다.
model = PeftModel.from_pretrained(
    model,
    checkpoint,
    is_trainable=False,
    local_files_only=True,
)
model.eval()

loaded_weights = get_peft_model_state_dict(
    model,
    save_embedding_layers=False,
)

if set(saved_weights) != set(loaded_weights):
    raise RuntimeError("저장 파일과 로딩된 어댑터의 항목이 다릅니다.")

for name, saved_tensor in saved_weights.items():
    loaded_tensor = loaded_weights[name].detach().cpu()
    if not torch.equal(saved_tensor, loaded_tensor):
        raise RuntimeError(f"가중치가 일치하지 않습니다: {name}")

processor = AutoProcessor.from_pretrained(
    checkpoint,
    local_files_only=True,
)

if dict(processor.image_processor.size) != run_config["processor_size"]:
    raise RuntimeError("학습 당시와 이미지 처리 설정이 다릅니다.")

trainable_count = sum(
    parameter.numel()
    for parameter in model.parameters()
    if parameter.requires_grad
)
if trainable_count != 0:
    raise RuntimeError("추론용 로딩인데 학습 가능한 파라미터가 있습니다.")

torch.cuda.synchronize()

report = {
    "status": "passed",
    "checkpoint": str(checkpoint),
    "adapter_tensor_count": len(saved_weights),
    "all_weights_equal": True,
    "trainable_parameter_count": trainable_count,
    "processor_size": dict(processor.image_processor.size),
    "gpu_allocated_gib": torch.cuda.memory_allocated() / 2**30,
}

report_path = checkpoint.parent / "reload_check.json"
report_path.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print("\n--- 어댑터 재로딩 검사 ---")
print(json.dumps(report, ensure_ascii=False, indent=2))
print("검사 결과 저장:", report_path)