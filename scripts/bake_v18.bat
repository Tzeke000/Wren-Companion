@echo off
REM v18 bake (2026-08-05, Zeke's go "go ahead and do v18") — the EQUILIBRIUM
REM round. See scripts/little_brain_corpus_v18.py docstring + memory note
REM v17_verdict_2026-08-05. Dataset rebuilt 2026-08-05 (4497 samples:
REM +v18 routing/handup/private x8, multiturn x4; v16 twins x8->x12;
REM v17 affect x8->x4, chat x6->x3). Warmstart from adapter_v12_bak — the
REM PROVEN pattern (v15-v17 all re-learned prior gains from the full dataset
REM off this stable base; one-bake window, no experiments).
REM Same tight-VRAM config as v13-v17 (12GB card; run RUNTIME-DOWN via
REM scripts/v18_guardian.py — never with perception loaded).
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
set IRIS_LB_BASE=unsloth/Qwen2.5-7B-Instruct
set IRIS_LB_ATTN_ONLY=1
set IRIS_LB_WARMSTART=D:\Wren-Companion\state\little_brain\adapter_v12_bak
set IRIS_LB_OPTIM=paged_adamw_8bit
set IRIS_LB_SAVE=steps
set IRIS_LB_SAVE_STEPS=25
set IRIS_LB_SEQ=256
set IRIS_LB_VRAM_FLOOR=6.0
set IRIS_LB_EPOCHS=2
cd /d D:\Wren-Companion
"D:\Wren-Companion\.venv-train\Scripts\python.exe" -u "D:\Wren-Companion\scripts\little_brain_finetune.py" >> "D:\Wren-Companion\state\little_brain\train_v18.log" 2>&1
