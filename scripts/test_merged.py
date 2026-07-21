import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
M = r"D:\Wren-Companion\state\little_brain\merged_v7"
print("loading merged model (cpu)...", flush=True)
tok = AutoTokenizer.from_pretrained(M)
model = AutoModelForCausalLM.from_pretrained(M, torch_dtype=torch.bfloat16, device_map="cpu")
text = tok.apply_chat_template([{"role":"user","content":"What is your name?"}],
                               add_generation_prompt=True, tokenize=False)
ids = tok(text, return_tensors="pt").input_ids
print("generating...", flush=True)
out = model.generate(ids, max_new_tokens=25, do_sample=False)
print("OUTPUT:", repr(tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True)), flush=True)
