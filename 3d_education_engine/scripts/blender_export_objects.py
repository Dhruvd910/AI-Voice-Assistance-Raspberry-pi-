"""Runs INSIDE Blender: export named objects of the open .blend as OBJ files.

    blender --background atlas.blend --python scripts/blender_export_objects.py -- OUT_DIR NAME [NAME ...]

Called by models/converter.py:blender_export_objects. Uses the OBJ exporter
that ships with Blender 3.2 and later (bpy.ops.wm.obj_export).
"""

import sys

import bpy  # noqa: provided by Blender

args = sys.argv[sys.argv.index("--") + 1:]
out_dir, names = args[0], args[1:]
missing = []
for name in names:
    obj = bpy.data.objects.get(name)
    if obj is None:
        missing.append(name)
        continue
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    bpy.ops.wm.obj_export(filepath=f"{out_dir}/{safe}.obj", export_selected_objects=True,
                          export_materials=False, apply_modifiers=True)
if missing:
    print("MISSING OBJECTS:", ", ".join(missing))
    sys.exit(2)
