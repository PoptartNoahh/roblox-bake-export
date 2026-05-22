# SPDX-License-Identifier: GPL-3.0-or-later
#
# Roblox Bake Export - bake Cycles lighting into per-object albedo
# textures and export a Roblox-ready FBX in one click.

bl_info = {
    "name": "Roblox Bake Export",
    "author": "PoptartNoahh",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Roblox Bake",
    "description": "Path-trace bake lighting into per-object albedo and export an FBX ready for Roblox Studio's 3D Importer",
    "warning": "",
    "doc_url": "",
    "tracker_url": "",
    "category": "Import-Export",
}

import os
import sys
import time
import subprocess
import traceback
from pathlib import Path

import bpy
from bpy.types import (
    AddonPreferences,
    Operator,
    Panel,
    PropertyGroup,
)
from bpy.props import (
    BoolProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)


# -----------------------------------------------------------------------------
# Constants

BAKE_NODE_NAME = "RBX_Bake_Target"
DEFAULT_OUTPUT = "//baked_export/"

RESOLUTION_ITEMS = (
    ('256', "256", "256 x 256 - tiny props"),
    ('512', "512", "512 x 512 - small props"),
    ('1024', "1024", "1024 x 1024 - default"),
    ('2048', "2048", "2048 x 2048 - hero props"),
    ('4096', "4096", "4096 x 4096 - very large or detailed surfaces"),
)


# -----------------------------------------------------------------------------
# Helpers

def addon_prefs(context):
    return context.preferences.addons[__name__].preferences


def gather_targets(context):
    """Return the meshes the user wants to bake.

    If anything is selected, only baked the selected meshes. Otherwise fall
    back to every visible mesh in the view layer so the operator can be used
    from a clean scene without fussing with selection.
    """
    selection = [obj for obj in context.selected_objects if obj.type == 'MESH']
    if selection:
        return selection
    return [
        obj for obj in context.view_layer.objects
        if obj.type == 'MESH' and obj.visible_get()
    ]


def resolve_output_dir(raw_path):
    if raw_path.startswith("//") and not bpy.data.filepath:
        return None, "Save the .blend first, or set an absolute output folder"
    try:
        out = Path(bpy.path.abspath(raw_path)).resolve()
        out.mkdir(parents=True, exist_ok=True)
        (out / "textures").mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        return None, "Cannot create output folder: %s" % ex
    return out, None


def has_degenerate_uvs(mesh):
    """Active UV layer has no usable area (e.g. all loops at the origin)."""
    layer = mesh.uv_layers.active
    if layer is None or len(layer.data) == 0:
        return True
    umin = vmin = float("inf")
    umax = vmax = float("-inf")
    for loop in layer.data:
        u, v = loop.uv
        if u < umin: umin = u
        if u > umax: umax = u
        if v < vmin: vmin = v
        if v > vmax: vmax = v
    return (umax - umin) < 1e-5 or (vmax - vmin) < 1e-5


