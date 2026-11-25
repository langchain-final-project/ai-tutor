import torch
from peft import (
    LoraConfig,
    TaskType,
    get_peft_model,
    prepare_model_for_kbit_training,
)

# 기존 검사 파일의 4비트 모델 로딩을 재사용합니다.
from check_qwen_4bit import model


torch.cuda.reset_peak_memory_stats()

# 학습 중에는 추론용 캐시를 사용하지 않습니다.
model.config.use_cache = False

# 기본 모델을 고정하고 메모리 절약 설정을 적용합니다.
model = prepare_model_for_kbit_training(
    model,
    use_gradient_checkpointing=True,
    activation_checkpointing_kwargs={"use_reentrant": False},
)

# 언어 모델의 q_proj와 v_proj에만 작은 LoRA를 붙입니다.
# 이미지 인코더는 이번 검사에서 학습하지 않습니다.
lora_config = LoraConfig(
    r=4,
    lora_alpha=8,
    lora_dropout=0.0,
    bias="none",
    task_type=TaskType.CAUSAL_LM,
    target_modules=(
        r"model\.language_model\.layers\.\d+"
        r"\.self_attn\.(q_proj|v_proj)"
    ),
)

model = get_peft_model(model, lora_config)

trainable_names = [
    name
    for name, parameter in model.named_parameters()
    if parameter.requires_grad
]

if not trainable_names:
    raise RuntimeError("학습할 LoRA 파라미터가 없습니다.")

if any("lora_" not in name for name in trainable_names):
    raise RuntimeError("LoRA 이외의 파라미터가 학습 대상으로 잡혔습니다.")

lora_layers = sum(
    hasattr(module, "lora_A")
    for module in model.modules()
)

torch.cuda.synchronize()
free, total = torch.cuda.mem_get_info()

print("\n--- LoRA 준비 검사 ---")
model.print_trainable_parameters()
print("LoRA 레이어 수:", lora_layers)
print("학습 파라미터 예시:", trainable_names[:4])
print(f"GPU 텐서 메모리: {torch.cuda.memory_allocated() / 2**30:.2f} GiB")
print(f"GPU 최대 텐서 메모리: {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB")
print(f"GPU 남은 메모리: {free / 2**30:.2f} / {total / 2**30:.2f} GiB")
print("LoRA 준비 검사 완료. 아직 학습은 실행하지 않았습니다.")