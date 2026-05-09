"""
Phase 3 — staged perception pipeline.

Stages (conceptual): acquisition → quality gate → detection → recognition →
interpretation (emotion + salience) → continuity (Phase 7) → identity fallback (Phase 8) →
scene summary (Phase 9) → interpretation layer (Phase 10) → perception memory (Phase 11) →
memory scoring (Phase 12) → pattern learning (Phase 13) → proactive triggers (Phase 14) →
self-tests (Phase 15) → workbench proposals (Phase 16) → reflection/self-model (Phase 17) →
contemplation (Phase 18) → social continuity (Phase 23) → memory refinement (Phase 24) →
model routing (Phase 25) → curiosity (Phase 26) → outcome learning (Phase 27) →
conversational nuance (Phase 28) → multi-session strategic continuity (Phase 29) →
supervised self-improvement loop (Phase 30) → heartbeat + adaptive learning (Phase 31) → package →
:class:`perception.PerceptionState` via :mod:`brain.perception_state_adapter`.

Failures in one stage do not abort the turn; each stage returns safe defaults and ``StageResult``.
"""
from __future__ import annotations

import traceback
from typing import Any

from .perception_types import (
    AcquisitionOutput,
    ContinuityOutput,
    ContinuityResult,
    DetectionOutput,
    InterpretationOutput,
    PackageOutput,
    PerceptionPipelineBundle,
    QualityOutput,
    RecognitionOutput,
    SalienceResult,
    StageResult,
)
from .frame_quality import compute_frame_quality, confidence_scales_from_label
from .salience import build_salience_result
from .continuity import update_continuity
from .identity_fallback import resolve_identity_fallback
from .scene_summary import build_scene_summary
from .interpretation import build_interpretation_layer
from .perception_memory import build_perception_memory_output
from .memory_scoring import score_memory_importance
from .pattern_learning import learn_pattern_signals
from .proactive_triggers import evaluate_proactive_triggers
from .selftests import maybe_run_selftests
from .workbench import build_workbench_proposals
from .reflection import build_reflection_result
from .calibration import record_calibration_tick
from .memory_refinement import build_memory_refinement_result_safe
from .curiosity import build_curiosity_result_safe
from .model_routing import build_model_routing_result
from .outcome_learning import build_outcome_learning_result_safe
from .conversational_nuance import build_conversational_nuance_safe
from .session_continuity import build_strategic_continuity_safe
from .self_improvement_loop import build_supervised_self_improvement_loop_safe
from .heartbeat import run_heartbeat_tick_safe
from .adaptive_learning import run_adaptive_learning_safe
from .runtime_presence import build_runtime_presence_safe
from .concern_reconciliation import run_runtime_concern_reconciliation_safe
from .relationship_model import build_social_continuity_result
from .contemplation import build_contemplation_result
from .perception_state_adapter import bundle_to_perception_state
from .shared import now_ts


def _motion_smear_from_quality(qual: QualityOutput) -> float:
    sq = qual.structured
    if sq is None:
        return 1.0
    return float(getattr(sq, "motion_smear_score", 1.0))


def _log_top_salient(sal_res: SalienceResult) -> None:
    top = next((x for x in sal_res.items if x.is_top), None)
    if top:
        print(
            f"[perception_pipeline] top_salient={top.item_type}:{top.label} "
            f"score={top.score:.2f} combined={sal_res.combined_scalar:.2f}"
        )
    else:
        print(f"[perception_pipeline] top_salient=none combined={sal_res.combined_scalar:.2f}")


def _stage_acquisition(camera_manager: Any, image: Any) -> AcquisitionOutput:
    try:
        resolved = camera_manager.resolve_frame_detailed(image)
        print(
            f"[perception_pipeline] acquisition ok src={resolved.source} "
            f"has_frame={resolved.frame is not None} seq={resolved.frame_seq}"
        )
        return AcquisitionOutput(
            StageResult(ok=True, meta={"frame_seq": resolved.frame_seq, "source": resolved.source}),
            resolved=resolved,
        )
    except Exception as e:
        print(f"[perception_pipeline] acquisition failed: {e}\n{traceback.format_exc()}")
        return AcquisitionOutput(StageResult(ok=False, error=str(e)), resolved=None)


