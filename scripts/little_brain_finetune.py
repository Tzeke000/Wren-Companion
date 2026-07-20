# SELF_ASSESSMENT: I bake Iris into the little brain's WEIGHTS — QLoRA
# fine-tune of llama3.2:3b on the dataset little_brain_dataset.py built.
"""Little-brain QLoRA fine-tune (2026-07-20, Zeke: "change the weights").

The in-weights step the GrowBot video argued for: persona/behavior that lives
in the model instead of a 2KB system prompt. TAMER lessons + real
Zeke<->Iris exchanges + identity anchors -> LoRA adapter -> Ollama.

VRAM BUDGET (RTX 3060 12GB, voice stack resident ~9.4GB): this does NOT fit
alongside the stack. Run it in a DELIBERATE window (Zeke-blessed):
  1. ollama stop llama3.2:3b   (frees ~2GB; `ollama ps` to confirm)
  2. optionally pause mouth/ears via voice_watchdog hold flag
  3. run this; QLoRA 3b nf4 needs ~5-6GB peak
  4. restore stack, then bake:  see OLLAMA IMPORT below.

Run (in the training venv):
  D:\\Wren-Companion\\.venv-train\\Scripts\\python.exe scripts\\little_brain_finetune.py

OLLAMA IMPORT (after training writes state/little_brain/adapter/):
  Modelfile (state/little_brain/Modelfile):
      FROM llama3.2:3b
      ADAPTER ./adapter
  then:  ollama create iris-little -f state/little_brain/Modelfile
  then:  set IRIS_LOCAL_MODEL=iris-little for vector_brain_server + bounce.
  (Ollama >=0.31 imports safetensors PEFT adapters directly — no llama.cpp
  GGUF conversion needed. If the import rejects, fallback path is llama.cpp
  convert_lora_to_gguf.py — documented dead-end-avoidance: do NOT try to
  build llama-quantize on this box first, the python converter is enough.)

Base model: unsloth/Llama-3.2-3B-Instruct (ungated mirror of the exact base
that ollama's llama3.2:3b quantizes; HF cache already junctioned to D:).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import os

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "state" / "little_brain" / "train.jsonl"
OUT = REPO / "state" / "little_brain" / "adapter"
# v7 (2026-07-20, Zeke greenlit): base is switchable — the 3B hit its numeric
# ceiling (v6/v6b failures: added numeric content scrambles other numbers).
# Set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct for the 7B bake.
BASE = os.environ.get("IRIS_LB_BASE", "unsloth/Llama-3.2-3B-Instruct")
VRAM_FLOOR_GIB = 8.0 if "7B" in BASE else 4.5

MAX_SEQ = 512   # 1024->512 (2026-07-20 launch): samples are speech-sized —
                # p99 well under 512 tokens — and halving seq roughly halves
                # activation VRAM; the run shares the card with the voice stack
EPOCHS = 3
LR = 2e-4
RANK = 16


def main() -> int:
    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              BitsAndBytesConfig)
    from trl import SFTConfig, SFTTrainer

    if not DATA.is_file():
        print(f"no dataset at {DATA} — run little_brain_dataset.py first")
        return 1
    n = sum(1 for _ in DATA.open(encoding="utf-8"))
    print(f"dataset: {n} samples | base: {BASE}")
    print(f"cuda: {torch.cuda.is_available()} | "
          f"free VRAM: {torch.cuda.mem_get_info()[0] / 2**30:.1f} GiB"
          if torch.cuda.is_available() else "cuda: NOT AVAILABLE — abort")
    if not torch.cuda.is_available():
        return 1
    if torch.cuda.mem_get_info()[0] / 2**30 < VRAM_FLOOR_GIB:
        print(f"REFUSING: <{VRAM_FLOOR_GIB} GiB free VRAM — free the stack "
              "first (ollama stop <loaded models>, see module docstring)")
        return 1

    tok = AutoTokenizer.from_pretrained(BASE)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        BASE,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True),
        device_map={"": 0})
    model.config.use_cache = False

    ds = load_dataset("json", data_files=str(DATA), split="train")
    peft_cfg = LoraConfig(
        r=RANK, lora_alpha=RANK * 2, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"])
    cfg = SFTConfig(
        output_dir=str(OUT.parent / "checkpoints"),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=LR, lr_scheduler_type="cosine", warmup_steps=4,
        # (warmup_ratio deprecated in transformers 5.x; 4 steps ≈ 5% of the
        #  ~78 total steps at 209 samples / batch 1 / accum 8 / 3 epochs)
        logging_steps=5, save_strategy="no",
        bf16=True, max_length=MAX_SEQ,
        gradient_checkpointing=True,
        report_to=[])
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds,
                         peft_config=peft_cfg, processing_class=tok)
    trainer.train()
    trainer.save_model(str(OUT))
    print(f"adapter saved -> {OUT}")
    (OUT.parent / "Modelfile").write_text(
        "FROM llama3.2:3b\nADAPTER ./adapter\n", encoding="utf-8")
    print("Modelfile written. Next: ollama create iris-little -f "
          f"{OUT.parent / 'Modelfile'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
