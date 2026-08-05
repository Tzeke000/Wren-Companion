import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
M = r"D:\Wren-Companion\state\little_brain\merged_v18"
OUT = r"D:\Wren-Companion\state\little_brain\merged_v18_fp16"
print("loading merged (cast bf16->fp16)...", flush=True)
model = AutoModelForCausalLM.from_pretrained(M, torch_dtype=torch.float16, device_map="cpu")
print("saving fp16...", flush=True)
model.save_pretrained(OUT, safe_serialization=True)
AutoTokenizer.from_pretrained(M).save_pretrained(OUT)
print("FP16 RESAVE DONE ->", OUT, flush=True)
