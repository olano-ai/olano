# Third-party notices

Olano's own code is released under the MIT License (see [LICENSE](LICENSE)). The
components below are included in this repository under their own licences,
which continue to apply to them. Dependencies installed from PyPI or npm are
not listed here; their licences ship with the packages.

## Code

### hermes-agent (MIT)

The messaging adapter package `olano/platforms/` was originally ported from the
gateway of [hermes-agent](https://github.com/NousResearch/hermes-agent) (the
adapter base, platform config, session and status modules) and has been
extended substantially since.

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### OpenClaw (MIT)

The fake-secret guard in `olano/platforms/api_server.py` is ported from an
[OpenClaw](https://github.com/openclaw/openclaw) change, and the Feishu and QQ
bot adapters follow the structure of OpenClaw's adapters for those platforms.

```
MIT License

Copyright (c) 2026 OpenClaw Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Caveman (Apache License 2.0)

The content-aware preview for offloaded tool results,
`olano/core/tool_result_preview.py`, is a modified Python port of compressors
from the engine of [Caveman](https://github.com/JuliusBrussee/caveman)
(`engine/compressors/json.go`, `tabular.go`, `log.go`, `invariants.go` and
`redundancy.go`). Changes in this repository: rewritten in Python; no query
relevance, change-point or element-length heuristics; every preview is fitted
to a fixed character budget; markers point at the offloaded file instead of a
recovery tool. The full licence text is in `olano/core/CAVEMAN_LICENSE.txt`.

```
Caveman
Copyright 2026 Julius Brussee
```

## Skills

### Anthropic skills (Apache License 2.0)

These folders under `olano/skills/built_in/` come from
[anthropics/skills](https://github.com/anthropics/skills) and are licensed
under the Apache License, Version 2.0. Each folder carries the full licence
text in `LICENSE.txt`.

| Skill | Changes in this repository |
|---|---|
| `canvas-design` | `SKILL.md` edited (wording, an Olano note on charts); marked as modified in its frontmatter |
| `frontend-design` | `SKILL.md` edited (wording); marked as modified in its frontmatter |
| `internal-comms` | none |
| `slack-gif-creator` | none |
| `doc-coauthoring` | none |

### Anthropic document skills (source-available)

`docx`, `pdf`, `pptx` and `xlsx` under `olano/skills/built_in/` are Anthropic's
document skills. They are source-available, not open source; the terms are in
each folder's `LICENSE.txt`. Copyright (c) 2025 Anthropic, PBC.

## Libraries bundled inside skills

### Vega, Vega-Lite and Vega-Embed (BSD-3-Clause)

`olano/skills/built_in/charts/assets/vendor/` holds unmodified builds of
vega 6.4.0, vega-lite 6.4.3 and vega-embed 7.2.0. Copyright (c) University of
Washington Interactive Data Lab. The licence texts sit beside the files
(`LICENSE-vega`, `LICENSE-vega-lite`, `LICENSE-vega-embed`) and `VERSIONS.txt`
records the upstream builds and checksums.

## Fonts

### Fonts bundled with the canvas-design skill (SIL Open Font License 1.1)

`olano/skills/built_in/canvas-design/canvas-fonts/` bundles these families
under the SIL Open Font License, Version 1.1. Each family's licence text sits
beside its font files as `<Family>-OFL.txt`: Arsenal SC, Big Shoulders,
Boldonse, Bricolage Grotesque, Crimson Pro, DM Mono, Erica One, Geist Mono,
Gloock, IBM Plex Mono, IBM Plex Serif, Instrument Sans, Instrument Serif,
Italiana, JetBrains Mono, Jura, Libre Baskerville, Lora, National Park, Nothing
You Could Do, Outfit, Pixelify Sans, Poiret One, Red Hat Mono, Silkscreen, Smooch
Sans, Tektur, Work Sans and Young Serif ([IBM Plex](https://github.com/IBM/plex),
[Instrument Serif](https://github.com/Instrument/instrument-serif)).
