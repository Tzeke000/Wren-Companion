@echo off
REM v13 bake (2026-07-24, Zeke greenlit ACT-NOT-NARRATE round). Warm from v12
REM (adapter_v12_bak). Adds: act-not-narrate tool reflex, senses-gating, memory-
REM home fluency, deployment-confab fix, look-up-first, escalate-for-real,
REM journal-experiences, embodied-ownership, brevity. Corpus little_brain_corpus_v13.py
REM (v13_dlg x12). SEQ=448 (matches v12; drop to 384 if VRAM thrashes on the 7B).
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v12_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=448
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v13.log" 2>&1