def _stage_quality(resolved: Any) -> QualityOutput:
    if resolved is None:
        print("[perception_pipeline] quality skipped (no resolved frame)")
        return QualityOutput(
            stage=StageResult(ok=False, skipped=True, error="no_resolution"),
            visual_truth_trusted=False,
            vision_status="stable",
            recognition_confidence_scale=1.0,
            expression_confidence_scale=1.0,
        )
    structured = getattr(resolved, "quality_detail", None)
    if structured is None and resolved.frame is not None:
        structured = compute_frame_quality(resolved.frame)
    label = structured.quality_label if structured is not None else "unreliable"
    q_rec, q_expr = confidence_scales_from_label(label)
    blur_val = 0.0
    blur_label = "sharp"
    br = be = bi = 1.0
    if structured is not None:
        blur_val = float(getattr(structured, "blur_value", 0.0))
        blur_label = getattr(structured, "blur_label", "sharp")
        br = float(getattr(structured, "blur_recognition_scale", 1.0))
        be = float(getattr(structured, "blur_expression_scale", 1.0))
        bi = float(getattr(structured, "blur_interpretation_scale", 1.0))
    rec_scale = q_rec * br
    expr_scale = q_expr * be
    conf = 1.0 if resolved.visual_truth_trusted else 0.0
    print(
        f"[perception_pipeline] quality vision={resolved.vision_status} "
        f"trusted={resolved.visual_truth_trusted} fq={resolved.frame_quality:.2f} "
        f"qlabel={label} recovery={resolved.recovery_state} "
        f"blur_value={blur_val:.1f} blur_label={blur_label} "
        f"blur_scale rec={br:.2f} expr={be:.2f} interp={bi:.2f} "
        f"combined_rec={rec_scale:.2f} combined_expr={expr_scale:.2f}"
    )
    return QualityOutput(
        stage=StageResult(
            ok=True,
            confidence=conf,
            meta={
                "vision_status": resolved.vision_status,
                "quality_label": label,
                "blur_label": blur_label,
                "blur_value": blur_val,
            },
        ),
        visual_truth_trusted=resolved.visual_truth_trusted,
        vision_status=resolved.vision_status,
        frame_quality=resolved.frame_quality,
        frame_quality_reasons=list(resolved.frame_quality_reasons or []),
        is_fresh=resolved.is_fresh,
        recovery_state=resolved.recovery_state,
        fresh_frame_streak=resolved.fresh_frame_streak,
        structured=structured,
        recognition_confidence_scale=rec_scale,
        expression_confidence_scale=expr_scale,
        blur_value=blur_val,
        blur_label=blur_label,
        blur_recognition_scale=br,
        blur_expression_scale=be,
        blur_interpretation_scale=bi,
        quality_only_recognition_scale=q_rec,
        quality_only_expression_scale=q_expr,
    )


def _stage_detection(camera_manager: Any, resolved: Any, g: dict) -> DetectionOutput:
    if resolved is None or resolved.frame is None:
        print("[perception_pipeline] detection skipped (no frame)")
        return DetectionOutput(
            stage=StageResult(ok=False, skipped=True, error="no_frame"),
            face_status="No camera image",
        )
    try:
        face_status = camera_manager.detect_face(resolved.frame, g)
    except Exception as e:
        print(f"[perception_pipeline] detection face_status failed: {e}")
        face_status = "No camera image"
    person_count = 0
    face_detected = False
    gaze_present = False
    face_rects: list[tuple[int, int, int, int]] = []
    try:
        cascade = g.get("face_cascade")
        if cascade is not None:
            import cv2

            gray = cv2.cvtColor(resolved.frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.3, 5)
            person_count = len(faces)
            face_detected = person_count > 0
            gaze_present = face_detected
            face_rects = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in faces]
    except Exception as e:
        print(f"[perception_pipeline] detection cascade failed: {e}")
    print(
        f"[perception_pipeline] detection ok={face_status!r} count={person_count} "
        f"face_detected={face_detected} rects={len(face_rects)}"
    )
    return DetectionOutput(
        stage=StageResult(ok=True, meta={"cascade": person_count >= 0}),
        face_detected=face_detected,
        person_count=person_count,
        face_status=face_status,
        gaze_present=gaze_present,
        face_rects=face_rects,
    )