def open_in_file_browser(path):
    """Reveal *path* in the host OS file browser."""
    path = str(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        return False


# -----------------------------------------------------------------------------
# Preferences

class RBX_AP_preferences(AddonPreferences):
    bl_idname = __name__

    default_output: StringProperty(
        name="Default Output Folder",
        description="Where new scenes start writing their bakes",
        default=DEFAULT_OUTPUT,
        subtype='DIR_PATH',
    )
    default_resolution: EnumProperty(
        name="Default Resolution",
        items=RESOLUTION_ITEMS,
        default='1024',
    )
    default_samples: IntProperty(
        name="Default Samples",
        default=256,
        min=1,
        soft_max=4096,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Defaults applied to fresh scenes")
        col = layout.column(align=True)
        col.prop(self, "default_output")
        col.prop(self, "default_resolution")
        col.prop(self, "default_samples")


# -----------------------------------------------------------------------------
# Settings (scene-scoped)

class RBX_PG_settings(PropertyGroup):
    output_folder: StringProperty(
        name="Output Folder",
        description="Where the FBX and baked PNGs are written. Use // for a path relative to the .blend file",
        default=DEFAULT_OUTPUT,
        subtype='DIR_PATH',
    )
    resolution: EnumProperty(
        name="Resolution",
        description="Pixel size of each per-object albedo texture",
        items=RESOLUTION_ITEMS,
        default='1024',
    )
    samples: IntProperty(
        name="Cycles Samples",
        description="More samples gives a cleaner bake but takes longer",
        default=256,
        min=1,
        soft_max=4096,
    )
    denoise: BoolProperty(
        name="Denoise",
        description="Run the Cycles denoiser on the baked image",
        default=True,
    )
    auto_unwrap: BoolProperty(
        name="Auto-unwrap Missing UVs",
        description="If a mesh has no UV map (or its UVs have zero area), run Smart UV Project before baking",
        default=True,
    )
    margin: IntProperty(
        name="Bake Margin",
        description="Pixels of padding around each UV island to prevent seams when mip-mapped",
        default=4,
        min=0,
        max=64,
        subtype='PIXEL',
    )
    open_when_done: BoolProperty(
        name="Reveal in File Browser",
        description="Open the output folder in the OS file browser after the FBX is written",
        default=True,
    )


# -----------------------------------------------------------------------------
# Operator: Bake & Export

class RBX_OT_bake_and_export(Operator):
    """Bake lighting of selected meshes into per-object albedo textures and export a Roblox-ready FBX"""
    bl_idname = 'rbx.bake_and_export'
    bl_label = "Bake & Export FBX"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode == 'OBJECT'

    # -- node / image plumbing ------------------------------------------------

    def create_bake_image(self, obj, resolution):
        name = "%s_Albedo" % obj.name
        existing = bpy.data.images.get(name)
        if existing is not None:
            bpy.data.images.remove(existing)
        image = bpy.data.images.new(
            name=name,
            width=resolution,
            height=resolution,
            alpha=False,
            float_buffer=False,
        )
        image.colorspace_settings.name = 'sRGB'
        return image

    def ensure_material(self, obj):
        """Guarantee the object has at least one node-based material."""
        if not obj.material_slots:
            mat = bpy.data.materials.new(name="%s_AutoMat" % obj.name)
            mat.use_nodes = True
            obj.data.materials.append(mat)
            return [mat], True
        appended = False
        mats = []
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                mat = bpy.data.materials.new(name="%s_AutoMat" % obj.name)
                mat.use_nodes = True
                slot.material = mat
                appended = True
            elif not mat.use_nodes:
                mat.use_nodes = True
            mats.append(mat)
        return mats, appended

    def add_bake_target_node(self, mat, image):
        nodes = mat.node_tree.nodes
        for stale in [n for n in nodes if n.name == BAKE_NODE_NAME]:
            nodes.remove(stale)
        for n in nodes:
            n.select = False
        node = nodes.new(type='ShaderNodeTexImage')
        node.name = BAKE_NODE_NAME
        node.label = "RBX Bake Target"
        node.image = image
        node.location = (-800, -400)
        node.select = True
        nodes.active = node
        return node

    def strip_bake_target_nodes(self, mat):
        if not mat or not mat.use_nodes:
            return
        nodes = mat.node_tree.nodes
        for stale in [n for n in nodes if n.name == BAKE_NODE_NAME]:
            nodes.remove(stale)

    def build_baked_material(self, obj, image):
        name = "%s_Baked" % obj.name
        existing = bpy.data.materials.get(name)
        if existing is not None:
            bpy.data.materials.remove(existing)
        mat = bpy.data.materials.new(name=name)
        mat.use_nodes = True

        tree = mat.node_tree
        for n in list(tree.nodes):
            tree.nodes.remove(n)

        bsdf = tree.nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        # Lighting is already baked into the colour. Flatten the PBR response
        # so Roblox doesn't double-light the surface on import.
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 1.0
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.0
        if "Specular" in bsdf.inputs:               # Blender 3.x
            bsdf.inputs["Specular"].default_value = 0.0
        elif "Specular IOR Level" in bsdf.inputs:   # Blender 4.x
            bsdf.inputs["Specular IOR Level"].default_value = 0.0

        tex = tree.nodes.new(type='ShaderNodeTexImage')
        tex.image = image
        tex.location = (-400, 0)

        out = tree.nodes.new(type='ShaderNodeOutputMaterial')
        out.location = (300, 0)

        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        tree.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        return mat

    # -- bake op call (version-tolerant) --------------------------------------

    def run_bake(self, margin):
        """Run a Cycles COMBINED bake, tolerating signature changes across Blender versions.

        Blender 4.x removed ``'AO'`` from the COMBINED ``pass_filter`` enum
        (it is its own bake type now). Older builds accept different sets.
        Try progressively simpler kwargs until one is accepted.
        """
        attempts = (
            {
                'type': 'COMBINED',
                'pass_filter': {'DIRECT', 'INDIRECT', 'DIFFUSE', 'GLOSSY', 'EMIT'},
                'margin': margin,
                'use_clear': True,
                'save_mode': 'INTERNAL',
            },
            {
                'type': 'COMBINED',
                'margin': margin,
                'use_clear': True,
                'save_mode': 'INTERNAL',
            },
            {
                'type': 'COMBINED',
                'margin': margin,
                'use_clear': True,
            },
            {'type': 'COMBINED'},
        )
        last = None
        for kwargs in attempts:
            try:
                bpy.ops.object.bake(**kwargs)
                return True, None
            except (RuntimeError, TypeError) as ex:
                last = ex
        return False, last

    # -- UV ensuring ----------------------------------------------------------

    def ensure_uvs(self, context, obj, auto_unwrap):
        mesh = obj.data
        has_layer = len(mesh.uv_layers) > 0
        needs_unwrap = (not has_layer) or has_degenerate_uvs(mesh)
        if not needs_unwrap:
            return True, None
        if not auto_unwrap:
            reason = "no UV map" if not has_layer else "UVs have zero area"
            return False, "%s: %s and auto-unwrap is disabled" % (obj.name, reason)

        prev_active = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)

        try:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj

            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.02)
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError as ex:
            if obj.mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except RuntimeError:
                    pass
            return False, "%s: auto-unwrap failed (%s)" % (obj.name, ex)
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for o in prev_selected:
                if o.name in context.view_layer.objects:
                    o.select_set(True)
            if prev_active is not None and prev_active.name in context.view_layer.objects:
                context.view_layer.objects.active = prev_active

        return True, None

    # -- main -----------------------------------------------------------------

    def execute(self, context):
        scene = context.scene
        settings = scene.rbx_bake
        t_start = time.time()

        targets = gather_targets(context)
        if not targets:
            self.report({'ERROR'}, "No mesh objects to bake. Select some, or have visible meshes")
            return {'CANCELLED'}

        output_dir, err = resolve_output_dir(settings.output_folder)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        textures_dir = output_dir / "textures"

        resolution = int(settings.resolution)
        margin = settings.margin

        # Snapshot what we are about to mutate.
        prev_engine = scene.render.engine
        prev_samples = None
        prev_denoise = None
        if hasattr(scene, "cycles"):
            prev_samples = scene.cycles.samples
            prev_denoise = scene.cycles.use_denoising

        prev_active = context.view_layer.objects.active
        prev_selection = list(context.selected_objects)

        material_backup = {}
        appended_slot = {}
        for obj in targets:
            material_backup[obj.name] = [slot.material for slot in obj.material_slots]
            appended_slot[obj.name] = False

        baked_images = {}
        materials_touched = set()
        new_baked_mats = []
        warnings_log = []
        skipped = []

        try:
            scene.render.engine = 'CYCLES'
            scene.render.bake.use_clear = True
            scene.render.bake.margin = margin
            if hasattr(scene, "cycles"):
                scene.cycles.samples = settings.samples
                scene.cycles.use_denoising = bool(settings.denoise)
                if hasattr(scene.cycles, "bake_type"):
                    scene.cycles.bake_type = 'COMBINED'

            # Per-object: ensure UVs, create image, bake, save.
            for index, obj in enumerate(targets, start=1):
                print("[RBX Bake] (%d/%d) %s" % (index, len(targets), obj.name))

                ok, msg = self.ensure_uvs(context, obj, settings.auto_unwrap)
                if not ok:
                    warnings_log.append(msg)
                    skipped.append(obj.name)
                    continue

                mats, appended = self.ensure_material(obj)
                appended_slot[obj.name] = appended
                if appended and len(material_backup[obj.name]) < len(obj.material_slots):
                    material_backup[obj.name] = [None] * len(obj.material_slots)

                image = self.create_bake_image(obj, resolution)
                baked_images[obj.name] = image

                for mat in mats:
                    self.add_bake_target_node(mat, image)
                    materials_touched.add(mat.name)

                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj

                ok, bake_err = self.run_bake(margin)
                if not ok:
                    warnings_log.append("%s: bake failed (%s)" % (obj.name, bake_err))
                    skipped.append(obj.name)
                    continue

                png_path = textures_dir / ("%s_Albedo.png" % obj.name)
                try:
                    image.filepath_raw = str(png_path)
                    image.file_format = 'PNG'
                    image.save()
                except RuntimeError as ex:
                    warnings_log.append("%s: failed to save PNG (%s)" % (obj.name, ex))
                    skipped.append(obj.name)
                    continue

            # Swap to baked materials on the survivors.
            survivors = [
                obj for obj in targets
                if obj.name in baked_images and obj.name not in skipped
            ]
            for obj in survivors:
                image = baked_images[obj.name]
                baked_mat = self.build_baked_material(obj, image)
                new_baked_mats.append(baked_mat.name)
                if not obj.material_slots:
                    obj.data.materials.append(baked_mat)
                else:
                    for slot in obj.material_slots:
                        slot.material = baked_mat

            if not survivors:
                self.report({'ERROR'}, "Nothing was baked successfully (see console)")
                return {'CANCELLED'}

            # FBX export.
            stem = Path(bpy.data.filepath).stem if bpy.data.filepath else "BakedScene"
            fbx_path = output_dir / ("%s.fbx" % stem)

            bpy.ops.object.select_all(action='DESELECT')
            for obj in survivors:
                obj.select_set(True)
            context.view_layer.objects.active = survivors[0]

            bpy.ops.export_scene.fbx(
                filepath=str(fbx_path),
                check_existing=False,
                use_selection=True,
                use_visible=False,
                use_active_collection=False,
                object_types={'MESH'},
                use_mesh_modifiers=True,
                mesh_smooth_type='FACE',
                use_tspace=False,
                path_mode='COPY',
                embed_textures=True,
                axis_forward='-Z',
                axis_up='Y',
                bake_space_transform=True,
                apply_unit_scale=True,
                bake_anim=False,
            )

            elapsed = time.time() - t_start
            summary = "Baked %d mesh(es) -> %s (%.1fs)" % (
                len(survivors), fbx_path.name, elapsed,
            )
            if skipped:
                summary += " | skipped: %s" % ", ".join(skipped)
            self.report({'INFO'}, summary)
            for w in warnings_log:
                self.report({'WARNING'}, w)
            print("[RBX Bake] %s" % summary)
            print("[RBX Bake] Output dir: %s" % output_dir)

            scene.rbx_bake_last_output = str(output_dir)

            if settings.open_when_done:
                open_in_file_browser(output_dir)

            return {'FINISHED'}

        except Exception as ex:
            traceback.print_exc()
            self.report({'ERROR'}, "Bake & Export crashed: %s" % ex)
            return {'CANCELLED'}

        finally:
            # Restore: materials, bake target nodes, engine, selection.
            for obj in targets:
                originals = material_backup.get(obj.name, [])
                if appended_slot.get(obj.name) and not originals:
                    obj.data.materials.clear()
                else:
                    for i, orig in enumerate(originals):
                        if i < len(obj.material_slots):
                            obj.material_slots[i].material = orig

            for name in materials_touched:
                mat = bpy.data.materials.get(name)
                if mat is not None:
                    self.strip_bake_target_nodes(mat)

            for name in new_baked_mats:
                mat = bpy.data.materials.get(name)
                if mat is not None and mat.users == 0:
                    bpy.data.materials.remove(mat)

            scene.render.engine = prev_engine
            if hasattr(scene, "cycles"):
                if prev_samples is not None:
                    scene.cycles.samples = prev_samples
                if prev_denoise is not None:
                    scene.cycles.use_denoising = prev_denoise

            try:
                bpy.ops.object.select_all(action='DESELECT')
                for o in prev_selection:
                    if o.name in context.view_layer.objects:
                        o.select_set(True)
                if prev_active is not None and prev_active.name in context.view_layer.objects:
                    context.view_layer.objects.active = prev_active
            except RuntimeError:
                pass


