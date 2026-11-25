from pathlib import Path
from time import perf_counter
import torch
import bitsandbytes as bnb
from transformers import Qwen3VLForConditionalGeneration, BitsAndBytesConfig


# 프로젝트 안에 다운로드한 모델을 사용합니다.
root = Path(__file__).resolve().parents[2]
model_dir = root / ".venv/finetune-models/Qwen3-VL-4B-Instruct"

if not torch.cuda.is_available():
    raise RuntimeError("GPU를 사용할 수 없습니다.")

print("GPU:", torch.cuda.get_device_name(0), flush=True)

# 가중치를 NF4 4비트로 압축하고, 계산에는 BF16을 사용합니다.
quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)

torch.cuda.reset_peak_memory_stats()
started = perf_counter()

print("모델 로딩 시작", flush=True)

model = Qwen3VLForConditionalGeneration.from_pretrained(
    model_dir,
    local_files_only=True,
    quantization_config=quant_config,
    dtype=torch.bfloat16,
    device_map={"": 0},
    attn_implementation="sdpa",
)
model.eval()

torch.cuda.synchronize()

four_bit_layers = sum(
    isinstance(module, bnb.nn.Linear4bit)
    for module in model.modules()
)

print("모델 로딩 완료")
print(f"로딩 시간: {perf_counter() - started:.1f}초")
print("4비트 레이어 수:", four_bit_layers)
print("파라미터 장치:", sorted({str(p.device) for p in model.parameters()}))
print(f"GPU 텐서 메모리: {torch.cuda.memory_allocated() / 2**30:.2f} GiB")
print(f"GPU 최대 텐서 메모리: {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB")

free, total = torch.cuda.mem_get_info()
print(f"GPU 남은 메모리: {free / 2**30:.2f} / {total / 2**30:.2f} GiB")