def _stage_recognition(
    camera_manager: Any, resolved: Any, g: dict, trusted: bool
) -> RecognitionOutput:
    if not trusted or resolved is None or resolved.frame is None:
        print("[perception_pipeline] recognition skipped (untrusted or no frame)")
        return RecognitionOutput(
            stage=StageResult(ok=False, skipped=True, error="skipped_untrusted_or_no_frame"),
            recognized_text="",
            face_identity=None,
            identity_confidence=0.0,
        )
    try:
        recognized_text, face_identity = camera_manager.recognize_face(resolved.frame, g)
    except Exception as e:
        print(f"[perception_pipeline] recognition failed: {e}")
        return RecognitionOutput(
            stage=StageResult(ok=False, error=str(e)),
            recognized_text="",
            face_identity=None,
            identity_confidence=0.0,
        )
    print(
        f"[perception_pipeline] recognition ok identity={face_identity!r} "
        f"preview={(recognized_text or '')[:100]!r}"
    )
    return RecognitionOutput(
        stage=StageResult(ok=True),
        recognized_text=recognized_text or "",
        face_identity=face_identity,
        identity_confidence=0.0,
    )


def _stage_continuity(
    camera_manager: Any,
    resolved: Any,
    det: DetectionOutput,
    recog: RecognitionOutput,
    interp: InterpretationOutput,
    trusted: bool,
) -> ContinuityOutput:
    """Phase 7 temporal continuity after salience; ``note_trusted_identity`` on confirmed recognition."""
    last_stable = getattr(resolved, "last_stable_identity", None) if resolved else None
    sal_top_label = ""
    sal_top_type = ""
    sr = interp.salience_structured
    if sr is not None and sr.items:
        top = next((x for x in sr.items if x.is_top), None)
        if top is None:
            top = sr.items[0]
        sal_top_label = top.label
        sal_top_type = top.item_type

    frame_seq = int(getattr(resolved, "frame_seq", 0) or 0) if resolved else 0
    frame_ts = float(getattr(resolved, "frame_ts", 0.0) or 0.0) if resolved else 0.0
    shape = resolved.frame.shape if resolved is not None and resolved.frame is not None else None

    cr = update_continuity(
        trusted=trusted,
        frame_seq=frame_seq,
        frame_ts=frame_ts,
        frame_shape=shape,
        face_detected=bool(det.face_detected),
        face_rects=list(det.face_rects or []),
        recognized_text=recog.recognized_text or "",
        face_identity=recog.face_identity if trusted else None,
        salience_top_label=sal_top_label,
        salience_top_type=sal_top_type,
    )

    if not trusted or resolved is None:
        print(
            f"[perception_pipeline] continuity state={cr.identity_state} conf={cr.continuity_confidence:.2f} "
            f"(deferred_untrusted)"
        )
        return ContinuityOutput(
            stage=StageResult(ok=False, skipped=True, error="untrusted"),
            last_stable_identity=last_stable,
            continuity_confidence=cr.continuity_confidence,
            tracking_note="continuity_deferred_until_trusted_frame",
            structured=cr,
        )

    if cr.identity_state == "confirmed_recognition" and recog.face_identity:
        last_stable = recog.face_identity
    elif cr.last_stable_identity:
        last_stable = cr.last_stable_identity or last_stable

    tracking_note = ",".join(cr.matched_notes[:5]) if cr.matched_notes else "continuity_v7"
    flip = " suppress_flip" if cr.suppress_flip else ""
    print(
        f"[perception_pipeline] continuity state={cr.identity_state} conf={cr.continuity_confidence:.2f}"
        f"{flip} last_stable={last_stable!r} note={tracking_note!r}"
    )
    return ContinuityOutput(
        stage=StageResult(ok=True, confidence=cr.continuity_confidence),
        last_stable_identity=last_stable,
        continuity_confidence=cr.continuity_confidence,
        tracking_note=tracking_note,
        structured=cr,
    )