# -----------------------------------------------------------------------------
# Operator: Open Output Folder

class RBX_OT_open_output_folder(Operator):
    """Reveal the most recent output folder in the OS file browser"""
    bl_idname = 'rbx.open_output_folder'
    bl_label = "Open Output Folder"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        path = getattr(context.scene, "rbx_bake_last_output", "")
        return bool(path) and Path(path).exists()

    def execute(self, context):
        path = context.scene.rbx_bake_last_output
        if not open_in_file_browser(path):
            self.report({'WARNING'}, "Could not open: %s" % path)
            return {'CANCELLED'}
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Panel

class RBX_PT_main(Panel):
    bl_label = "Roblox Lighting Bake"
    bl_idname = 'RBX_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Roblox Bake"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.rbx_bake

        col = layout.column(align=True)
        col.prop(settings, "output_folder", text="Output")

        row = layout.row(align=True)
        row.prop(settings, "resolution", text="Res")

        col = layout.column(align=True)
        col.prop(settings, "samples")
        col.prop(settings, "denoise")
        col.prop(settings, "margin")
        col.prop(settings, "auto_unwrap")
        col.prop(settings, "open_when_done")

        layout.separator()

        count = self.target_count(context)
        info = layout.row()
        if count == 0:
            info.alert = True
        info.label(text="Targets: %d mesh(es)" % count, icon='OUTLINER_OB_MESH')

        run = layout.row()
        run.scale_y = 1.6
        run.enabled = count > 0 and context.mode == 'OBJECT'
        run.operator(RBX_OT_bake_and_export.bl_idname, icon='RENDER_RESULT')

        if context.mode != 'OBJECT':
            sub = layout.row()
            sub.alert = True
            sub.label(text="Switch to Object Mode to bake", icon='ERROR')

        if not bpy.data.filepath and settings.output_folder.startswith("//"):
            sub = layout.row()
            sub.alert = True
            sub.label(text="Save the .blend or pick an absolute path", icon='ERROR')

        last = getattr(context.scene, "rbx_bake_last_output", "")
        if last:
            sub = layout.row()
            sub.operator(RBX_OT_open_output_folder.bl_idname, icon='FILE_FOLDER')

    @staticmethod
    def target_count(context):
        sel = sum(1 for o in context.selected_objects if o.type == 'MESH')
        if sel:
            return sel
        return sum(
            1 for o in context.view_layer.objects
            if o.type == 'MESH' and o.visible_get()
        )


# -----------------------------------------------------------------------------
# Registration

classes = (
    RBX_AP_preferences,
    RBX_PG_settings,
    RBX_OT_bake_and_export,
    RBX_OT_open_output_folder,
    RBX_PT_main,
)

addon_keymaps = []


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)

    bpy.types.Scene.rbx_bake = PointerProperty(type=RBX_PG_settings)
    bpy.types.Scene.rbx_bake_last_output = StringProperty(
        name="Last Output",
        default="",
    )

    # Optional keymap stub - assign a shortcut via Preferences > Keymap if wanted.
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is not None:
        km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
        kmi = km.keymap_items.new(
            RBX_OT_bake_and_export.bl_idname,
            type='B',
            value='PRESS',
            ctrl=True,
            shift=True,
            alt=True,
        )
        addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    for attr in ("rbx_bake", "rbx_bake_last_output"):
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
