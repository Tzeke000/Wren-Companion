import { useEffect, useRef } from "react";
import * as THREE from "three";

interface OrbProps {
  emotion: string;
  emotionColor: string;
  state: "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "offline";
  size?: number;
}

const EMOTION_CONFIG: Record<string, {
  color: string; lightColor: string; darkColor: string;
  shape: string; coreScale: number; particleSpread: number;
  connectionDensity: number; gravityY: number; tiltX: number;
  pulseSpeed: number; pulseAmplitude: number;
}> = {
  calmness:     { color:"#1a6cf5",lightColor:"#6aa3ff",darkColor:"#0a3080",shape:"sphere",    coreScale:1.0, particleSpread:1.0, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.05 },
  joy:          { color:"#f5c518",lightColor:"#ffe680",darkColor:"#a07800",shape:"scattered",  coreScale:1.3, particleSpread:1.3, connectionDensity:0.8, gravityY:0.3,  tiltX:0,    pulseSpeed:3.0, pulseAmplitude:0.15 },
  happiness:    { color:"#f5c518",lightColor:"#ffe680",darkColor:"#a07800",shape:"scattered",  coreScale:1.2, particleSpread:1.2, connectionDensity:0.7, gravityY:0.2,  tiltX:0,    pulseSpeed:2.5, pulseAmplitude:0.12 },
  excitement:   { color:"#ff6b00",lightColor:"#ffa060",darkColor:"#8a3000",shape:"scattered",  coreScale:1.4, particleSpread:1.5, connectionDensity:0.9, gravityY:0,    tiltX:0,    pulseSpeed:6.0, pulseAmplitude:0.2  },
  curiosity:    { color:"#00d4d4",lightColor:"#80ffff",darkColor:"#007070",shape:"sphere",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0.3,  pulseSpeed:2.0, pulseAmplitude:0.08 },
  interest:     { color:"#00d4d4",lightColor:"#80ffff",darkColor:"#007070",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.4, gravityY:0,    tiltX:0.2,  pulseSpeed:2.0, pulseAmplitude:0.07 },
  boredom:      { color:"#4a5568",lightColor:"#8090a8",darkColor:"#202830",shape:"compressed", coreScale:0.7, particleSpread:0.8, connectionDensity:0.1, gravityY:-0.4, tiltX:0,    pulseSpeed:0.5, pulseAmplitude:0.03 },
  sadness:      { color:"#553c9a",lightColor:"#9070e0",darkColor:"#2a1a50",shape:"teardrop",   coreScale:0.75,particleSpread:0.85,connectionDensity:0.1, gravityY:-0.6, tiltX:0,    pulseSpeed:0.8, pulseAmplitude:0.04 },
  loneliness:   { color:"#2c5282",lightColor:"#6090c0",darkColor:"#102040",shape:"contracted", coreScale:0.6, particleSpread:0.7, connectionDensity:0.05,gravityY:-0.3, tiltX:0,    pulseSpeed:0.6, pulseAmplitude:0.03 },
  anger:        { color:"#c53030",lightColor:"#ff6060",darkColor:"#600000",shape:"compressed", coreScale:1.3, particleSpread:0.9, connectionDensity:0.2, gravityY:0,    tiltX:0,    pulseSpeed:8.0, pulseAmplitude:0.25 },
  frustration:  { color:"#e53e3e",lightColor:"#ff8080",darkColor:"#701010",shape:"compressed", coreScale:1.1, particleSpread:0.9, connectionDensity:0.15,gravityY:0,    tiltX:0,    pulseSpeed:5.0, pulseAmplitude:0.2  },
  fear:         { color:"#44337a",lightColor:"#8060c0",darkColor:"#201040",shape:"contracted", coreScale:0.5, particleSpread:0.6, connectionDensity:0.1, gravityY:0,    tiltX:0,    pulseSpeed:7.0, pulseAmplitude:0.08 },
  anxiety:      { color:"#44337a",lightColor:"#8060c0",darkColor:"#201040",shape:"contracted", coreScale:0.6, particleSpread:0.65,connectionDensity:0.1, gravityY:0,    tiltX:0,    pulseSpeed:6.0, pulseAmplitude:0.07 },
  surprise:     { color:"#d53f8c",lightColor:"#ff80cc",darkColor:"#700040",shape:"scattered",  coreScale:1.5, particleSpread:1.6, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:10.0,pulseAmplitude:0.3  },
  trust:        { color:"#38a169",lightColor:"#70e0a0",darkColor:"#185030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.05 },
  anticipation: { color:"#d69e2e",lightColor:"#ffd060",darkColor:"#705000",shape:"sphere",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.4, gravityY:0.1,  tiltX:0.25, pulseSpeed:2.5, pulseAmplitude:0.1  },
  love:         { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.2, particleSpread:1.1, connectionDensity:0.9, gravityY:0.1,  tiltX:0,    pulseSpeed:2.0, pulseAmplitude:0.1  },
  affection:    { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.7, gravityY:0.1,  tiltX:0,    pulseSpeed:1.8, pulseAmplitude:0.08 },
  adoration:    { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.3, particleSpread:1.2, connectionDensity:0.8, gravityY:0.15, tiltX:0,    pulseSpeed:2.2, pulseAmplitude:0.12 },
  pride:        { color:"#6b46c1",lightColor:"#b080ff",darkColor:"#301870",shape:"elongated",  coreScale:1.2, particleSpread:1.1, connectionDensity:0.5, gravityY:0.5,  tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.06 },
  confidence:   { color:"#ecc94b",lightColor:"#ffe880",darkColor:"#806800",shape:"sphere",     coreScale:1.3, particleSpread:1.15,connectionDensity:0.6, gravityY:0.3,  tiltX:0,    pulseSpeed:1.8, pulseAmplitude:0.07 },
  triumph:      { color:"#ecc94b",lightColor:"#ffe880",darkColor:"#806800",shape:"elongated",  coreScale:1.4, particleSpread:1.2, connectionDensity:0.7, gravityY:0.6,  tiltX:0,    pulseSpeed:2.5, pulseAmplitude:0.1  },
  contempt:     { color:"#4a5568",lightColor:"#8090a8",darkColor:"#202830",shape:"compressed", coreScale:0.8, particleSpread:0.85,connectionDensity:0.05,gravityY:-0.1, tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.04 },
  shame:        { color:"#b7791f",lightColor:"#e0a040",darkColor:"#5a3500",shape:"contracted", coreScale:0.65,particleSpread:0.75,connectionDensity:0.1, gravityY:-0.4, tiltX:-0.2, pulseSpeed:0.8, pulseAmplitude:0.03 },
  guilt:        { color:"#2d3748",lightColor:"#607090",darkColor:"#101820",shape:"teardrop",   coreScale:0.6, particleSpread:0.7, connectionDensity:0.05,gravityY:-0.5, tiltX:0,    pulseSpeed:0.6, pulseAmplitude:0.03 },
  envy:         { color:"#68d391",lightColor:"#a0ffc0",darkColor:"#206030",shape:"scattered",  coreScale:0.9, particleSpread:1.1, connectionDensity:0.2, gravityY:0,    tiltX:0.15, pulseSpeed:2.0, pulseAmplitude:0.1  },
  disgust:      { color:"#2f855a",lightColor:"#60c080",darkColor:"#103020",shape:"compressed", coreScale:0.8, particleSpread:0.85,connectionDensity:0.1, gravityY:-0.2, tiltX:-0.1, pulseSpeed:1.5, pulseAmplitude:0.06 },
  awe:          { color:"#4299e1",lightColor:"#90d0ff",darkColor:"#183870",shape:"scattered",  coreScale:1.5, particleSpread:1.5, connectionDensity:0.4, gravityY:0.2,  tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.2  },
  relief:       { color:"#81e6d9",lightColor:"#c0fff8",darkColor:"#306860",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:1.2, pulseAmplitude:0.06 },
  nostalgia:    { color:"#d4a574",lightColor:"#f0cc90",darkColor:"#705030",shape:"spiral",     coreScale:0.9, particleSpread:0.95,connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:0.8, pulseAmplitude:0.05 },
  hope:         { color:"#f6e05e",lightColor:"#fff080",darkColor:"#806800",shape:"elongated",  coreScale:1.1, particleSpread:1.05,connectionDensity:0.4, gravityY:0.4,  tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.08 },
  confusion:    { color:"#9f7aea",lightColor:"#d0a0ff",darkColor:"#402870",shape:"scattered",  coreScale:0.9, particleSpread:1.1, connectionDensity:0.2, gravityY:0,    tiltX:0,    pulseSpeed:4.0, pulseAmplitude:0.15 },
  contentment:  { color:"#68d391",lightColor:"#a0ffc0",darkColor:"#206030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.35,gravityY:0,    tiltX:0,    pulseSpeed:1.2, pulseAmplitude:0.05 },
  sympathy:     { color:"#38a169",lightColor:"#70e0a0",darkColor:"#185030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0.1,  pulseSpeed:1.5, pulseAmplitude:0.06 },
};

function getCfg(emotion: string) {
  const key = emotion.toLowerCase().replace(/[^a-z]/g,"");
  return EMOTION_CONFIG[key] || EMOTION_CONFIG["calmness"];
}

function createGlowTex(color: string): THREE.Texture {
  const c = document.createElement("canvas");
  c.width=128; c.height=128;
  const ctx = c.getContext("2d")!;
  const col = new THREE.Color(color);
  const g = ctx.createRadialGradient(64,64,0,64,64,64);
  g.addColorStop(0,`rgba(${Math.round(col.r*255)},${Math.round(col.g*255)},${Math.round(col.b*255)},1)`);
  g.addColorStop(0.4,`rgba(${Math.round(col.r*255)},${Math.round(col.g*255)},${Math.round(col.b*255)},0.4)`);
  g.addColorStop(1,"rgba(0,0,0,0)");
  ctx.fillStyle=g; ctx.fillRect(0,0,128,128);
  return new THREE.CanvasTexture(c);
}

export default function OrbCanvas({ emotion, state, size = 320 }: OrbProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const disposeRef = useRef<()=>void>(()=>{});

  useEffect(() => {
    if (!mountRef.current) return;
    disposeRef.current();
    const container = mountRef.current;
    const cfg = getCfg(emotion);

    const renderer = new THREE.WebGLRenderer({ antialias:true, alpha:true });
    renderer.setSize(size,size);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
    renderer.setClearColor(0x000000,0);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(70,1,0.01,100);
    camera.position.z = 2.8;

    const innerGroup = new THREE.Group();
    const outerGroup = new THREE.Group();
    const streamGroup = new THREE.Group();
    scene.add(innerGroup,outerGroup,streamGroup);

    // Core
    const coreGeo = new THREE.SphereGeometry(0.15,32,32);
    const coreMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfg.lightColor), transparent:true, opacity:0.95, blending:THREE.AdditiveBlending, depthWrite:false });
    const core = new THREE.Mesh(coreGeo,coreMat);
    innerGroup.add(core);

    const igGeo = new THREE.SphereGeometry(0.3,16,16);
    const igMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfg.color), transparent:true, opacity:0.3, blending:THREE.AdditiveBlending, depthWrite:false });
    const innerGlow = new THREE.Mesh(igGeo,igMat);
    innerGroup.add(innerGlow);

    // Streams
    const streamPhases: number[] = [];
    const streams: THREE.Line[] = [];
    for(let i=0;i<16;i++){
      const pts: THREE.Vector3[] = [];
      const cp = [
        new THREE.Vector3(0,0,0),
        new THREE.Vector3((Math.random()-.5)*1.2,(Math.random()-.5)*1.2,(Math.random()-.5)*1.2),
        new THREE.Vector3((Math.random()-.5)*1.4,(Math.random()-.5)*1.4,(Math.random()-.5)*1.4),
        new THREE.Vector3((Math.random()-.5)*1.0,(Math.random()-.5)*1.0,(Math.random()-.5)*1.0),
        new THREE.Vector3(0,0,0),
      ];
      const curve = new THREE.CatmullRomCurve3(cp);
      for(let j=0;j<=60;j++) pts.push(curve.getPoint(j/60));
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const mat = new THREE.LineBasicMaterial({ color:new THREE.Color(i<8?cfg.lightColor:cfg.color), transparent:true, opacity:0.4+Math.random()*0.4, blending:THREE.AdditiveBlending, depthWrite:false });
      const line = new THREE.Line(geo,mat);
      line.rotation.set(Math.random()*Math.PI*2,Math.random()*Math.PI*2,Math.random()*Math.PI*2);
      streamGroup.add(line); streams.push(line); streamPhases.push(Math.random()*Math.PI*2);
    }

    // Particles
    const N = 1500;
    const pos = new Float32Array(N*3);
    const orig = new Float32Array(N*3);
    const vel = new Float32Array(N*3);
    const pcol = new Float32Array(N*3);
    const baseC = new THREE.Color(cfg.color);
    const lightC = new THREE.Color(cfg.lightColor);
    const darkC = new THREE.Color(cfg.darkColor);

    for(let i=0;i<N;i++){
      const tier = Math.random();
      const r = tier<0.25 ? 0.05+Math.random()*0.35 : tier<0.65 ? 0.35+Math.random()*0.35 : 0.7+Math.random()*0.3;
      const theta = Math.random()*Math.PI*2;
      const phi = Math.acos(2*Math.random()-1);
      const x=r*Math.sin(phi)*Math.cos(theta), y=r*Math.sin(phi)*Math.sin(theta), z=r*Math.cos(phi);
      pos[i*3]=x; pos[i*3+1]=y; pos[i*3+2]=z;
      orig[i*3]=x; orig[i*3+1]=y; orig[i*3+2]=z;
      vel[i*3]=(Math.random()-.5)*0.001; vel[i*3+1]=(Math.random()-.5)*0.001; vel[i*3+2]=(Math.random()-.5)*0.001;
      const c = tier<0.25?lightC:tier<0.65?baseC:darkC;
      pcol[i*3]=c.r; pcol[i*3+1]=c.g; pcol[i*3+2]=c.b;
    }

    const pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute("position",new THREE.BufferAttribute(pos,3));
    pGeo.setAttribute("color",new THREE.BufferAttribute(pcol,3));
    const pMat = new THREE.PointsMaterial({ size:0.025, vertexColors:true, transparent:true, opacity:0.85, blending:THREE.AdditiveBlending, depthWrite:false, sizeAttenuation:true });
    innerGroup.add(new THREE.Points(pGeo,pMat));

    // Shell
    const shellGeo = new THREE.SphereGeometry(1.0,16,12);
    const shellMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfg.darkColor), wireframe:true, transparent:true, opacity:0.08, blending:THREE.AdditiveBlending, depthWrite:false });
    outerGroup.add(new THREE.Mesh(shellGeo,shellMat));

    // Halo
    const haloTex = createGlowTex(cfg.color);
    const haloMat = new THREE.SpriteMaterial({ map:haloTex, transparent:true, opacity:0.2, blending:THREE.AdditiveBlending, depthWrite:false });
    const halo = new THREE.Sprite(haloMat);
    halo.scale.set(3.5,3.5,1);
    scene.add(halo);

    const pLight = new THREE.PointLight(new THREE.Color(cfg.color),2.0,5);
    scene.add(pLight);

    const clock = new THREE.Clock();
    let fid = 0;

    function morph(t: number) {
      const c = getCfg(emotion);
      const sp = c.particleSpread, gy = c.gravityY, tx = c.tiltX;
      const pa = pGeo.attributes.position as THREE.BufferAttribute;
      for(let i=0;i<N;i++){
        const ox=orig[i*3], oy=orig[i*3+1], oz=orig[i*3+2];
        const dist=Math.sqrt(ox*ox+oy*oy+oz*oz);
        let mx=ox*sp, my=oy*sp, mz=oz*sp;
        if(c.shape==="teardrop"){ my-=Math.max(0,-oy)*0.4; my+=gy*dist*0.5; }
        else if(c.shape==="elongated"){ my*=1.4; my+=gy*0.3; }
        else if(c.shape==="compressed"){ my*=0.7; }
        else if(c.shape==="contracted"){ mx*=0.6; my*=0.6; mz*=0.6; }
        else if(c.shape==="scattered"){ const f=1+Math.sin(t*0.5+dist*3)*0.15; mx*=f; my*=f; mz*=f; }
        else if(c.shape==="double"){ if(oy>0){mx+=0.15;my+=0.1;}else{mx-=0.15;my-=0.05;} }
        else if(c.shape==="spiral"){ const a=t*0.3+dist*2; const nx=mx*Math.cos(a)-mz*Math.sin(a); mz=mx*Math.sin(a)+mz*Math.cos(a); mx=nx; }
        my+=gy*0.2;
        mz+=tx*oy*0.3;
        mx+=vel[i*3]*30; my+=vel[i*3+1]*30; mz+=vel[i*3+2]*30;
        pa.setXYZ(i,mx,my,mz);
      }
      pa.needsUpdate=true;
    }

    function spd() {
      return {thinking:2.5,deep:5.0,speaking:1.8,bored:0.3,excited:7.0,offline:0.1,idle:1.0}[state]||1.0;
    }

    function animate(){
      fid=requestAnimationFrame(animate);
      const t=clock.getElapsedTime();
      const s=spd();
      const c=getCfg(emotion);
      const r=s*0.001;
      innerGroup.rotation.y+=r; innerGroup.rotation.x+=r*0.4;
      outerGroup.rotation.y-=r*0.7; outerGroup.rotation.z+=r*0.3;
      streamGroup.rotation.y+=r*1.2; streamGroup.rotation.x-=r*0.5;
      const pulse=1+Math.sin(t*c.pulseSpeed)*c.pulseAmplitude;
      core.scale.setScalar(pulse*c.coreScale);
      innerGlow.scale.setScalar(pulse*c.coreScale*0.9);
      pLight.intensity=1.5+Math.sin(t*c.pulseSpeed)*0.5;
      morph(t);
      for(let i=0;i<N;i++){
        vel[i*3]+=(Math.random()-.5)*0.00002; vel[i*3+1]+=(Math.random()-.5)*0.00002; vel[i*3+2]+=(Math.random()-.5)*0.00002;
        vel[i*3]*=0.99; vel[i*3+1]*=0.99; vel[i*3+2]*=0.99;
      }
      streams.forEach((l,i)=>{
        const mat=l.material as THREE.LineBasicMaterial;
        mat.opacity=Math.sin(t*s*2+streamPhases[i])>0.7?0.9:0.2+Math.sin(t*s+streamPhases[i])*0.2;
        l.rotation.y+=r*0.5*Math.sin(streamPhases[i]);
      });
      halo.scale.set(3.5+Math.sin(t*0.7)*0.3,3.5+Math.sin(t*0.7)*0.3,1);
      haloMat.opacity=0.15+Math.sin(t*0.5)*0.05;
      if(state==="offline"){ coreMat.opacity=Math.max(0,coreMat.opacity-0.002); pMat.opacity=Math.max(0,pMat.opacity-0.001); }
      if(emotion==="confusion") innerGroup.rotation.z+=Math.sin(t*3)*0.005;
      renderer.render(scene,camera);
    }
    animate();

    disposeRef.current=()=>{
      cancelAnimationFrame(fid);
      renderer.dispose();
      [coreGeo,igGeo,shellGeo,pGeo].forEach(g=>g.dispose());
      [coreMat,igMat,shellMat,pMat,haloMat].forEach(m=>m.dispose());
      streams.forEach(l=>{ l.geometry.dispose(); (l.material as THREE.Material).dispose(); });
      haloTex.dispose();
      if(container.contains(renderer.domElement)) container.removeChild(renderer.domElement);
    };

    return () => disposeRef.current();
  }, [emotion, state, size]);

  return (
    <div ref={mountRef} style={{ width:size, height:size, display:"flex", alignItems:"center", justifyContent:"center", background:"transparent", overflow:"visible", flexShrink:0 }} />
  );
}
