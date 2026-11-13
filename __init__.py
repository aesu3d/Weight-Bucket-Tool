# Weight Bucket ver 1.0, made with love by Aesu <3

# dictionary
bl_info = {
    "name": "Weight Bucket",
    "author": "Aesu3D",
    "version": (1, 0, 1),
    "blender": (4, 2, 0),
    "location": "Weight Paint mode > Alt+F, or Sidebar (N) > AESU > Weight Bucket",
    "description": "Fill a whole mesh island (like L-select) with weight, like a paint bucket",
    "category": "Paint",
}

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from bpy_extras import view3d_utils
from mathutils import Vector, kdtree
from mathutils.bvhtree import BVHTree

twin_reach = 0.0001
bucket_is_on = False  # turns True while Bucket Mode is running ^-^


# settings
class WBUCKET_Settings(bpy.types.PropertyGroup):
    mode: EnumProperty(
        name="Mode",
        items=[
            ("REPLACE", "Replace", "Set the island to the weight"),
            ("ADD", "Add", "Add the weight to the island"),
            ("SUBTRACT", "Subtract", "Subtract the weight from the island"),
        ],
        default="REPLACE",
    )
    use_brush_weight: BoolProperty(
        name="Use Brush Weight",
        default=True,
        description="Use the weight from your current brush. Turn off to use a custom value",
    )
    weight: FloatProperty(
        name="Weight",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )
    use_mirror: BoolProperty(
        name="Mirror X",
        default=True,
        description="Also fill the mirrored island when the mesh's X symmetry is on "
                    "(uses _L/_R style group names if Vertex Groups mirroring is on)",
    )
    normalize: BoolProperty(
        name="Normalize",
        default=False,
        description="Scale the other deform groups so each filled vertex adds up to 1.0 "
                    "(locked groups are left alone)",
    )


# helpers
def bucket_settings(context):
    return context.scene.weight_bucket


def paint_ready(context):
    obj = context.active_object
    if obj is None:
        return False
    if obj.type != "MESH":
        return False
    return obj.mode == "WEIGHT_PAINT"


def brush_weight(context):
    paint = context.tool_settings.weight_paint
    shared = None
    if hasattr(paint, "unified_paint_settings"):
        shared = paint.unified_paint_settings
    elif hasattr(context.tool_settings, "unified_paint_settings"):
        shared = context.tool_settings.unified_paint_settings
    if shared is not None and shared.use_unified_weight:
        return shared.weight
    if paint.brush is not None:
        return paint.brush.weight
    return 1.0


def deform_names(obj):
    names = set()
    for modifier in obj.modifiers:
        if modifier.type != "ARMATURE" or modifier.object is None:
            continue
        for bone in modifier.object.data.bones:
            if bone.use_deform:
                names.add(bone.name)
    if len(names) == 0:
        for group in obj.vertex_groups:
            names.add(group.name)
    return names


# which part of the view mouse's over
def view_under_mouse(context, event):
    area = context.area
    if area is None or area.type != "VIEW_3D":
        return None
    big_view = None
    for region in area.regions:
        inside_x = region.x <= event.mouse_x < region.x + region.width
        inside_y = region.y <= event.mouse_y < region.y + region.height
        if not (inside_x and inside_y):
            continue
        if region.type == "WINDOW":
            big_view = region
        else:
            return None
    return big_view


# where every vert is right now
def posed_spots(context, obj):
    mesh = obj.data
    depsgraph = context.evaluated_depsgraph_get()
    posed_obj = obj.evaluated_get(depsgraph)
    posed_mesh = posed_obj.to_mesh()
    spots = []
    if len(posed_mesh.vertices) == len(mesh.vertices):
        for vert in posed_mesh.vertices:
            spots.append(vert.co.copy())
    else:
        for vert in mesh.vertices:
            spots.append(vert.co.copy())
    posed_obj.to_mesh_clear()
    return spots


# math... gulp
def face_under_mouse(context, obj, region, mouse_x, mouse_y):
    view = region.data
    mouse = (mouse_x, mouse_y)

    ray_start = view3d_utils.region_2d_to_origin_3d(region, view, mouse)
    ray_aim = view3d_utils.region_2d_to_vector_3d(region, view, mouse)

    to_local = obj.matrix_world.inverted()
    local_start = to_local @ ray_start
    local_aim = to_local.to_3x3() @ ray_aim
    local_aim.normalize()

    mesh = obj.data
    skip_hidden = mesh.use_paint_mask or mesh.use_paint_mask_vertex
    faces = []
    face_numbers = []
    for face in mesh.polygons:
        if skip_hidden and face.hide:
            continue
        faces.append(tuple(face.vertices))
        face_numbers.append(face.index)
    if len(faces) == 0:
        return None

    tree = BVHTree.FromPolygons(posed_spots(context, obj), faces)
    hit_spot, hit_normal, hit_number, hit_distance = tree.ray_cast(local_start, local_aim)
    if hit_number is None:
        return None
    return face_numbers[hit_number]


