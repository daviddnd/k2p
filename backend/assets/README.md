# Draft-filler assets

`NotoSansHK.ttf` — Noto Sans HK (variable TTF), the font the CR-draft filler embeds
as its Traditional-Chinese/overlay face (ADR-0004 as amended 2026-08-19: "fields
carry data, overlay carries rendering"). Fetched from the font's upstream
`google/fonts` path: <https://github.com/google/fonts/raw/main/ofl/notosanshk/NotoSansHK%5Bwght%5D.ttf>
(the same bytes the Phase 0 fill spike proved, SHA-256
`76098ee78ec234cd4f8c950742b3f766fea2f8b43d5180d901048f4fc86c6849`).

It is Traditional/HK-only: a Simplified character is absent from its cmap and the
filler refuses such a value by name (`font_glyph_unavailable`) rather than printing
tofu (see `research/counter-parties/cr/chinese-character-requirements.md`).

`OFL.txt` — the SIL Open Font License 1.1 the font ships under, verbatim from
<https://github.com/google/fonts/raw/main/ofl/notosanshk/OFL.txt>.

Both files ship in the execution-plane image (`deploy/backend.Dockerfile` copies
this directory to `/app/assets`). A deployment may point `AICS_DRAFT_FONT_PATH` at
another copy; the packaged path is the default.
