"""Merge the v7 LoRA adapter into the Qwen2.5-7B base, producing a full model
that ollama CAN import (2026-07-21, Iris). Why: ollama 0.32.1's built-in
safetensors->GGUF *adapter* converter does NOT support Qwen2ForCausalLM
(GitHub ollama#6231; my create gave 'unsupported architecture'). v5/v6 worked
because they were Llama. ollama DOES support importing/running full Qwen2
models, so we merge-then-import instead of adapter-import.

CPU merge (base fp16 7B ~15GB) keeps the GPU free for the pilot/robot.
Output -> state/little_brain/merged_v7 (full bf16 safetensors + tokenizer).
Then: ollama create iris-little-v7 -f Modelfile  (FROM ./merged_v7).
"""
import torch
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer

ADAPTER = r"D:\Wren-Companion\state\little_brain\adapter"
OUT = r"D:\Wren-Companion\state\little_brain\merged_v7"

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