# island
def find_island(mesh, start_face):
    faces = mesh.polygons
    only_selected = mesh.use_paint_mask

    faces_at_vert = []
    for vert in mesh.vertices:
        faces_at_vert.append([])
    for face in faces:
        for vert_index in face.vertices:
            faces_at_vert[vert_index].append(face.index)

    island = set()
    seen_faces = {start_face}
    to_do = [start_face]
    while to_do:
        face_index = to_do.pop()
        for vert_index in faces[face_index].vertices:
            if vert_index in island:
                continue
            island.add(vert_index)
            for neighbor in faces_at_vert[vert_index]:
                if neighbor in seen_faces:
                    continue
                if only_selected and not faces[neighbor].select:
                    continue
                seen_faces.add(neighbor)
                to_do.append(neighbor)
    return island


# mirrored verts
def find_twin_verts(mesh, verts):
    tree = kdtree.KDTree(len(mesh.vertices))
    for vert in mesh.vertices:
        tree.insert(vert.co, vert.index)
    tree.balance()

    twins = set()
    for vert_index in verts:
        spot = mesh.vertices[vert_index].co
        mirror_spot = Vector((-spot.x, spot.y, spot.z))
        found_spot, found_index, gap = tree.find(mirror_spot)
        reach = twin_reach
        if abs(spot.x) > 1.0:
            reach = twin_reach * abs(spot.x)
        if found_index is not None and gap < reach:
            twins.add(found_index)
    return twins


# normalize (weights should add up to 1)
def squeeze_others(obj, vert_index, group, new_weight, bone_names):
    groups = obj.vertex_groups
    locked_total = 0.0
    free_total = 0.0
    free_slots = []
    for slot in obj.data.vertices[vert_index].groups:
        if slot.group == group.index:
            continue
        other = groups[slot.group]
        if other.name not in bone_names or slot.weight <= 0:
            continue
        if other.lock_weight:
            locked_total += slot.weight
        else:
            free_total += slot.weight
            free_slots.append(slot)
    if free_total == 0:
        return

    room = 1.0 - new_weight - locked_total
    if room < 0.0:
        room = 0.0
    scale = room / free_total
    for slot in free_slots:
        groups[slot.group].add([vert_index], slot.weight * scale, "REPLACE")


# fill with weight
def fill_verts(obj, group, verts, value, mode, normalize, bone_names):
    for vert_index in verts:
        old_weight = 0.0
        for slot in obj.data.vertices[vert_index].groups:
            if slot.group == group.index:
                old_weight = slot.weight
                break

        if mode == "REPLACE":
            new_weight = value
        elif mode == "ADD":
            new_weight = old_weight + value
        else:
            new_weight = old_weight - value
        if new_weight < 0.0:
            new_weight = 0.0
        if new_weight > 1.0:
            new_weight = 1.0

        group.add([vert_index], new_weight, "REPLACE")
        if normalize:
            squeeze_others(obj, vert_index, group, new_weight, bone_names)


# whole thingy
def fill_at_mouse(operator, context, event):
    obj = context.active_object
    if not paint_ready(context):
        operator.report({"WARNING"}, "Weight Bucket works in Weight Paint mode on a mesh")
        return False
    group = obj.vertex_groups.active
    if group is None:
        operator.report({"WARNING"}, "No active vertex group — pick one first")
        return False
    if group.lock_weight:
        operator.report({"WARNING"}, f"'{group.name}' is locked")
        return False

    region = view_under_mouse(context, event)
    if region is None:
        return False
    mouse_x = event.mouse_x - region.x
    mouse_y = event.mouse_y - region.y
    face_index = face_under_mouse(context, obj, region, mouse_x, mouse_y)
    if face_index is None:
        return False  # clicked empty space :p

    mesh = obj.data
    if mesh.use_paint_mask and not mesh.polygons[face_index].select:
        operator.report({"WARNING"}, "That face is outside the face mask")
        return False

    settings = bucket_settings(context)
    if settings.use_brush_weight:
        value = brush_weight(context)
    else:
        value = settings.weight

    verts = find_island(mesh, face_index)
    if mesh.use_paint_mask_vertex:
        selected = set()
        for vert_index in verts:
            if mesh.vertices[vert_index].select:
                selected.add(vert_index)
        verts = selected
    if len(verts) == 0:
        return False

    bone_names = deform_names(obj)
    fill_verts(obj, group, verts, value, settings.mode, settings.normalize, bone_names)
    message = f"Filled {len(verts)} verts in '{group.name}' ({settings.mode.lower()} {value:.2f})"

    if settings.use_mirror and mesh.use_mirror_x:  # mirror
        twins = find_twin_verts(mesh, verts) - verts  # minus just filled
        if len(twins) > 0:
            twin_group = group
            if mesh.use_mirror_vertex_groups:
                flipped = bpy.utils.flip_name(group.name)
                twin_group = obj.vertex_groups.get(flipped)
                if twin_group is None:
                    active_number = obj.vertex_groups.active_index
                    twin_group = obj.vertex_groups.new(name=flipped)
                    obj.vertex_groups.active_index = active_number
            if not twin_group.lock_weight:
                fill_verts(obj, twin_group, twins, value, settings.mode, settings.normalize, bone_names)
                message += f" + {len(twins)} mirrored in '{twin_group.name}'"

    mesh.update()  # tell blender weights changed so the view upd
    obj.update_tag()
    operator.report({"INFO"}, message)
    return True


