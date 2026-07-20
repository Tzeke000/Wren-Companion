"""Iris's room — v3 'everything I know' render. Top-down, charger=origin.
Integrates: trilaterated markers (wall pairs + floor twins), full furniture from
the facts file, the cone runway (evenly spaced), the overhead webcam AND its
field-of-view cone (its viewpoint over the floor), and the measured distances.
Still an anchor model, not SLAM — camera + runway length are estimates."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, Circle, Wedge
import math

fig, ax = plt.subplots(figsize=(11, 12))
ax.set_facecolor("#0b0f1a"); fig.patch.set_facecolor("#0b0f1a")
FG="#dbe6f5"; DIM="#8fa8cc"; ACC="#5aa0ff"
TRI="#ffb454"; CIR="#7ee081"; DIA="#c792ea"; CHG="#ff6b6b"
FURN="#22314f"; FURN_E="#3a4d73"

# ---- SOLVED anchors (m). East wall on RIGHT (x=0); room extends WEST (-x).
charger=(0.0,0.0)
tri=(0.0,0.699)             # Triangles wall pair — EAST wall, 0.699 N of dock
dia=(-2.665,-0.366)         # Diamonds bed post — WEST wall
circ=(-2.018,1.32)          # Circles books-dresser — NORTH/across
cube=(-1.70,0.0)            # end of runway (length estimated)
cones=[(-0.34,0),(-0.68,0),(-1.02,0),(-1.36,0)]

def _tri2(p,rp,q,rq,south=True):
    d=math.dist(p,q); a=(d*d+rp*rp-rq*rq)/(2*d); h=math.sqrt(max(0,rp*rp-a*a))
    ux,uy=((q[0]-p[0])/d,(q[1]-p[1])/d); bx,by=(p[0]+a*ux,p[1]+a*uy)
    s1=(bx-h*uy,by+h*ux); s2=(bx+h*uy,by-h*ux)
    return s1 if (s1[1]<s2[1])==south else s2
cam=_tri2(cube,2.457,dia,2.163,south=True)   # webcam estimate (SW desk)

# floor twins (face-up, ~at the wall marker, slightly into the room)
tri_f=(tri[0]-0.14,tri[1]); cir_f=(circ[0],circ[1]-0.16); dia_f=(dia[0]+0.14,dia[1])

x0,x1=-3.0,0.2
y0,y1=cam[1]-0.4, circ[1]+0.4
ax.add_patch(Rectangle((x0,y0),x1-x0,y1-y0,fill=False,edgecolor="#29395c",lw=2.5))

# ---- WEBCAM FIELD OF VIEW (its viewpoint over the floor) — drawn FIRST (under)
# FOV MEASURED v9 (Zeke: "find the camera degrees — it's connected to you").
# Cam = unbranded Sonix module (VID 0C45 PID 6536), no published spec — so it
# was solved from the frame itself: two TAPED anchors visible at known
# bearings (Triangles4 @ px562, Diamonds floor twin @ px~52) fit the
# rectilinear model px = cx + f*tan(axis-theta) -> f≈594px,
# HFOV = 2*atan(320/f) ≈ 56.6 deg, optical axis ≈ 79 deg.
fov=Wedge(cam,3.7,51,107,facecolor=ACC,alpha=0.10,edgecolor=ACC,lw=1.0,
          linestyle=(0,(4,3)),zorder=0)
ax.add_patch(fov)
ax.text(-2.4,-1.05,"webcam FOV\n56.6° (measured)",color=ACC,fontsize=8.5,
        ha="center",va="center",style="italic",alpha=0.9,zorder=1)

# ---- FURNITURE (v5 2026-07-20: OBSERVED through the overhead webcam) -------
# Zeke: "you can actually look through your camera to see what you need to."
# Frame: state/vector/overhead_probe.jpg (camera looks N-NE over the floor).
# OBSERVED (solid): bed runs NORTH-SOUTH on the west side, from the south
# area up to the Diamonds post (post = bed's NORTH end), a hand off the west
# wall; couch back near the NORTH wall just EAST of the books dresser, FACING
# SOUTH toward the desk; lit floor lamp between dresser and bed; charger +
# Vector at the east baseboard under the window. GUESS (dashed) = inference.
def furn(x,y,w,h,label,fs=8,rot=0,fc=FURN,tc="#b9c6dd",guess=False):
    ax.add_patch(Rectangle((x,y),w,h,facecolor=fc,edgecolor=("#c98a4a" if guess
                 else FURN_E),lw=1.4,linestyle=((0,(3,2)) if guess else "-"),
                 zorder=1))
    ax.text(x+w/2,y+h/2,label,color=tc,fontsize=fs,ha="center",va="center",
            rotation=rot,zorder=2)
# v6 (Zeke, Discord, post-freeze): base = v2's bones + tape + HIS corrections.
# "Ignore what opus made": furniture goes ON the walls (desk still floats).
# BED all the way into the WEST/NORTH-WEST corner, AGAINST the west wall,
# running south to the Diamonds post (post = its FOOT). Lamp BESIDE the bed
# (cam confirms: mattress ends, lamp, then dresser). Dresser-2 (speaker) sits
# IN FRONT of the bed near its foot (his past-pictures correction).
# BED: hard against the west wall, head in the NW corner, foot = Diamonds post
furn(x0,dia[1],0.52,y1-dia[1]-0.02,"lofted bed",fs=7.5,rot=90)
# FLOOR LAMP: beside the bed's head, before the dresser (cam-confirmed)
ax.add_patch(Circle((x0+0.62,circ[1]+0.12),0.07,facecolor="#ffe9a8",
             edgecolor="#c9b06a",lw=1.2,zorder=2))
ax.text(x0+0.62,circ[1]-0.12,"lamp",color="#c9b06a",fontsize=6.5,
        ha="center",zorder=2)
# NORTH wall: books dresser + bookshelf on top (Circles station), on the wall
furn(circ[0]-0.28,circ[1]+0.02,1.0,0.34,"books dresser + shelf",fs=7.5)
# COUCH: against the north wall, EAST of the dresser, facing SOUTH (cam)
furn(-1.25,y1-0.52,1.12,0.5,"couch",fs=7.5)
ax.annotate("",xy=(-0.69,y1-0.9),xytext=(-0.69,y1-0.54),
            arrowprops=dict(arrowstyle="->",color="#b9c6dd",lw=1.1))
ax.text(-0.6,y1-0.78,"faces desk",color="#8fa8cc",fontsize=6.5,ha="left")
# SPEAKER DRESSER: in FRONT of the bed = off its FOOT, south of the Diamonds
# post (Zeke). NOT in camera view — it sits behind the webcam's left shoulder.
# Sightline proof he taught: the v7 spot (east of the bed) would have blocked
# the cam's ray to the lamp + Circles, both of which the frame clearly shows.
furn(x0+0.02,dia[1]-0.65,0.52,0.55,"speaker\ndresser",fs=7)
# DESK+PC+webcam: against the SOUTH wall, piano beside it (Zeke corrected —
# back to the earlier layout; the webcam sits on this desk)
furn(cam[0]-0.6,y0+0.02,1.2,0.34,"desk + PC",fs=8)
# PIANO (Wren's machine): on the south wall next to the desk
furn(cam[0]+0.65,y0+0.02,0.85,0.3,"piano (Wren's PC)",fs=7)
# (NO corner dressers by the piano — removed per Zeke)
# east wall features: brick + door south of dock
ax.add_patch(Rectangle((x1-0.16,-0.72),0.16,0.34,facecolor="#5a3a2a",
             edgecolor="#7a5a44",lw=1.2,zorder=1))
ax.text(x1-0.22,-0.55,"brick",color="#c8a488",fontsize=7,ha="right",va="center")
ax.add_patch(Rectangle((x1-0.05,-1.7),0.05,0.62,facecolor="#333f5c",
             edgecolor="#55688f",lw=1.2,zorder=1))
ax.text(x1-0.09,-1.4,"door",color=DIM,fontsize=7,ha="right",va="center")

# ---- helpers
def dot(p,c,label,dy=0.12,fs=10.5):
    ax.add_patch(Circle(p,0.068,color=c,zorder=5))
    ax.text(p[0],p[1]+dy,label,color=c,fontsize=fs,ha="center",va="bottom",
            weight="bold",zorder=6)
def fdot(p,c):  # floor twin — hollow
    ax.add_patch(Circle(p,0.05,facecolor="none",edgecolor=c,lw=1.6,zorder=5))
def dim(p,q,label,lpos,col=DIM):
    ax.add_patch(FancyArrowPatch(p,q,arrowstyle="<->",color=col,lw=1.3,
                 mutation_scale=10,zorder=3,linestyle=(0,(5,3))))
    ax.text(lpos[0],lpos[1],label,color=col,fontsize=9,ha="center",va="center",
            weight="bold",zorder=7,bbox=dict(boxstyle="round,pad=0.2",
            fc="#0b0f1a",ec=col,lw=0.8))

# runway
for cx,cy in cones: ax.plot(cx,cy,marker="^",color="#ff8c42",ms=10,zorder=4)
ax.add_patch(Circle(cube,0.06,color="#ff8c42",zorder=5))
ax.text(cube[0],cube[1]-0.15,"cube",color="#ff8c42",fontsize=9,ha="center",
        va="top",weight="bold")
ax.text(-0.85,0.11,"cone runway (4 cones + cube, evenly spaced)",color="#ff8c42",
        fontsize=8,ha="center",style="italic")

# markers (wall pairs) + floor twins
dot(charger,CHG,"CHARGER / dock",dy=-0.28)
dot(tri,TRI,"▲ Triangles"); fdot(tri_f,TRI)
dot(dia,DIA,"◆ Diamonds",dy=0.12); fdot(dia_f,DIA)
dot(circ,CIR,"● Circles",dy=0.12); fdot(cir_f,CIR)
ax.text(circ[0]+0.9,circ[1]-0.18,"(○ = floor twin)",color=DIM,fontsize=7.5,
        ha="left",va="center")
# webcam
ax.plot(cam[0],cam[1],marker="s",color=ACC,ms=13,zorder=6)
ax.text(cam[0]-0.1,cam[1]-0.12,"≈ webcam (est.)\nh 1.23 m",color=ACC,fontsize=8,
        ha="right",va="top",weight="bold",zorder=6)

# measured distances
dim(tri,charger,"0.699 m",lpos=(0.5,0.35),col=TRI)
dim(tri,dia,"2.870 m",lpos=(-1.0,-0.95),col="#c9a0ff")
dim(charger,circ,"~2.38 m",lpos=(-0.5,0.92),col=CIR)
dim(dia,circ,"1.765 m",lpos=(-2.72,0.55),col=DIM)
dim(cam,cube,"2.457 m",lpos=(-1.5,-1.35),col=ACC)
# cam -> Diamonds3 (v8, Zeke's tape 2026-07-20): 98in slant = 2.489 m, cam
# height 48.5in = 1.232 m -> floor run = sqrt(2.489^2 - 1.232^2) = 2.163 m.
_slant=math.sqrt(2.489**2 - 1.232**2)   # = 2.163 (the trig, done live)
dim(cam,dia_f,f"{_slant:.3f} m floor\n(98in slant − 1.23 m height)",
    lpos=(-2.95,-1.55),col=DIA)

# walls
ax.text(x1+0.02,0.4,"EAST wall — outlet/window, dock",color=DIM,fontsize=8.5,
        ha="left",va="center",rotation=90)
ax.text(x0-0.04,dia[1]+0.15,"WEST wall",color=DIM,fontsize=8.5,ha="right",
        va="center",rotation=90)
ax.text((x0+x1)/2,y0-0.05,"SOUTH wall",color=DIM,fontsize=9,ha="center",va="top")
ax.text((x0+x1)/2,y1+0.04,"NORTH / across the room",color=DIM,fontsize=9,
        ha="center",va="bottom")

ax.set_title("Iris's room — v8: speaker dresser off the bed's FOOT (out of "
             "cam view), FOV tightened to what the frame really shows\n"
             "SOLID = anchors + furniture per Zeke + webcam — "
             "correct me if off",
             color=FG,fontsize=11,weight="bold",pad=12)
ax.set_xlim(x0-0.8,x1+0.4); ax.set_ylim(y0-0.4,y1+0.45)
ax.set_aspect("equal"); ax.axis("off")
fig.text(0.5,0.015,"Charger=origin. No lidar → anchor model, not SLAM. Markers "
         "solved from Zeke's tape; webcam + runway length are estimates. The FOV "
         "cone is roughly where the webcam watches the floor.",color=DIM,
         fontsize=7.5,ha="center",va="bottom")

out=r"D:\Wren-Companion\state\scratch\room_map.png"
import os
os.makedirs(os.path.dirname(out),exist_ok=True)
fig.savefig(out,dpi=130,facecolor=fig.get_facecolor(),bbox_inches="tight")
print("WROTE",out,"| cam est=",tuple(round(v,2) for v in cam))