def _stage_interpretation(
    resolved: Any,
    det: DetectionOutput,
    recog: RecognitionOutput,
    trusted: bool,
    user_text: str,
    qual: QualityOutput,
) -> InterpretationOutput:
    emotion: str | None = None
    motion = _motion_smear_from_quality(qual)
    shape = resolved.frame.shape if resolved is not None and resolved.frame is not None else None

    if not trusted or resolved is None or resolved.frame is None or not det.face_detected:
        print("[perception_pipeline] interpretation emotion skipped")
        sal_res = build_salience_result(
            frame_shape=shape,
            face_rects=list(det.face_rects or []),
            face_detected=bool(det.face_detected),
            person_count=int(det.person_count or 0),
            face_identity=recog.face_identity if trusted else None,
            face_emotion=None,
            user_text=user_text,
            motion_smear_score=motion,
        )
        _log_top_salient(sal_res)
        return InterpretationOutput(
            stage=StageResult(ok=False, skipped=True, error="no_emotion_path"),
            face_emotion=None,
            salience=sal_res.combined_scalar,
            scene_summary_hint="",
            salience_structured=sal_res,
        )
    try:
        from .vision import analyze_face_emotion

        emotion = analyze_face_emotion(resolved.frame)
    except Exception as e:
        print(f"[perception_pipeline] interpretation emotion failed: {e}")
        emotion = "neutral"
    sal_res = build_salience_result(
        frame_shape=shape,
        face_rects=list(det.face_rects or []),
        face_detected=det.face_detected,
        person_count=det.person_count,
        face_identity=recog.face_identity,
        face_emotion=emotion,
        user_text=user_text,
        motion_smear_score=motion,
    )
    _log_top_salient(sal_res)
    print(
        f"[perception_pipeline] interpretation emotion={emotion!r} salience={sal_res.combined_scalar:.2f} "
        f"(scene_summary_hook=unused)"
    )
    return InterpretationOutput(
        stage=StageResult(ok=True, meta={"emotion": emotion}),
        face_emotion=emotion,
        salience=sal_res.combined_scalar,
        scene_summary_hint="",
        salience_structured=sal_res,
    )