# buttons
class WBUCKET_OT_fill(bpy.types.Operator):
    bl_idname = "paint.weight_bucket_fill"
    bl_label = "Weight Bucket Fill"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return paint_ready(context)

    def invoke(self, context, event):  # note to self, invoke = execute
        if fill_at_mouse(self, context, event):
            return {"FINISHED"}
        return {"CANCELLED"}


# bucket mode: stays on every click until esc/right click
class WBUCKET_OT_mode(bpy.types.Operator):
    bl_idname = "paint.weight_bucket_mode"
    bl_label = "Bucket Mode"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if bucket_is_on:
            return False  # already running, the button turns gray, it's normal!!
        if not paint_ready(context):
            return False
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context, event):
        global bucket_is_on  # global = change the one at the top
        bucket_is_on = True
        context.window_manager.modal_handler_add(self)  # blender sends every event to modal()
        context.window.cursor_modal_set("PAINT_BRUSH")
        context.area.header_text_set("Weight Bucket: click an island to fill · Right-click / Esc to finish")
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def finish(self, context):
        global bucket_is_on
        bucket_is_on = False
        if context.window:
            context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
            context.area.tag_redraw()

    def cancel(self, context):
        self.finish(context)

    def modal(self, context, event):
        if not paint_ready(context):
            self.finish(context)
            return {"CANCELLED"}
        if event.type in {"RIGHTMOUSE", "ESC"} and event.value == "PRESS":
            self.finish(context)
            return {"FINISHED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if view_under_mouse(context, event) is None:
                return {"PASS_THROUGH"}
            if fill_at_mouse(self, context, event):
                bpy.ops.ed.undo_push(message="Weight Bucket Fill")
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}


# sidebar
class WBUCKET_PT_panel(bpy.types.Panel):
    bl_label = "Weight Bucket"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AESU"
    bl_context = "weightpaint"  # only show in weight paint. hashtag clean

    def draw(self, context):
        settings = bucket_settings(context)
        column = self.layout.column()
        column.operator(WBUCKET_OT_mode.bl_idname, icon="BRUSH_DATA")
        column.label(text="Or hover an island and press Alt+F", icon="INFO")
        column.separator()
        column.prop(settings, "mode", expand=True)
        column.prop(settings, "use_brush_weight")
        row = column.row()
        row.enabled = not settings.use_brush_weight
        row.prop(settings, "weight")
        column.prop(settings, "use_mirror")
        column.prop(settings, "normalize")
        obj = context.active_object
        if obj and obj.vertex_groups.active:
            column.separator()
            column.label(text=f"Group: {obj.vertex_groups.active.name}", icon="GROUP_VERTEX")


# register/unregister
bucket_classes = (  # tuple
    WBUCKET_Settings,
    WBUCKET_OT_fill,
    WBUCKET_OT_mode,
    WBUCKET_PT_panel,
)
bucket_keys = []  # hotkey placeholder


def register():  # turn addon on
    global bucket_is_on
    bucket_is_on = False
    for bucket_class in bucket_classes:
        bpy.utils.register_class(bucket_class)
    bpy.types.Scene.weight_bucket = PointerProperty(type=WBUCKET_Settings)
    # hotkey: Alt+F in weight paint!!
    key_config = bpy.context.window_manager.keyconfigs.addon
    if key_config:
        key_map = key_config.keymaps.new(name="Weight Paint", space_type="EMPTY")
        key = key_map.keymap_items.new(WBUCKET_OT_fill.bl_idname, "F", "PRESS", alt=True)
        bucket_keys.append((key_map, key))


def unregister():  # turn addon off
    for key_map, key in bucket_keys:
        key_map.keymap_items.remove(key)
    bucket_keys.clear()
    del bpy.types.Scene.weight_bucket
    for bucket_class in reversed(bucket_classes):
        bpy.utils.unregister_class(bucket_class)


if __name__ == "__main__":
    register()
