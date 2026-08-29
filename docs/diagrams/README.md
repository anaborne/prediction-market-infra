These diagrams are generated. Source of truth is the layout function in `hot_path_diagram.py`
(kept outside this repository), which emits the two SVGs here and the inline copy on
anaborne.github.io from one set of coordinates.

The README embeds the PNGs. GitHub serves README images through its own proxy, and Chrome reports
`naturalWidth: 0` for these SVGs loaded that way, so the picture renders as a broken image. The
SVGs are kept as the vector source and for anything that embeds them directly.

    hot-path-{light,dark}.svg   vector source, 980x252
    hot-path-{light,dark}.png   2x raster, 1960x504, transparent background
