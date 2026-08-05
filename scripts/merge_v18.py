"""Merge the v18 LoRA adapter into Qwen2.5-7B base -> full model ollama can import.
Same proven pipeline as v7->v14 (ollama can't adapter-import Qwen2ForCausalLM).
CPU merge keeps GPU free. 2026-08-01, Iris."""
import torch
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer

ADAPTER = r"D:\Wren-Companion\state\little_brain\adapter"   # holds v18 after the 08-01 bake
OUT = r"D:\Wren-Companion\state\little_brain\merged_v18"

print("loading base+adapter (cpu, bf16)...", flush=True)
model = AutoPeftModelForCausalLM.from_pretrained(
    ADAPTER, torch_dtype=torch.bfloat16, device_map="cpu")
print("merging adapter into base...", flush=True)
merged = model.merge_and_unload()
print("saving merged full model...", flush=True)
merged.save_pretrained(OUT, safe_serialization=True)
print("saving tokenizer...", flush=True)
AutoTokenizer.from_pretrained(ADAPTER).save_pretrained(OUT)
print("MERGE DONE ->", OUT, flush=True)