def run_perception_pipeline(
    camera_manager: Any,
    image: Any,
    g: dict,
    user_text: str = "",
) -> PerceptionPipelineBundle:
    """Run all stages in order; each stage tolerates upstream failure."""
    ut = user_text or ""
    acq = _stage_acquisition(camera_manager, image)
    resolved = acq.resolved
    qual = _stage_quality(resolved)

    trusted = bool(
        resolved is not None
        and resolved.visual_truth_trusted
        and resolved.frame is not None
    )
    if trusted:
        det = _stage_detection(camera_manager, resolved, g)
        recog = _stage_recognition(camera_manager, resolved, g, True)
        interp = _stage_interpretation(resolved, det, recog, True, ut, qual)
        cont = _stage_continuity(camera_manager, resolved, det, recog, interp, True)
    else:
        print("[perception_pipeline] detection/recognition short-circuit (untrusted or no frame)")
        det = DetectionOutput(
            stage=StageResult(ok=False, skipped=True, error="untrusted_or_no_frame"),
            face_status="No camera image",
        )
        recog = RecognitionOutput(
            stage=StageResult(ok=False, skipped=True, error="skipped_untrusted"),
            recognized_text="",
            face_identity=None,
            identity_confidence=0.0,
        )
        interp = _stage_interpretation(resolved, det, recog, False, ut, qual)
        cont = _stage_continuity(camera_manager, resolved, det, recog, interp, False)

    cr_struct: ContinuityResult | None = cont.structured
    id_res = resolve_identity_fallback(
        trusted=trusted,
        face_detected=bool(det.face_detected),
        raw_identity=recog.face_identity,
        recognized_text=recog.recognized_text or "",
        recognition_confidence_scale=float(qual.recognition_confidence_scale),
        continuity=cr_struct,
    )
    if (
        trusted
        and id_res.identity_state == "confirmed_recognition"
        and id_res.raw_identity
    ):
        try:
            camera_manager.note_trusted_identity(id_res.raw_identity)
        except Exception as e:
            print(f"[perception_pipeline] identity note_trusted_identity failed: {e}")
    print(
        f"[perception_pipeline] identity resolved state={id_res.identity_state} "
        f"source={id_res.fallback_source} conf={id_res.identity_confidence:.2f} "
        f"resolved={id_res.resolved_identity!r}"
    )

    vs = getattr(resolved, "vision_status", "no_frame") if resolved is not None else "no_frame"
    af = getattr(resolved, "acquisition_freshness", "unavailable") if resolved is not None else "unavailable"
    fseq = int(getattr(resolved, "frame_seq", 0) or 0) if resolved is not None else 0
    ss = build_scene_summary(
        trusted=trusted,
        vision_status=str(vs),
        face_detected=bool(det.face_detected),
        person_count=int(det.person_count or 0),
        id_res=id_res,
        qual=qual,
        interp=interp,
        cont=cont,
        acquisition_freshness=str(af),
        frame_seq=fseq,
    )
    print(
        f"[perception_pipeline] summary state={ss.overall_scene_state} "
        f"identity={ss.primary_identity_summary!r} change={ss.scene_change_summary!r}"
    )

    il = build_interpretation_layer(
        trusted=trusted,
        vision_status=str(vs),
        user_text=ut,
        id_res=id_res,
        scene=ss,
        interp=interp,
        qual=qual,
        cont=cont,
    )
    print(
        f"[perception_pipeline] interpretation primary={il.primary_event!r} "
        f"priority={il.event_priority:.2f} conf={il.event_confidence:.2f}"
    )

    pm = build_perception_memory_output(
        wall_time=now_ts(),
        frame_seq=fseq,
        trusted=trusted,
        acquisition_freshness=str(af),
        id_res=id_res,
        scene=ss,
        il=il,
        qual=qual,
        interp=interp,
        cont=cont,
    )
    if pm.skipped or pm.event is None:
        print(
            f"[perception_pipeline] memory event=— candidate=False "
            f"skipped={pm.skipped} reason={pm.skip_reason!r}"
        )
    else:
        print(
            f"[perception_pipeline] memory event={pm.event.event_type!r} "
            f"candidate={pm.event.memory_worthy_candidate}"
        )

    mi = score_memory_importance(
        perception_memory=pm,
        id_res=id_res,
        scene=ss,
        il=il,
        qual=qual,
        cont=cont,
        acquisition_freshness=str(af),
    )
    print(
        f"[perception_pipeline] memory score={mi.decision.importance_score:.2f} "
        f"class={mi.decision.memory_class} worthy={mi.decision.memory_worthy}"
    )
    pl = learn_pattern_signals(
        perception_memory=pm,
        memory_importance=mi,
        id_res=id_res,
        scene=ss,
        il=il,
        cont=cont,
        acquisition_freshness=str(af),
    )
    print(
        f"[perception_pipeline] pattern={pl.primary_signal.pattern_type} "
        f"strength={pl.primary_signal.pattern_strength:.2f}"
    )
    pt = evaluate_proactive_triggers(
        perception_memory=pm,
        memory_importance=mi,
        pattern_learning=pl,
        id_res=id_res,
        scene=ss,
        il=il,
        qual=qual,
        cont=cont,
        acquisition_freshness=str(af),
        visual_truth_trusted=trusted,
        voice_user_turn_priority=bool(g.get("_voice_user_turn_priority")),
        runtime_silence_bias=float(g.get("_runtime_proactive_silence_bias", 0.0) or 0.0),
    )
    print(
        f"[perception_pipeline] proactive={pt.trigger_type} "
        f"score={pt.trigger_score:.2f}"
    )
    st = maybe_run_selftests(
        camera_manager=camera_manager,
        g=g,
        acquisition_freshness=str(af),
    )
    print(
        f"[perception_pipeline] selftest={st.summary.overall_status} "
        f"run={st.run_type}"
    )
    wb = build_workbench_proposals(
        selftests=st,
        acquisition_freshness=str(af),
        proactive_trigger=pt,
    )
    print(
        f"[perception_pipeline] workbench={wb.top_proposal.proposal_type} "
        f"priority={wb.top_proposal.priority}"
    )
    rf = build_reflection_result(
        memory_importance=mi,
        pattern_learning=pl,
        proactive_trigger=pt,
        selftests=st,
        workbench=wb,
        workbench_execution_result=g.get("_last_workbench_execution_result"),
        workbench_command_result=g.get("_last_workbench_command_result"),
        visual_truth_trusted=trusted,
        acquisition_freshness=str(af),
    )
    print(
        f"[perception_pipeline] reflection={rf.reflection_category} "
        f"state={rf.self_model.current_operational_state}"
    )
    ct = build_contemplation_result(
        reflection=rf,
        memory_importance=mi,
        pattern_learning=pl,
        perception_memory=pm,
        proactive_trigger=pt,
        selftests=st,
        workbench=wb,
        visual_truth_trusted=trusted,
        acquisition_freshness=str(af),
    )
    print(
        f"[perception_pipeline] contemplation={ct.contemplation_theme} "
        f"theme={ct.contemplation_theme}"
    )

    soc = build_social_continuity_result(
        user_text=ut,
        g=g,
        perception_memory=pm,
        memory_importance=mi,
        pattern_learning=pl,
        proactive_trigger=pt,
        reflection=rf,
        contemplation=ct,
        interpretation_layer=il,
        scene_summary=ss,
        identity_resolution=id_res,
    )
    print(
        f"[perception_pipeline] relationship hint={soc.interaction_style_hint} "
        f"familiarity={soc.familiarity_score:.2f} unfinished_thread={soc.unfinished_thread_present}"
    )

    mr = build_memory_refinement_result_safe(
        user_text=ut,
        g=g,
        perception_memory=pm,
        memory_importance=mi,
        pattern_learning=pl,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        interpretation_layer=il,
        identity_resolution=id_res,
    )
    print(
        f"[perception_pipeline] memory_refined class={mr.decision.refined_memory_class} "
        f"worthy={mr.decision.refined_memory_worthy}"
    )

    route_res = build_model_routing_result(
        user_text=ut,
        g=g,
        quality=qual,
        workbench=wb,
        memory_refinement=mr,
        social_continuity=soc,
        reflection=rf,
        contemplation=ct,
        interpretation_layer=il,
    )

    cq = build_curiosity_result_safe(
        user_text=ut,
        g=g,
        pattern_learning=pl,
        memory_refinement=mr,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        proactive_trigger=pt,
        selftests=st,
        workbench=wb,
        model_routing=route_res,
        identity_resolution=id_res,
        interpretation_layer=il,
        scene_summary=ss,
    )
    print(
        f"[perception_pipeline] curiosity={cq.curiosity_theme} triggered={cq.curiosity_triggered} "
        f"mode={cq.exploration_mode}"
    )

    ol = build_outcome_learning_result_safe(
        g=g,
        quality=qual,
        perception_memory=pm,
        memory_importance=mi,
        pattern_learning=pl,
        proactive_trigger=pt,
        selftests=st,
        workbench=wb,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        memory_refinement=mr,
        model_routing=route_res,
        curiosity=cq,
        identity_resolution=id_res,
        interpretation_layer=il,
        scene_summary=ss,
    )
    print(
        f"[perception_pipeline] outcome_learning={ol.outcome_category} conf={ol.adjustment_confidence:.2f} "
        f"target={ol.adjustment_target!r}"
    )

    cn = build_conversational_nuance_safe(
        g=g,
        quality=qual,
        interpretation_layer=il,
        scene_summary=ss,
        pattern_learning=pl,
        proactive_trigger=pt,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        memory_refinement=mr,
        model_routing=route_res,
        curiosity=cq,
        outcome_learning=ol,
    )
    print(
        f"[perception_pipeline] nuance={cn.confidence:.2f} tone={cn.nuance_tone} "
        f"pacing_hint={cn.emotional_pacing_hint!r}"
    )

    sc = build_strategic_continuity_safe(
        g=g,
        user_text=ut,
        identity_resolution=id_res,
        social_continuity=soc,
        memory_refinement=mr,
        workbench=wb,
        reflection=rf,
        contemplation=ct,
        curiosity=cq,
        outcome_learning=ol,
        conversational_nuance=cn,
        selftests=st,
    )
    print(
        f"[perception_pipeline] continuity_session={sc.continuity_scope} "
        f"conf={sc.continuity_confidence:.2f}"
    )

    ilp = build_supervised_self_improvement_loop_safe(
        g=g,
        selftests=st,
        workbench=wb,
        reflection=rf,
        contemplation=ct,
        outcome_learning=ol,
        strategic_continuity=sc,
    )
    print(
        f"[perception_pipeline] improvement_loop={ilp.loop_stage} "
        f"active={ilp.loop_active}"
    )

    hb = run_heartbeat_tick_safe(
        g=g,
        user_text=ut,
        selftests=st,
        workbench=wb,
        strategic_continuity=sc,
        curiosity=cq,
        outcome_learning=ol,
        improvement_loop=ilp,
        social_continuity=soc,
        model_routing=route_res,
        memory_refinement=mr,
    )
    al = run_adaptive_learning_safe(
        g=g,
        heartbeat=hb,
        outcome_learning=ol,
        curiosity=cq,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        conversational_nuance=cn,
        model_routing=route_res,
        memory_refinement=mr,
    )
    rp = build_runtime_presence_safe(
        g=g,
        heartbeat=hb,
        adaptive_learning=al,
        strategic_continuity=sc,
        improvement_loop=ilp,
        workbench=wb,
        selftests=st,
        social_continuity=soc,
    )
    g["_runtime_proactive_silence_bias"] = float((rp.meta or {}).get("proactive_silence_bias", 0.2) or 0.2)
    print(
        f"[perception_pipeline] heartbeat={hb.heartbeat_mode} "
        f"tick={hb.heartbeat_tick_id} learn_update={bool(al.learning_update_applied)} "
        f"presence={rp.presence_mode}"
    )

    concern_reconciliation = run_runtime_concern_reconciliation_safe(
        g=g,
        trusted=trusted,
        acquisition_freshness=str(af),
        identity_resolution=id_res,
        quality=qual,
        selftests=st,
        workbench=wb,
        improvement_loop=ilp,
        heartbeat=hb,
        runtime_presence=rp,
    )

    print(
        f"[perception_pipeline] package trusted={trusted} vision="
        f"{getattr(resolved, 'vision_status', 'n/a') if resolved else 'n/a'}"
    )
    pkg = PackageOutput(stage=StageResult(ok=True))

    bundle = PerceptionPipelineBundle(
        acquisition=acq,
        quality=qual,
        detection=det,
        recognition=recog,
        continuity=cont,
        interpretation=interp,
        package=pkg,
        resolved=resolved,
        user_text=ut,
        identity_resolution=id_res,
        scene_summary=ss,
        interpretation_layer=il,
        perception_memory=pm,
        memory_importance=mi,
        pattern_learning=pl,
        proactive_trigger=pt,
        selftests=st,
        workbench=wb,
        reflection=rf,
        contemplation=ct,
        social_continuity=soc,
        memory_refinement=mr,
        model_routing=route_res,
        curiosity=cq,
        outcome_learning=ol,
        conversational_nuance=cn,
        strategic_continuity=sc,
        improvement_loop=ilp,
        heartbeat=hb,
        adaptive_learning=al,
        runtime_presence=rp,
        concern_reconciliation=concern_reconciliation,
    )
    record_calibration_tick(bundle)
    return bundle
