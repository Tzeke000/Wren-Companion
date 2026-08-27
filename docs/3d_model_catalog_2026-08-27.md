# 3D model catalog for lyric_viz centerpieces (hunted 2026-08-27)

Zeke's ask: *"find like 20 to 30 different 3-D models... free audio-visualization-reactive
models."* All from **Poly Pizza** (poly.pizza — the Google Poly archive + living creators),
all FREE, low-poly (perfect for the wireframe/mesh look), downloadable as **glTF/GLB**.
URL pattern: `https://poly.pizza/m/<ID>`.

## License rules (matters for published videos)
- **Public Domain (CC0)** — use freely, no credit needed. Prefer these for TikTok.
- **CC-BY 3.0** — free but MUST credit the author in the video description/caption.

## The catalog (34 curated of 43 found)

### Skulls & bones (Burn It Down / dark dubstep energy)
| Model | Author | License | ID |
|---|---|---|---|
| Skull | Kay Lousberg | **CC0** | 7clFQGz5jH |
| Skull (variant 1) | Quaternius | **CC0** | YgmRBtFlcF |
| Skull (variant 2) | Quaternius | **CC0** | ExZmhOIjka |
| Skull (variant 3) | Quaternius | **CC0** | EAsVEJwsv7 |
| Skull (variant 4) | Quaternius | **CC0** | DJT7N0HuVB |
| Ghost Skull | Quaternius | **CC0** | TX8r9WBXpe |
| Skeleton | Quaternius | **CC0** | yq5ATpujSt |
| skull003 (realistic) | Jake K-H | CC-BY 3.0 | bjf0z6Qb9Tv |
| Skull | CreativeTechLab | CC-BY 3.0 | oxtpAT0TaB |
| Skull | Alex Safayan | CC-BY 3.0 | 52a7EEFzFWi |
| Skull | Ian Wall | CC-BY 3.0 | 738EKrsYz96 |
| Brain | Poly by Google | CC-BY 3.0 | 5mPRPZkI3qt |

### Hearts (Plastic Heart / Be My One / Love the Mirror)
| Heart | Poly by Google | CC-BY 3.0 | 8RA5hHU5gHK |
| Heart | Quaternius | **CC0** | 1yCRUwFnwX |
| Heart (v2) | Poly by Google | CC-BY 3.0 | 5POtMKIT_Ze |

### Fire (Burn It Down)
| Fire | Jakob Hippe | CC-BY 3.0 | 1QpMTUO7P-G |
| Campfire | Poly by Google | CC-BY 3.0 | 0vzzmM-t8CP |

### Cars & motorcycles (Chipped White Car / the Zaro moto angle)
| CAR Model | Ignition Labs | CC-BY 3.0 | 5zUWP5UsLg- |
| Car | Poly by Google | CC-BY 3.0 | 75h3mi6uHuC |
| Red Car | J-Toastie | CC-BY 3.0 | dVLJ5CjB0h |
| Mazda RX-7 | IvOfficial | CC-BY 3.0 | SnIoWlh7S2 |
| Motorcycle | Poly by Google | CC-BY 3.0 | dse64pqMKAR |
| Motorcycle (v2) | Poly by Google | CC-BY 3.0 | 5_MTCnqfUTr |

### Cities & skylines (Crimson Skyline / Neon Horizon)
| SF Street | Alan Zimmerman | CC-BY 3.0 | cnTMgkFoTS0 |
| Rio de Janeiro | Alan Zimmerman | CC-BY 3.0 | 2binsxeOBve |
| Vancouver, but small | Michal Minecki | CC-BY 3.0 | 5deJc9xvuzn |

### Gems, bolts, cosmic (drops / EDM generic)
| diamond | smoj | CC-BY 3.0 | 5SvQ6iU_CHg |
| Gem Green | Quaternius | **CC0** | kbgiCMzdxg |
| Jewel | Zsky | CC-BY 3.0 | velVo80s1D |
| Lightning Bolt | Jarlan Perez | CC-BY 3.0 | 7I1IhiE7O8s |
| Lightning bolt | Poly by Google | CC-BY 3.0 | 8rX_fFhz6XH |
| Planets (set) | Poly by Google | CC-BY 3.0 | 3_tN7i962hZ |
| Planet | Quaternius | **CC0** | 18Uxrb2dIc |
| Sun | Poly by Google | CC-BY 3.0 | 77wHkzwlpOq |
| Rose | Erbay ÇELIK | CC-BY 3.0 | 4UQ29NSK0ir |

## Implementation note (the actual work, next session)
lyric_viz's `--shape` renders procedural wireframes from vertex/edge arrays
(`_shape_mesh`). To use these models: add a `--shape model:<file.glb>` path — load GLB
via `trimesh` (pip, MIT) or a minimal GLB parser, decimate to ~1-3k edges, feed the same
`_project` pipeline. Bass-reactive scale/rotation comes free from `_viz_shape`. CC0
skull (Quaternius) is the obvious first test on Burn It Down.

## Sources sweep (for future hunts)
- poly.pizza — this catalog; search + `/m/<id>` pages, license per model. ✅ works headless
- Also known-good free sources not yet needed: Quaternius.com (CC0 packs),
  Kenney.nl (CC0), polyhaven.com/models (CC0), OpenGameArt, Sketchfab (filter
  downloadable+CC — needs login for some).
