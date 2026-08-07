"""Render a fixed Nyan Cat loop and compile it to a bicycle0 .anim.

Renderer geometry adapted from the community reference app
(maxswinkels/busybar-apps, apps/nyan-cat). Deterministic (seeded RNG) so the
committed asset is reproducible. Build-time only.

    uv run python tools/build_nyan_anim.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image

from tools.anim_encoder import encode_anim

W, H = 72, 16
FRAMES = 24  # ~2s loop at 12 fps
FPS = 12

CRUST=(0xFF,0xCC,0x99); FROSTING=(0xFF,0x99,0xFF); SPRINKLE=(0xDD,0x33,0x88)
GRAY=(0x99,0x99,0x99); BLACK=(0,0,0); CHEEK=(0xFF,0x99,0x99); STAR=(0xFF,0xFF,0xFF)
RAINBOW=[(0xFF,0,0),(0xFF,0x99,0),(0xFF,0xFF,0),(0x33,0xFF,0),(0,0x99,0xFF),(0x66,0x33,0xFF)]
CX,BY=44,3; HX,HY=CX+9,5; TRAIL_END=CX-5

def _blank(): return [(0,0,0)]*(W*H)
def _rect(buf,x,y,w,h,rgb):
    x2,y2=min(W,x+w),min(H,y+h); x,y=max(0,x),max(0,y)
    for yy in range(y,y2):
        base=yy*W
        for xx in range(x,x2): buf[base+xx]=rgb

def _stars_state(): return [{"x":8,"y":3,"p":0},{"x":26,"y":13,"p":2},{"x":46,"y":1,"p":1},{"x":66,"y":11,"p":3}]
def _tick_stars(buf,stars,rng):
    for s in stars:
        s["x"]-=3; s["p"]=(s["p"]+1)%4
        if s["x"]<-2: s["x"]=W+rng.randint(0,10); s["y"]=rng.randint(1,H-2)
        x,y,p=s["x"],s["y"],s["p"]
        if p==0: _rect(buf,x,y,1,1,STAR)
        elif p==1: _rect(buf,x-1,y,3,1,STAR); _rect(buf,x,y-1,1,3,STAR)
        elif p==2: _rect(buf,x-2,y,5,1,STAR); _rect(buf,x,y-2,1,5,STAR)
        else:
            for dx,dy in ((-2,0),(2,0),(0,-2),(0,2)): _rect(buf,x+dx,y+dy,1,1,STAR)

def _rainbow(buf,phase):
    for band,color in enumerate(RAINBOW):
        y=2+band*2; x=0
        while x<TRAIL_END:
            w=min(8,TRAIL_END-x); off=(x//8+phase)%2
            _rect(buf,x,y+off,w,2,color); x+=w

def _cat(buf,phase):
    bob=phase; by,hy=BY+bob,HY+bob
    _rect(buf,CX-2,by+5,2,2,GRAY)
    _rect(buf,CX-4,by+3,2,2,GRAY) if phase==0 else _rect(buf,CX-4,by+7,2,2,GRAY)
    for lx in (CX+1,CX+5,CX+10,CX+14): _rect(buf,lx+bob,13,2,2,GRAY)
    _rect(buf,CX+1,by,12,1,CRUST); _rect(buf,CX,by+1,14,8,CRUST); _rect(buf,CX+1,by+9,12,1,CRUST)
    _rect(buf,CX+1,by+1,12,8,FROSTING)
    for sx,sy in ((2,2),(6,3),(3,5),(7,6),(5,7)): _rect(buf,CX+sx,by+sy,1,1,SPRINKLE)
    _rect(buf,HX+1,hy,8,1,GRAY); _rect(buf,HX,hy+1,10,6,GRAY); _rect(buf,HX+1,hy+7,8,1,GRAY)
    _rect(buf,HX+1,hy-2,1,1,GRAY); _rect(buf,HX+1,hy-1,2,1,GRAY)
    _rect(buf,HX+8,hy-2,1,1,GRAY); _rect(buf,HX+7,hy-1,2,1,GRAY)
    _rect(buf,HX+2,hy+2,1,1,BLACK); _rect(buf,HX+7,hy+2,1,1,BLACK)
    _rect(buf,HX+1,hy+4,1,1,CHEEK); _rect(buf,HX+8,hy+4,1,1,CHEEK)
    _rect(buf,HX+2,hy+4,1,1,BLACK); _rect(buf,HX+6,hy+4,1,1,BLACK); _rect(buf,HX+3,hy+5,3,1,BLACK)

def render_frames():
    rng=random.Random(1)  # deterministic
    stars=_stars_state(); frames=[]
    for t in range(FRAMES):
        phase=(t//3)%2; buf=_blank()
        _tick_stars(buf,stars,rng); _rainbow(buf,phase); _cat(buf,phase)
        frames.append(buf)
    return frames

def main():
    out_dir=Path(__file__).resolve().parents[1]/"assets"/"nyan"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames=render_frames()
    # write PNGs for inspection/reproducibility
    frames_dir=out_dir/"frames"; frames_dir.mkdir(exist_ok=True)
    bgr_frames=[]
    for i,buf in enumerate(frames):
        img=Image.new("RGB",(W,H)); img.putdata(buf); img.save(frames_dir/f"frame_{i}.png")
        bgr_frames.append(bytes(c for (r,g,b) in buf for c in (b,g,r)))
    meta={"fps":FPS,"color_mode":"rgb888","sections":[]}
    (out_dir/"meta.json").write_text(json.dumps(meta, indent=2)+"\n")
    data=encode_anim(bgr_frames, W, H, fps=FPS, color_mode="rgb888")
    (out_dir/"nyan_72x16.anim").write_bytes(data)
    print(f"wrote {out_dir/'nyan_72x16.anim'} ({len(data)} bytes, {len(frames)} frames)")

if __name__=="__main__":
    main()
