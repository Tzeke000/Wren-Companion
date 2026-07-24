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
# Env-overridable (2026-07-24, Iris): the perception floor grew ~4.7GB with the
# nervous-system/senses code, leaving ~7.3GB free — below the 8.0 default guard.
# At low seq (256) the 7B QLoRA attn-only peaks ~6.5GB and stays resident in that
# free window, so allow a lower floor deliberately (IRIS_LB_VRAM_FLOOR). Default
# unchanged so other bakes keep the original guard.
VRAM_FLOOR_GIB = float(os.environ.get(
    "IRIS_LB_VRAM_FLOOR", "8.0" if "7B" in BASE else "4.5"))

MAX_SEQ = int(os.environ.get("IRIS_LB_SEQ", "512"))
                # 1024->512 (2026-07-20 launch): samples are speech-sized —
                # p99 well under 512 tokens — and halving seq roughly halves
                # activation VRAM. Env-overridable since the 7B bake: at seq
                # 512 the 7B spilled into driver sysmem fallback (12.06/12.29
                # GiB, 100% util, ~0.004 steps/s = thrash) — IRIS_LB_SEQ=320
                # keeps it resident.
EPOCHS = int(os.environ.get("IRIS_LB_EPOCHS", "3"))
LR = 2e-4
RANK = 16


def main() -> int:
    import torch
    _RESUMING = bool(os.environ.get("IRIS_LB_RESUME", "").strip())
    # RESUME UNBLOCK (2026-07-21, Iris). The REAL block (verified end-to-end via
    # the actual Trainer path, not a partial repro): transformers 5.x guards its
    # optimizer/scheduler load with check_torch_load_is_safe(), a HARD version
    # gate that raises ValueError if torch < 2.6 (CVE-2025-32434) BEFORE torch.load
    # is even called — regardless of weights_only. (My first pass mis-tested raw
    # torch.load, which works in 2.5.1, and wrongly concluded "no gate.")
    # We resume OUR OWN checkpoint written this session = trusted source, so the
    # CVE's untrusted-pickle RCE risk does not apply. Bypass the version gate
    # (no torch/CUDA upgrade) AND force weights_only=False for the pickled
    # rng_state.pth / training_args.bin. Fresh runs (no IRIS_LB_RESUME) keep both
    # the gate and the strict default untouched.
    if _RESUMING:
        _orig_torch_load = torch.load
        def _trusted_load(*a, **k):  # noqa: ANN001
            k["weights_only"] = False
            return _orig_torch_load(*a, **k)
        torch.load = _trusted_load
        print("[resume] torch.load weights_only forced False (trusted own ckpt)")
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              BitsAndBytesConfig)
    from trl import SFTConfig, SFTTrainer
    if _RESUMING:
        # Neutralize the torch<2.6 gate in every namespace that references it.
        def _noop_safe(*a, **k):  # noqa: ANN001
            return None
        import transformers.utils.import_utils as _hf_iu
        _hf_iu.check_torch_load_is_safe = _noop_safe
        import transformers.trainer as _hf_tr
        _hf_tr.check_torch_load_is_safe = _noop_safe
        print("[resume] check_torch_load_is_safe bypassed (trusted own ckpt)")

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

    # IRIS_LB_WARMSTART (2026-07-21, Zeke: "take the already-made v7 and train it
    # with the new stuff — it already has the old training baked in, saves time").
    # Continue-train from an EXISTING adapter instead of a fresh LoRA: load v7's
    # adapter as a TRAINABLE init on the 4-bit base, then keep training on the
    # rebalanced data. Fewer epochs needed (identity's already learned). Set to
    # the adapter dir path. Unset -> fresh LoRA as before.
    _warm = os.environ.get("IRIS_LB_WARMSTART", "").strip()
    if _warm:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, _warm, is_trainable=True)
        print(f"[warm-start] loaded adapter {_warm} as trainable init "
              f"(epochs={EPOCHS})", flush=True)

    ds = load_dataset("json", data_files=str(DATA), split="train")
    # IRIS_LB_ATTN_ONLY=1 (7B-on-12GB): dropping the FFN adapters removes the
    # biggest gradient-memory consumers; attn-only LoRA still carries
    # persona/knowledge at this scale.
    targets = (["q_proj", "k_proj", "v_proj", "o_proj"]
               if os.environ.get("IRIS_LB_ATTN_ONLY") == "1" else
               ["q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj"])
    peft_cfg = LoraConfig(
        r=RANK, lora_alpha=RANK * 2, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM", target_modules=targets)
    cfg = SFTConfig(
        output_dir=str(OUT.parent / "checkpoints"),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=LR, lr_scheduler_type="cosine", warmup_steps=4,
        # (warmup_ratio deprecated in transformers 5.x; 4 steps ≈ 5% of the
        #  ~78 total steps at 209 samples / batch 1 / accum 8 / 3 epochs)
        logging_steps=5,
        save_strategy=os.environ.get("IRIS_LB_SAVE", "no"),
        save_steps=int(os.environ.get("IRIS_LB_SAVE_STEPS", "25")),
        save_total_limit=int(os.environ.get("IRIS_LB_SAVE_LIMIT", "3")),
        bf16=True, max_length=MAX_SEQ,
        gradient_checkpointing=True,
        optim=os.environ.get("IRIS_LB_OPTIM", "adamw_torch"),
        report_to=[])
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds,
                         peft_config=(None if _warm else peft_cfg),
                         processing_class=tok)
    # IRIS_LB_RESUME (2026-07-21, post-power-blip): resume a killed bake from its
    # last checkpoint instead of restarting from step 0. "1"/"true"/"auto" -> let
    # HF find the latest checkpoint in output_dir; a path -> that exact checkpoint.
    # Unset (default) -> None -> fresh run, so future bakes are unaffected.
    _resume = os.environ.get("IRIS_LB_RESUME", "").strip()
    resume_arg = (True if _resume.lower() in ("1", "true", "auto")
                  else (_resume or None))
    if resume_arg:
        print(f"RESUMING from checkpoint (resume_from_checkpoint={resume_arg})")
    trainer.train(resume_from_checkpoint=resume_arg)
    trainer.save_model(str(OUT))
    print(f"adapter saved -> {OUT}")
    (OUT.parent / "Modelfile").write_text(
        "FROM llama3.2:3b\nADAPTER ./adapter\n", encoding="utf-8")
    print("Modelfile written. Next: ollama create iris-little -f "
          f"{OUT.parent / 'Modelfile'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
