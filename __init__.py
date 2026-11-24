import os
import json
import uuid
import shutil
import subprocess
import bpy
from mathutils import Vector, Quaternion
from bpy.props import (
    StringProperty,
    BoolProperty,
    IntProperty,
    FloatProperty,
    FloatVectorProperty,
    EnumProperty,
    CollectionProperty,
)
from bpy.app.handlers import persistent

ADDON_NAME = "Smart Tabs"
ADDON_VERSION = "1.0.0"
ADDON_AUTHOR = "Graph Team"
ADDON_CONTACT = "graph.support@gmail.com"

# Global caches to prevent Blender C-RNA garbage collection glitches on dynamic EnumProperties
_SESSION_ENUM_CACHE = []
_SEARCH_ENUM_CACHE = []

# True while the add-on itself writes a temp buffer, so save_post can tell those
# internal writes apart from a user's File > Save landing on a temp buffer.
_INTERNAL_SAVE_DEPTH = 0


def internal_save_as_copy(filepath):
    """Write a copy of the current file without it counting as a user save."""
    global _INTERNAL_SAVE_DEPTH
    _INTERNAL_SAVE_DEPTH += 1
    try:
        bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=True)
    finally:
        _INTERNAL_SAVE_DEPTH -= 1


def get_any_window():
    """Return a usable window for deferred operator calls (timers have no window)."""
    win = getattr(bpy.context, "window", None)
    if win:
        return win
    wm = getattr(bpy.context, "window_manager", None)
    if wm and wm.windows:
        return wm.windows[0]
    return None


def call_deferred(func, delay=0.01):
    """Run func on a timer, with a window in context, after the current operator finishes."""
    def _run():
        win = get_any_window()
        try:
            if win:
                with bpy.context.temp_override(window=win, screen=win.screen):
                    func()
            else:
                func()
        except Exception as exc:
            print(f"[{ADDON_NAME}] deferred call failed: {exc}")
        return None
    bpy.app.timers.register(_run, first_interval=delay)


def close_tab_by_uid_deferred(uid):
    """Close the tab with this uid once the current save or load has finished."""
    def _close():
        prefs = get_addon_prefs(bpy.context)
        idx = find_tab_index_by_uid(prefs, uid)
        if idx != -1:
            bpy.ops.wm.close_blend_tab('EXEC_DEFAULT', index=idx)
    call_deferred(_close)


def get_addon_prefs(context=None):
    """Retrieve Extension preferences safely across 4.2+ package namespaces."""
    if context is None:
        context = bpy.context
    if not context or not getattr(context, "preferences", None):
        return None
    addon_key = __package__ if __package__ else __name__
    addon = context.preferences.addons.get(addon_key)
    return addon.preferences if addon else None


def mark_prefs_dirty(context=None):
    """Ensure Blender's Auto-Save Preferences persists tab bar state across restarts."""
    if context is None:
        context = bpy.context
    if context and getattr(context, "preferences", None):
        context.preferences.is_dirty = True


def tag_redraw_all_tab_bars(context=None):
    """Force immediate UI redraw of all Topbar and 3D Viewport headers."""
    if context is None:
        context = bpy.context
    wm = getattr(context, "window_manager", None)
    if not wm:
        return
    for win in wm.windows:
        if win.screen:
            for area in win.screen.areas:
                if area.type in {'TOPBAR', 'VIEW_3D'}:
                    area.tag_redraw()


def get_config_dir():
    """Return this extension's writable user directory (sessions, clipboard, unsaved buffers)."""
    return bpy.utils.extension_path_user(__package__, create=True)


def get_unsaved_dir():
    """Directory used to cache unsaved or duplicated project tabs while switching."""
    path = os.path.join(get_config_dir(), "unsaved_buffers")
    os.makedirs(path, exist_ok=True)
    return path


def get_clipboard_filepath():
    return os.path.join(get_config_dir(), "tab_clipboard.blend")


def get_clipboard_meta_filepath():
    return os.path.join(get_config_dir(), "tab_clipboard_meta.json")


def get_sessions_filepath():
    return os.path.join(get_config_dir(), "tab_sessions.json")


def is_temp_buffer_path(filepath):
    """Check whether a filepath points to an internal unsaved tab cache."""
    if not filepath:
        return False
    norm_fp = os.path.normcase(os.path.normpath(filepath))
    norm_dir = os.path.normcase(os.path.normpath(get_unsaved_dir()))
    return norm_fp.startswith(norm_dir)


def generate_next_untitled_name(prefs, exclude_item=None):
    """Generate the next available 'Untitled 1', 'Untitled 2', etc. tab label."""
    existing = {
        item.display_name
        for item in prefs.open_files
        if item != exclude_item and item.display_name
    }
    idx = 1
    while f"Untitled {idx}" in existing:
        idx += 1
    return f"Untitled {idx}"


def generate_duplicate_tab_name(prefs, base_name):
    """Generate a unique '(Copy)' display name for a duplicated project tab."""
    existing = {item.display_name for item in prefs.open_files if item.display_name}
    stem = base_name[:-6] if base_name.lower().endswith(".blend") else base_name
    candidate = f"{stem} (Copy)"
    if candidate not in existing:
        return candidate
    idx = 2
    while f"{stem} (Copy {idx})" in existing:
        idx += 1
    return f"{stem} (Copy {idx})"


def get_active_tab_item(prefs):
    """Read-only lookup of the currently active TAB_PG_file_item (safe inside UI draw callbacks)."""
    if not prefs or len(prefs.open_files) == 0:
        return None, -1

    # 1. Match by active_tab_uid first (handles multiple Untitled & Duplicate tabs accurately)
    if prefs.active_tab_uid:
        for idx, item in enumerate(prefs.open_files):
            if item.uid == prefs.active_tab_uid:
                return item, idx

    # 2. Read-only fallback: match by normalized disk filepath without mutating RNA state
    current_fp = bpy.data.filepath
    if current_fp and not is_temp_buffer_path(current_fp):
        norm_cur = os.path.normpath(current_fp)
        for idx, item in enumerate(prefs.open_files):
            if item.filepath and os.path.normpath(item.filepath) == norm_cur:
                return item, idx

    return prefs.open_files[0], 0


def find_tab_index_by_uid(prefs, uid):
    """Return the index of a tab matching uid, or -1 if not found."""
    if not prefs or not uid:
        return -1
    for idx, item in enumerate(prefs.open_files):
        if item.uid == uid:
            return idx
    return -1


def is_tab_item_dirty(item, is_active):
    """Return True if the tab has unsaved modifications (displays trailing '*')."""
    if is_active:
        return bool(bpy.data.is_dirty or item.is_virtual_dirty)
    return bool(item.is_virtual_dirty)


# -------------------------------------------------------------------
# 3D Viewport State Snapshot & Restore Helpers
# -------------------------------------------------------------------

def find_primary_view3d(window=None):
    """Find the largest active 3D Viewport SpaceView3D and RegionView3D safely."""
    if window is None:
        window = getattr(bpy.context, "window", None)
    if window is None:
        wm = getattr(bpy.context, "window_manager", None)
        if wm and wm.windows:
            window = wm.windows[0]

    if not window or not window.screen:
        return None, None, None

    best_area = None
    best_size = -1
    for area in window.screen.areas:
        if area.type == 'VIEW_3D':
            size = area.width * area.height
            if size > best_size:
                best_size = size
                best_area = area

    if best_area:
        space = best_area.spaces.active
        if space and space.type == 'VIEW_3D' and space.region_3d:
            return best_area, space, space.region_3d
    return None, None, None


def save_current_viewport_to_tab(context=None):
    """Store the current 3D Viewport perspective, transform, and shading in the active tab."""
    prefs = get_addon_prefs(context)
    if not prefs or not prefs.remember_viewport_state:
        return

    target_item, _ = get_active_tab_item(prefs)
    if not target_item:
        return

    win = getattr(context, "window", None) if context else None
    _area, space, r3d = find_primary_view3d(win)
    if not space or not r3d:
        return

    target_item.has_view_state = True
    target_item.view_location = tuple(r3d.view_location)
    target_item.view_rotation = tuple(r3d.view_rotation)
    target_item.view_distance = float(r3d.view_distance)
    target_item.view_perspective = r3d.view_perspective
    target_item.shading_type = space.shading.type
    mark_prefs_dirty(context)


def copy_viewport_state(src_item, dst_item):
    """Copy stored 3D Viewport state from one tab item to another."""
    dst_item.has_view_state = src_item.has_view_state
    dst_item.view_location = tuple(src_item.view_location)
    dst_item.view_rotation = tuple(src_item.view_rotation)
    dst_item.view_distance = float(src_item.view_distance)
    dst_item.view_perspective = src_item.view_perspective
    dst_item.shading_type = src_item.shading_type


def restore_viewport_from_active_tab():
    """Timer callback to apply the saved 3D Viewport camera and shading after tab switch."""
    prefs = get_addon_prefs(bpy.context)
    if not prefs or not prefs.remember_viewport_state:
        return None

    item, _ = get_active_tab_item(prefs)
    if item and item.has_view_state:
        area, space, r3d = find_primary_view3d(None)
        if space and r3d:
            r3d.view_location = Vector(item.view_location)
            r3d.view_rotation = Quaternion(item.view_rotation)
            r3d.view_distance = item.view_distance
            if item.view_perspective in {'PERSP', 'ORTHO', 'CAMERA'}:
                r3d.view_perspective = item.view_perspective
            if item.shading_type in {'WIREFRAME', 'SOLID', 'MATERIAL', 'RENDERED'}:
                try:
                    space.shading.type = item.shading_type
                except Exception:
                    pass
            if area:
                area.tag_redraw()
    return None


def get_safe_target_collection(context):
    """Return an active collection guaranteed to be visible/included in the current View Layer."""
    vl = context.view_layer
    active_lc = vl.active_layer_collection if vl else None
    if active_lc and not active_lc.exclude and not active_lc.hide_viewport:
        return active_lc.collection
    return context.scene.collection


def cache_active_untitled_tab(prefs):
    """If the active tab is an unsaved or duplicated project, snapshot it to a temp .blend file."""
    active_item, _ = get_active_tab_item(prefs)
    if not active_item or active_item.filepath:
        return

    if not active_item.uid:
        active_item.uid = uuid.uuid4().hex[:8]

    temp_path = os.path.join(get_unsaved_dir(), f"{active_item.uid}.blend")
    try:
        if bpy.data.is_dirty:
            active_item.is_virtual_dirty = True
        internal_save_as_copy(temp_path)
        active_item.temp_filepath = temp_path
    except Exception as exc:
        print(f"[{ADDON_NAME}] could not cache unsaved tab '{active_item.display_name}': {exc}")


def get_tab_buffer_path(item):
    if not item.uid:
        item.uid = uuid.uuid4().hex[:8]
    return os.path.join(get_unsaved_dir(), f"{item.uid}.blend")


def is_loaded_from_buffer(item):
    """True if the window currently shows this tab's temp buffer rather than its real file."""
    cur = bpy.data.filepath
    return bool(
        cur and item.temp_filepath and is_temp_buffer_path(cur)
        and os.path.normcase(os.path.normpath(cur)) == os.path.normcase(os.path.normpath(item.temp_filepath))
    )


def save_active_tab_to_disk(prefs):
    """Write the active saved tab to its real .blend file (a normal user-level save)."""
    item, _ = get_active_tab_item(prefs)
    if not item or not item.filepath:
        return False
    if is_loaded_from_buffer(item):
        bpy.ops.wm.save_as_mainfile(filepath=item.filepath, relative_remap=True)
    else:
        bpy.ops.wm.save_mainfile()
    return True


def stash_active_tab(prefs):
    """Protect the active tab's edits before another file is loaded into the window.

    Untitled tabs and saved projects with edits are written to a hidden buffer and keep
    their unsaved mark; the real .blend file is not touched. With auto_save_on_switch,
    saved projects are saved to their real file instead.
    """
    item, _ = get_active_tab_item(prefs)
    if not item:
        return
    if not item.filepath:
        cache_active_untitled_tab(prefs)
        return

    from_buffer = is_loaded_from_buffer(item)
    if not (bpy.data.is_dirty or from_buffer):
        return
    if prefs.auto_save_on_switch:
        save_active_tab_to_disk(prefs)
        return
    if from_buffer and not bpy.data.is_dirty and os.path.exists(item.temp_filepath):
        return
    buf = get_tab_buffer_path(item)
    try:
        internal_save_as_copy(buf)
        item.temp_filepath = buf
        item.is_virtual_dirty = True
    except Exception as exc:
        print(f"[{ADDON_NAME}] could not keep unsaved changes of '{item.display_name}': {exc}")
        raise


def has_buffered_changes(item):
    return bool(item.filepath and item.is_virtual_dirty and item.temp_filepath and os.path.exists(item.temp_filepath))


def write_tab_buffer_to_file(item):
    """Save a background tab's kept changes into its .blend file without opening it.

    The previous version of the file is kept as .blend1, like Blender's own save.
    Returns False if relative paths could not be remapped.
    """
    target = item.filepath
    if os.path.exists(target):
        try:
            shutil.copy2(target, target + "1")
        except Exception:
            pass
    remapped = copy_blend_with_remap(item.temp_filepath, target)
    remove_tab_temp_buffer(item)
    item.temp_filepath = ""
    item.is_virtual_dirty = False
    return remapped


MAX_CLOSED_TABS = 10


def get_closed_tabs_path():
    return os.path.join(get_config_dir(), "closed_tabs.json")


def load_closed_tabs():
    path = get_closed_tabs_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_closed_tabs(entries):
    try:
        with open(get_closed_tabs_path(), "w", encoding="utf-8") as f:
            json.dump(entries[-MAX_CLOSED_TABS:], f, indent=2)
    except Exception as exc:
        print(f"[{ADDON_NAME}] could not store closed tabs: {exc}")


def remember_closed_tab(item):
    """Note a saved project's tab so Reopen Closed Tab can bring it back."""
    if not item or not item.filepath:
        return
    norm = os.path.normcase(os.path.normpath(item.filepath))
    entries = [e for e in load_closed_tabs()
               if os.path.normcase(os.path.normpath(e.get("filepath", ""))) != norm]
    entries.append({
        "filepath": item.filepath,
        "display_name": item.display_name,
        "has_custom_label": bool(item.has_custom_label),
        "is_pinned": bool(item.is_pinned),
    })
    save_closed_tabs(entries)


def open_tab_item_in_window(prefs, target_item):
    """Load a TAB_PG_file_item (either saved .blend or cached Untitled buffer) into the window."""
    prefs.active_tab_uid = target_item.uid
    load_ui = not prefs.keep_current_ui

    if has_buffered_changes(target_item):
        bpy.ops.wm.open_mainfile(filepath=target_item.temp_filepath, load_ui=load_ui)
    elif target_item.filepath and os.path.exists(target_item.filepath):
        bpy.ops.wm.open_mainfile(filepath=target_item.filepath, load_ui=load_ui)
    elif target_item.temp_filepath and os.path.exists(target_item.temp_filepath):
        bpy.ops.wm.open_mainfile(filepath=target_item.temp_filepath, load_ui=load_ui)
    else:
        bpy.ops.wm.read_homefile(use_empty=False, load_ui=load_ui)


def copy_blend_with_remap(src, dst):
    """Copy a .blend file so its relative (//) paths still resolve from dst's folder.

    A plain file copy keeps paths relative to the *source* folder, which breaks textures and
    libraries once the copy lives in the cache folder. A background Blender re-saves the file
    with relative_remap instead. Returns False if it had to fall back to a plain copy.
    """
    src_dir = os.path.normcase(os.path.dirname(os.path.abspath(src)))
    dst_dir = os.path.normcase(os.path.dirname(os.path.abspath(dst)))
    if src_dir == dst_dir:
        shutil.copy2(src, dst)
        return True

    if os.path.exists(dst):
        os.remove(dst)
    expr = (
        "import bpy, sys; "
        "bpy.ops.wm.save_as_mainfile(filepath=sys.argv[sys.argv.index('--') + 1], "
        "copy=True, relative_remap=True)"
    )
    cmd = [
        bpy.app.binary_path,
        "--background", "--factory-startup", "--disable-autoexec",
        src,
        "--python-exit-code", "1",
        "--python-expr", expr,
        "--", dst,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode == 0 and os.path.exists(dst):
            return True
        print(f"[{ADDON_NAME}] remapped copy failed ({result.returncode}): {result.stderr[-500:]!r}")
    except Exception as exc:
        print(f"[{ADDON_NAME}] remapped copy failed: {exc}")

    shutil.copy2(src, dst)
    return False


def remove_tab_temp_buffer(item):
    """Safely delete an unsaved tab's temporary .blend buffer from disk."""
    if item and item.temp_filepath and os.path.exists(item.temp_filepath):
        try:
            os.remove(item.temp_filepath)
        except Exception:
            pass


def draw_about_support_ui(layout):
    """Render the Graph Team About & Support information block and utility buttons."""
    box = layout.box()
    col = box.column(align=True)
    col.label(text=f"{ADDON_NAME} v{ADDON_VERSION}", icon='INFO')
    col.separator(factor=0.5)
    col.label(text=f"Author: {ADDON_AUTHOR}", icon='USER')
    col.label(text=f"Contact: {ADDON_CONTACT}", icon='URL')
    col.separator(factor=0.8)

    btn_row = col.row(align=True)
    btn_row.operator("wm.copy_support_email", text="Copy Email", icon='COPYDOWN')

    col.separator(factor=0.4)
    col.operator("wm.open_tabs_config_folder", text="Open Data Folder", icon='FILE_FOLDER')


# -------------------------------------------------------------------
# Data Structures & Persistent Preferences
# -------------------------------------------------------------------

class TAB_PG_file_item(bpy.types.PropertyGroup):
    uid: StringProperty(
        name="Tab UID",
        description="Unique internal identifier for this tab",
    )
    display_name: StringProperty(
        name="Tab Title",
        description="Display title shown in the project tab bar",
        default="",
    )
    has_custom_label: BoolProperty(
        name="Has Custom Label",
        description="Whether this tab uses a locked custom label that persists even after saving to disk",
        default=False,
    )
    filepath: StringProperty(
        name="File Path",
        description="Full disk path to the saved .blend project file (empty for Untitled/Duplicated tabs)",
        subtype='FILE_PATH',
    )
    temp_filepath: StringProperty(
        name="Temp Buffer Path",
        description="Temporary .blend buffer path for unsaved or duplicated tabs",
        subtype='FILE_PATH',
    )
    is_virtual_dirty: BoolProperty(
        name="Unsaved Buffer Flag",
        default=False,
    )
    is_pinned: BoolProperty(
        name="Pin Tab",
        description="Protect this tab when closing unpinned tabs",
        default=False,
    )
    has_view_state: BoolProperty(default=False)
    view_location: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    view_rotation: FloatVectorProperty(size=4, default=(1.0, 0.0, 0.0, 0.0))
    view_distance: FloatProperty(default=10.0)
    view_perspective: StringProperty(default='PERSP')
    shading_type: StringProperty(default='SOLID')


def get_session_enum_items(self, context):
    """Dynamically populate saved tab session presets with global reference caching."""
    global _SESSION_ENUM_CACHE
    _SESSION_ENUM_CACHE.clear()

    sessions_file = get_sessions_filepath()
    if os.path.exists(sessions_file):
        try:
            with open(sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for idx, name in enumerate(sorted(data.keys())):
                count = len(data[name])
                _SESSION_ENUM_CACHE.append(
                    (name, f"{name} ({count} files)", f"Load session '{name}'", 'FILE_FOLDER', idx)
                )
        except Exception:
            pass
    if not _SESSION_ENUM_CACHE:
        _SESSION_ENUM_CACHE.append(('NONE', "No Saved Sessions", "Save a session below first", 'INFO', 0))
    return _SESSION_ENUM_CACHE


class TAB_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ if __package__ else __name__

    active_tab_uid: StringProperty(
        name="Active Tab UID",
        default="",
    )

    tab_mode: EnumProperty(
        name="Tab Source",
        description="Switch between multi-file .blend Project Tabs or in-file Scenes",
        items=[
            ('FILES', "Projects (.blend)", "Project tabs for switching .blend files", 'FILE_BLEND', 0),
            ('SCENES', "In-File Scenes", "Tabs for switching Scenes inside the current .blend file", 'SCENE_DATA', 1),
        ],
        default='FILES',
    )

    auto_save_on_switch: BoolProperty(
        name="Auto-Save Saved Projects on Switch",
        description="Save changed projects to their .blend file when switching tabs. When off, changes are kept unsaved in the tab until you save",
        default=False,
    )

    keep_current_ui: BoolProperty(
        name="Keep Current Workspace Layout",
        description="Ignore target file UI layout (load_ui=False) so workspaces stay consistent across tabs",
        default=True,
    )

    remember_viewport_state: BoolProperty(
        name="Remember 3D Viewport Camera & Shading Per Tab",
        description="Preserve each tab's 3D Viewport camera angle, zoom, perspective, and shading mode",
        default=True,
    )

    override_standard_copy_paste: BoolProperty(
        name="Use Standard Ctrl+C / Ctrl+V Across Tabs",
        description="Bind Ctrl+C and Ctrl+V in Object Mode & Outliner to copy/paste full objects, materials, and node groups across tabs",
        default=True,
    )

    reuse_existing_materials: BoolProperty(
        name="Reuse Existing Materials & Node Groups",
        description="When pasting, remap matching materials, node groups, and textures to existing ones instead of creating .001 duplicates",
        default=True,
    )

    include_all_materials: BoolProperty(
        name="Include All File Materials on Copy",
        description="Also copy unassigned materials and node groups from the current .blend file",
        default=False,
    )

    include_modifier_targets: BoolProperty(
        name="Include Modifier & Constraint Targets",
        description="Automatically include helper objects referenced by modifiers (e.g., Boolean cutters, Mirror objects, Armatures)",
        default=True,
    )

    include_world: BoolProperty(
        name="Include World Shader / HDRI on Copy",
        description="Copy and paste the active Scene's World environment lighting along with objects and materials",
        default=False,
    )

    paste_at_cursor: BoolProperty(
        name="Paste Objects at 3D Cursor",
        description="Center pasted objects on the 3D Cursor while keeping relative offsets",
        default=False,
    )

    link_from_source: BoolProperty(
        name="Link Instead of Append (When Source Saved)",
        description="Library-link objects and materials directly from the source .blend tab instead of making local copies",
        default=False,
    )

    session_name_input: StringProperty(
        name="Session Name",
        description="Name for saving the current group of open .blend tabs",
        default="My Project Session",
    )

    selected_session: EnumProperty(
        name="Saved Sessions",
        description="Select a saved tab session to load or delete",
        items=get_session_enum_items,
    )

    open_files: CollectionProperty(type=TAB_PG_file_item)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "tab_mode", expand=True)
        col.separator()

        box_switch = layout.box()
        box_switch.label(text="Tab Switching & Viewport Memory:", icon='VIEW3D')
        box_switch.prop(self, "auto_save_on_switch")
        box_switch.prop(self, "keep_current_ui")
        box_switch.prop(self, "remember_viewport_state")

        box_clip = layout.box()
        box_clip.label(text="Cross-Tab Asset & Material Transfer:", icon='MATERIAL')
        box_clip.prop(self, "override_standard_copy_paste")
        box_clip.prop(self, "reuse_existing_materials")
        box_clip.prop(self, "include_modifier_targets")
        box_clip.prop(self, "include_all_materials")
        box_clip.prop(self, "include_world")
        box_clip.prop(self, "paste_at_cursor")
        box_clip.prop(self, "link_from_source")

        layout.separator()
        draw_about_support_ui(layout)


# -------------------------------------------------------------------
# Operators: Easy Asset Transfer (Ctrl+C / Ctrl+V Across Tabs)
# -------------------------------------------------------------------

class WM_OT_copy_tab_data(bpy.types.Operator):
    """Copy selected objects, materials, textures, modifiers, and node groups to the cross-tab clipboard"""
    bl_idname = "wm.copy_tab_data"
    bl_label = "Copy Objects & Materials to Tab Clipboard"

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not event.alt:
            if prefs and not prefs.override_standard_copy_paste:
                return {'PASS_THROUGH'}
            if context.area and context.area.type == 'VIEW_3D' and context.mode != 'OBJECT':
                return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        clipboard_path = get_clipboard_filepath()
        meta_path = get_clipboard_meta_filepath()
        datablocks = set()

        selected_objs = list(context.selected_objects)

        for obj in selected_objs:
            datablocks.add(obj)

            for slot in obj.material_slots:
                if slot.material:
                    datablocks.add(slot.material)
            if obj.data and hasattr(obj.data, "materials"):
                for mat in obj.data.materials:
                    if mat:
                        datablocks.add(mat)

            for psys in obj.particle_systems:
                if psys.settings:
                    datablocks.add(psys.settings)
                    if psys.settings.instance_object:
                        datablocks.add(psys.settings.instance_object)
                    if psys.settings.instance_collection:
                        datablocks.add(psys.settings.instance_collection)

            if not prefs or prefs.include_modifier_targets:
                for mod in obj.modifiers:
                    for prop_name in (
                        "object",
                        "target",
                        "mirror_object",
                        "object_from",
                        "object_to",
                        "curve",
                        "project_limit_object",
                    ):
                        target_obj = getattr(mod, prop_name, None)
                        if isinstance(target_obj, bpy.types.Object):
                            datablocks.add(target_obj)
                    if hasattr(mod, "node_group") and mod.node_group:
                        datablocks.add(mod.node_group)

                for con in obj.constraints:
                    target_obj = getattr(con, "target", None)
                    if isinstance(target_obj, bpy.types.Object):
                        datablocks.add(target_obj)

        if not selected_objs or (prefs and prefs.include_all_materials):
            for mat in bpy.data.materials:
                if not mat.is_grease_pencil:
                    datablocks.add(mat)
            for ng in bpy.data.node_groups:
                datablocks.add(ng)

        copied_world_name = ""
        if prefs and prefs.include_world and context.scene and context.scene.world:
            datablocks.add(context.scene.world)
            copied_world_name = context.scene.world.name

        if not datablocks:
            self.report({'WARNING'}, "Nothing selected and no materials found to copy.")
            return {'CANCELLED'}

        try:
            bpy.data.libraries.write(
                clipboard_path,
                datablocks,
                path_remap='ABSOLUTE',
                fake_user=True,
            )
            source_fp = bpy.data.filepath if (bpy.data.filepath and not is_temp_buffer_path(bpy.data.filepath)) else ""
            meta = {
                "source_blend": source_fp,
                "object_names": [o.name for o in datablocks if isinstance(o, bpy.types.Object)],
                "material_names": [m.name for m in datablocks if isinstance(m, bpy.types.Material)],
                "node_group_names": [n.name for n in datablocks if isinstance(n, bpy.types.NodeTree)],
                "world_name": copied_world_name,
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to write clipboard: {exc}")
            return {'CANCELLED'}

        if selected_objs and bpy.ops.view3d.copybuffer.poll():
            try:
                bpy.ops.view3d.copybuffer()
            except Exception:
                pass

        obj_count = sum(1 for item in datablocks if isinstance(item, bpy.types.Object))
        mat_count = sum(1 for item in datablocks if isinstance(item, bpy.types.Material))
        world_msg = " + World" if copied_world_name else ""
        self.report({'INFO'}, f"Copied {obj_count} object(s), {mat_count} material(s){world_msg}.")
        return {'FINISHED'}


class WM_OT_paste_tab_data(bpy.types.Operator):
    """Paste objects, materials, textures, and node groups from the cross-tab clipboard"""
    bl_idname = "wm.paste_tab_data"
    bl_label = "Paste Objects & Materials from Tab Clipboard"
    bl_options = {'REGISTER', 'UNDO'}

    reuse_existing: BoolProperty(
        name="Reuse Existing Materials & Node Groups",
        description="Remap pasted materials, node groups, and images to existing ones with the same name instead of creating .001 duplicates",
        default=True,
        options={'SKIP_SAVE'},
    )
    paste_at_cursor: BoolProperty(
        name="Paste at 3D Cursor",
        description="Center pasted objects on the 3D Cursor while keeping relative offsets",
        default=False,
        options={'SKIP_SAVE'},
    )
    apply_world: BoolProperty(
        name="Apply Copied World Shader",
        description="If a World shader was copied, assign it to the current Scene",
        default=True,
        options={'SKIP_SAVE'},
    )
    link_from_source: BoolProperty(
        name="Link from Source .blend",
        description="Link datablocks from the original saved .blend file instead of appending local copies",
        default=False,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not event.alt:
            if prefs and not prefs.override_standard_copy_paste:
                return {'PASS_THROUGH'}
            if context.area and context.area.type == 'VIEW_3D' and context.mode != 'OBJECT':
                return {'PASS_THROUGH'}
        if prefs:
            self.reuse_existing = prefs.reuse_existing_materials
            self.paste_at_cursor = prefs.paste_at_cursor
            self.apply_world = prefs.include_world
            self.link_from_source = prefs.link_from_source
        return self.execute(context)

    def execute(self, context):
        clipboard_path = get_clipboard_filepath()
        meta_path = get_clipboard_meta_filepath()
        if not os.path.exists(clipboard_path):
            if bpy.ops.view3d.pastebuffer.poll():
                return bpy.ops.view3d.pastebuffer()
            self.report({'WARNING'}, "Tab clipboard is empty.")
            return {'CANCELLED'}

        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}

        source_blend = meta.get("source_blend", "")
        use_live_link = (
            self.link_from_source
            and bool(source_blend)
            and os.path.exists(source_blend)
            and os.path.normpath(source_blend) != os.path.normpath(bpy.data.filepath or "")
        )
        lib_path = source_blend if use_live_link else clipboard_path

        existing_mats = {mat.name: mat for mat in bpy.data.materials}
        existing_ngs = {ng.name: ng for ng in bpy.data.node_groups}
        existing_imgs = {img.name: img for img in bpy.data.images}

        try:
            with bpy.data.libraries.load(lib_path, link=use_live_link) as (data_from, data_to):
                if use_live_link and meta:
                    wanted_objs = set(meta.get("object_names", []))
                    wanted_mats = set(meta.get("material_names", []))
                    wanted_ngs = set(meta.get("node_group_names", []))
                    wanted_world = meta.get("world_name", "")
                    orig_mat_names = [m for m in data_from.materials if m in wanted_mats]
                    orig_ng_names = [n for n in data_from.node_groups if n in wanted_ngs]
                    orig_img_names = []
                    data_to.objects = [o for o in data_from.objects if o in wanted_objs]
                    data_to.worlds = [w for w in data_from.worlds if w == wanted_world] if self.apply_world else []
                else:
                    orig_mat_names = list(data_from.materials)
                    orig_ng_names = list(data_from.node_groups)
                    orig_img_names = list(data_from.images)
                    data_to.objects = list(data_from.objects)
                    data_to.worlds = list(data_from.worlds) if self.apply_world else []

                data_to.materials = list(orig_mat_names)
                data_to.node_groups = list(orig_ng_names)
                data_to.images = list(orig_img_names)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to load clipboard data: {exc}")
            return {'CANCELLED'}

        if context.mode != 'OBJECT' and bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode='OBJECT')

        for obj in list(context.selected_objects):
            obj.select_set(False)

        target_collection = get_safe_target_collection(context)
        pasted_objects = []
        selectable_objects = []

        for obj in data_to.objects:
            if obj is not None:
                if obj.name not in target_collection.objects:
                    target_collection.objects.link(obj)
                pasted_objects.append(obj)
                if obj.name in context.view_layer.objects and not obj.hide_viewport and not obj.hide_select:
                    obj.select_set(True)
                    selectable_objects.append(obj)

        if selectable_objects:
            context.view_layer.objects.active = selectable_objects[0]

        if pasted_objects and self.paste_at_cursor and not use_live_link:
            pasted_set = set(pasted_objects)
            root_objs = [o for o in pasted_objects if o.parent not in pasted_set]
            if root_objs:
                center = sum((o.location for o in root_objs), Vector((0.0, 0.0, 0.0))) / len(root_objs)
                offset = context.scene.cursor.location - center
                for o in root_objs:
                    o.location += offset

        if self.apply_world and data_to.worlds:
            loaded_world = next((w for w in data_to.worlds if w is not None), None)
            if loaded_world and context.scene:
                context.scene.world = loaded_world

        reused_mat_count = 0
        reused_ng_count = 0
        final_materials = []

        if self.reuse_existing and not use_live_link:
            for orig_name, loaded_img in zip(orig_img_names, data_to.images):
                if loaded_img is not None and orig_name in existing_imgs:
                    existing_img = existing_imgs[orig_name]
                    if loaded_img != existing_img:
                        loaded_img.use_fake_user = False
                        loaded_img.user_remap(existing_img)
                        if loaded_img.name in bpy.data.images:
                            bpy.data.images.remove(loaded_img)

            for orig_name, loaded_ng in zip(orig_ng_names, data_to.node_groups):
                if loaded_ng is None:
                    continue
                if orig_name in existing_ngs:
                    existing_ng = existing_ngs[orig_name]
                    if loaded_ng != existing_ng:
                        loaded_ng.use_fake_user = False
                        loaded_ng.user_remap(existing_ng)
                        if loaded_ng.name in bpy.data.node_groups:
                            bpy.data.node_groups.remove(loaded_ng)
                        reused_ng_count += 1
                else:
                    loaded_ng.use_fake_user = True

            for orig_name, loaded_mat in zip(orig_mat_names, data_to.materials):
                if loaded_mat is None:
                    continue
                if orig_name in existing_mats:
                    existing_mat = existing_mats[orig_name]
                    if loaded_mat != existing_mat:
                        loaded_mat.use_fake_user = False
                        loaded_mat.user_remap(existing_mat)
                        if loaded_mat.name in bpy.data.materials:
                            bpy.data.materials.remove(loaded_mat)
                        reused_mat_count += 1
                    final_materials.append(existing_mat)
                else:
                    loaded_mat.use_fake_user = True
                    final_materials.append(loaded_mat)
        else:
            for loaded_ng in data_to.node_groups:
                if loaded_ng is not None and not use_live_link:
                    loaded_ng.use_fake_user = True
            for loaded_mat in data_to.materials:
                if loaded_mat is not None:
                    if not use_live_link:
                        loaded_mat.use_fake_user = True
                    final_materials.append(loaded_mat)

        mode_label = "Linked" if use_live_link else "Pasted"
        if self.reuse_existing and (reused_mat_count > 0 or reused_ng_count > 0):
            self.report(
                {'INFO'},
                f"{mode_label} {len(pasted_objects)} object(s). Reused {reused_mat_count} material(s) & {reused_ng_count} node group(s).",
            )
        else:
            self.report(
                {'INFO'},
                f"{mode_label} {len(pasted_objects)} object(s) and {len(final_materials)} material(s).",
            )
        return {'FINISHED'}


# -------------------------------------------------------------------
# Copy / Paste of modifiers, materials and object settings (object right-click menu)
# -------------------------------------------------------------------

# Object types whose settings can be copied, mapped to (menu label, bpy.data collection).
SETTINGS_TYPES = {
    'CAMERA': ("Camera", "cameras"),
    'LIGHT': ("Light", "lights"),
    'LIGHT_PROBE': ("Light Probe", "lightprobes"),
    'SPEAKER': ("Speaker", "speakers"),
    'CURVE': ("Curve", "curves"),
    'FONT': ("Text", "curves"),
    'EMPTY': ("Empty", None),
}

EMPTY_SETTING_PROPS = (
    "empty_display_type", "empty_display_size", "empty_image_offset", "empty_image_depth",
    "empty_image_side", "show_empty_image_orthographic", "show_empty_image_perspective",
    "show_empty_image_only_axis_aligned", "use_empty_image_alpha",
)

# Properties never copied by "settings": identity, bookkeeping and actual content.
_SETTINGS_SKIP = {
    "rna_type", "name", "name_full", "users", "use_fake_user", "use_extra_user", "is_evaluated",
    "original", "session_uid", "is_missing", "is_runtime_data", "is_embedded_data",
    "is_library_indirect", "library", "library_weak_reference", "asset_data", "override_library",
    "preview", "tag", "is_editmode", "id_type", "animation_data", "shape_keys", "body",
    "filepath", "type", "materials", "texspace_location", "texspace_size", "use_auto_texspace",
}


def get_attr_clipboard_paths():
    base = get_config_dir()
    return os.path.join(base, "attr_clipboard.blend"), os.path.join(base, "attr_clipboard.json")


def read_attr_clipboard_meta():
    _blend, meta_path = get_attr_clipboard_paths()
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def modifier_dependencies(obj):
    """Objects and node groups the object's modifiers point at (Boolean cutters, mirror objects...)."""
    deps = set()
    for mod in obj.modifiers:
        for prop_name in ("object", "target", "mirror_object", "object_from", "object_to",
                          "curve", "project_limit_object", "offset_object", "start_cap", "end_cap",
                          "collection", "texture"):
            val = getattr(mod, prop_name, None)
            if isinstance(val, bpy.types.ID):
                deps.add(val)
        if getattr(mod, "node_group", None):
            deps.add(mod.node_group)
    return deps


def copy_rna_settings(src, dst, depth=0):
    """Copy every writable setting from src to dst, recursing into nested settings structs."""
    for prop in src.bl_rna.properties:
        pid = prop.identifier
        if pid in _SETTINGS_SKIP:
            continue
        if prop.type == 'COLLECTION':
            continue
        if prop.type == 'POINTER':
            value = getattr(src, pid, None)
            if prop.is_readonly:
                sub_dst = getattr(dst, pid, None)
                if (value is not None and sub_dst is not None and depth < 4
                        and not isinstance(value, bpy.types.ID)):
                    copy_rna_settings(value, sub_dst, depth + 1)
                continue
            try:
                setattr(dst, pid, value)
            except Exception:
                pass
            continue
        if prop.is_readonly:
            continue
        try:
            setattr(dst, pid, getattr(src, pid))
        except Exception:
            pass


def settings_label(obj):
    entry = SETTINGS_TYPES.get(obj.type) if obj else None
    return entry[0] if entry else ""


class OBJECT_OT_smart_tabs_copy_attributes(bpy.types.Operator):
    """Copy the active object's modifiers, materials or settings so they can be pasted onto other objects, in this or another tab"""
    bl_idname = "object.smart_tabs_copy_attributes"
    bl_label = "Copy Attributes"

    kind: EnumProperty(
        name="What",
        items=[
            ('MODIFIERS', "Modifiers", "Copy the whole modifier stack"),
            ('MATERIALS', "Materials", "Copy all material slots"),
            ('SETTINGS', "Settings", "Copy the camera, light or other object settings"),
        ],
        default='MODIFIERS',
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        obj = context.active_object
        prefs = get_addon_prefs(context)
        blend_path, meta_path = get_attr_clipboard_paths()
        meta = {
            "kind": self.kind,
            "object": obj.name,
            "obj_type": obj.type,
            "tab_uid": prefs.active_tab_uid if prefs else "",
            "file": bpy.data.filepath,
        }

        if self.kind == 'MODIFIERS':
            if not obj.modifiers:
                self.report({'WARNING'}, f"'{obj.name}' has no modifiers.")
                return {'CANCELLED'}
            blocks = {obj} | modifier_dependencies(obj)
            meta["items"] = [m.name for m in obj.modifiers]
        elif self.kind == 'MATERIALS':
            if not obj.material_slots:
                self.report({'WARNING'}, f"'{obj.name}' has no materials.")
                return {'CANCELLED'}
            mats = [slot.material for slot in obj.material_slots]
            blocks = {m for m in mats if m}
            meta["items"] = [m.name if m else "" for m in mats]
            if not blocks:
                blocks = {obj}
        else:
            if obj.type not in SETTINGS_TYPES:
                self.report({'WARNING'}, f"Settings can't be copied from a {obj.type.title()} object.")
                return {'CANCELLED'}
            if obj.type == 'EMPTY':
                blocks = {obj}
            else:
                blocks = {obj.data}
                meta["data"] = obj.data.name
            meta["items"] = [settings_label(obj)]

        try:
            bpy.data.libraries.write(blend_path, blocks, path_remap='ABSOLUTE', fake_user=True)
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not copy: {exc}")
            return {'CANCELLED'}

        if self.kind == 'SETTINGS':
            self.report({'INFO'}, f"Copied {settings_label(obj)} settings from '{obj.name}'.")
        else:
            self.report({'INFO'}, f"Copied {len(meta['items'])} {self.kind.lower()} from '{obj.name}'.")
        return {'FINISHED'}


class OBJECT_OT_smart_tabs_paste_attributes(bpy.types.Operator):
    """Paste copied modifiers, materials or settings onto the selected objects"""
    bl_idname = "object.smart_tabs_paste_attributes"
    bl_label = "Paste Attributes"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Mode",
        items=[
            ('REPLACE', "Replace", "Remove what the objects have and use the copied ones"),
            ('ADD', "Add", "Keep what the objects have and add the copied ones after it"),
        ],
        default='REPLACE',
        options={'SKIP_SAVE'},
    )
    reuse_existing: BoolProperty(
        name="Reuse Existing Materials",
        description="Use a material already in this file with the same name instead of adding a .001 copy",
        default=True,
        options={'SKIP_SAVE'},
    )

    @classmethod
    def poll(cls, context):
        return bool(context.selected_objects) and context.mode == 'OBJECT'

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if prefs:
            self.reuse_existing = prefs.reuse_existing_materials
        return self.execute(context)

    def draw(self, context):
        meta = read_attr_clipboard_meta()
        layout = self.layout
        if meta.get("kind") in ('MODIFIERS', 'MATERIALS'):
            layout.prop(self, "mode", expand=True)
        if meta.get("kind") == 'MATERIALS':
            layout.prop(self, "reuse_existing")

    # -- helpers -------------------------------------------------------------
    def _is_same_project(self, context, meta):
        prefs = get_addon_prefs(context)
        uid = prefs.active_tab_uid if prefs else ""
        if meta.get("tab_uid") and meta.get("tab_uid") == uid:
            return True
        return bool(meta.get("file")) and meta.get("file") == bpy.data.filepath

    def _targets(self, context, src_obj=None):
        return [o for o in context.selected_editable_objects if o is not src_obj]

    def _new_ids(self, before):
        after = {}
        for coll_name, old in before.items():
            coll = getattr(bpy.data, coll_name)
            after[coll_name] = [i for i in coll if i not in old]
        return after

    def _snapshot_ids(self):
        names = ("objects", "meshes", "curves", "materials", "node_groups", "images",
                 "cameras", "lights", "lightprobes", "speakers", "textures", "collections")
        return {n: set(getattr(bpy.data, n)) for n in names}

    def _cleanup_orphans(self, new_ids, keep=()):
        for coll_name in ("meshes", "curves", "materials", "images", "textures",
                          "cameras", "lights", "lightprobes", "speakers"):
            coll = getattr(bpy.data, coll_name)
            for idb in new_ids.get(coll_name, []):
                if idb in keep:
                    continue
                try:
                    if idb.users == 0 or (idb.use_fake_user and idb.users == 1):
                        coll.remove(idb)
                except ReferenceError:
                    pass

    # -- main ----------------------------------------------------------------
    def execute(self, context):
        meta = read_attr_clipboard_meta()
        blend_path, _meta_path = get_attr_clipboard_paths()
        kind = meta.get("kind")
        if not kind or not os.path.exists(blend_path):
            self.report({'WARNING'}, "Nothing copied yet. Right-click an object and choose Copy Modifiers, Copy Materials or Copy Settings.")
            return {'CANCELLED'}

        same = self._is_same_project(context, meta)
        if kind == 'MODIFIERS':
            return self._paste_modifiers(context, meta, blend_path, same)
        if kind == 'MATERIALS':
            return self._paste_materials(context, meta, blend_path, same)
        return self._paste_settings(context, meta, blend_path, same)

    def _paste_modifiers(self, context, meta, blend_path, same):
        src = bpy.data.objects.get(meta.get("object", "")) if same else None
        new_ids = {}
        if src is None:
            before = self._snapshot_ids()
            try:
                with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                    data_to.objects = [n for n in data_from.objects if n == meta.get("object")]
            except Exception as exc:
                self.report({'ERROR'}, f"Could not read the copied modifiers: {exc}")
                return {'CANCELLED'}
            src = data_to.objects[0] if data_to.objects else None
            if src is None:
                self.report({'ERROR'}, "The copied modifiers could not be loaded.")
                return {'CANCELLED'}
            new_ids = self._new_ids(before)
            temp_src = True
        else:
            temp_src = False

        targets = self._targets(context, src)
        if not targets:
            if temp_src:
                self._remove_temp_object(src, new_ids)
            self.report({'WARNING'}, "Select the objects to paste onto.")
            return {'CANCELLED'}

        failed = 0
        override = dict(
            active_object=src, object=src,
            selected_objects=[src] + targets, selected_editable_objects=[src] + targets,
        )
        with context.temp_override(**override):
            if self.mode == 'REPLACE':
                bpy.ops.object.make_links_data(type='MODIFIERS')
            else:
                for mod in src.modifiers:
                    try:
                        bpy.ops.object.modifier_copy_to_selected(modifier=mod.name)
                    except RuntimeError:
                        failed += 1

        if temp_src:
            # Objects the modifiers point at (Boolean cutters, mirror centers...) came along:
            # put them in the scene so they can be seen and edited.
            collection = get_safe_target_collection(context)
            for dep in new_ids.get("objects", []):
                if dep is not src and not dep.users_collection:
                    collection.objects.link(dep)
            self._remove_temp_object(src, new_ids)

        count = len(meta.get("items", []))
        msg = f"Pasted {count} modifier(s) onto {len(targets)} object(s)."
        if failed:
            msg += f" {failed} could not be added to some object types."
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    def _remove_temp_object(self, src, new_ids):
        bpy.data.objects.remove(src)
        self._cleanup_orphans(new_ids)

    def _paste_materials(self, context, meta, blend_path, same):
        names = meta.get("items", [])
        if same:
            mats = [bpy.data.materials.get(n) if n else None for n in names]
        else:
            existing = {m.name: m for m in bpy.data.materials}
            wanted = [n for n in names if n]
            try:
                with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                    data_to.materials = [n for n in data_from.materials if n in wanted]
                    loaded_names = list(data_to.materials)
            except Exception as exc:
                self.report({'ERROR'}, f"Could not read the copied materials: {exc}")
                return {'CANCELLED'}
            by_name = {}
            for orig, mat in zip(loaded_names, data_to.materials):
                if mat is None:
                    continue
                if self.reuse_existing and orig in existing and existing[orig] is not mat:
                    mat.use_fake_user = False
                    mat.user_remap(existing[orig])
                    bpy.data.materials.remove(mat)
                    by_name[orig] = existing[orig]
                else:
                    mat.use_fake_user = False
                    by_name[orig] = mat
            mats = [by_name.get(n) if n else None for n in names]

        targets = [o for o in context.selected_editable_objects
                   if o.data is not None and hasattr(o.data, "materials")]
        if not targets:
            self.report({'WARNING'}, "None of the selected objects can hold materials.")
            return {'CANCELLED'}

        for obj in targets:
            if self.mode == 'REPLACE':
                obj.data.materials.clear()
                for slot in obj.material_slots:
                    slot.material = None
            for mat in mats:
                obj.data.materials.append(mat)
        self.report({'INFO'}, f"Pasted {len(mats)} material(s) onto {len(targets)} object(s).")
        return {'FINISHED'}

    def _paste_settings(self, context, meta, blend_path, same):
        obj_type = meta.get("obj_type", "")
        label, coll_name = SETTINGS_TYPES.get(obj_type, ("", None))
        targets = [o for o in context.selected_editable_objects if o.type == obj_type]
        if not targets:
            self.report({'WARNING'}, f"Select {label.lower()} objects to paste {label} settings onto.")
            return {'CANCELLED'}

        before = self._snapshot_ids()
        src = None
        if obj_type == 'EMPTY':
            src = bpy.data.objects.get(meta.get("object", "")) if same else None
            temp = False
            if src is None:
                with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                    data_to.objects = [n for n in data_from.objects if n == meta.get("object")]
                src = data_to.objects[0] if data_to.objects else None
                temp = True
        else:
            coll = getattr(bpy.data, coll_name)
            src = coll.get(meta.get("data", "")) if same else None
            if src is None:
                with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
                    setattr(data_to, coll_name, [n for n in getattr(data_from, coll_name) if n == meta.get("data")])
                loaded = getattr(data_to, coll_name)
                src = loaded[0] if loaded else None
                temp = True
            else:
                temp = False
        if src is None:
            self.report({'ERROR'}, "The copied settings could not be loaded.")
            return {'CANCELLED'}

        changed = 0
        for obj in targets:
            if obj_type == 'EMPTY':
                if obj is src:
                    continue
                for pid in EMPTY_SETTING_PROPS:
                    try:
                        setattr(obj, pid, getattr(src, pid))
                    except Exception:
                        pass
                if src.data is not None:
                    obj.data = src.data
                changed += 1
                continue
            dst = obj.data
            if dst is None or dst == src:
                continue
            src_type = getattr(src, "type", None)
            if src_type is not None:
                try:
                    dst.type = src_type
                except Exception:
                    pass
                dst = obj.data  # re-fetch: changing a light's type changes its settings
            copy_rna_settings(src, dst)
            changed += 1

        if temp and src is not None:
            if obj_type == 'EMPTY':
                bpy.data.objects.remove(src)
            else:
                src.use_fake_user = False
                if src.users == 0:
                    getattr(bpy.data, coll_name).remove(src)
            self._cleanup_orphans(self._new_ids(before))

        self.report({'INFO'}, f"Pasted {label} settings onto {changed} object(s).")
        return {'FINISHED'}


class VIEW3D_MT_smart_tabs_copy_paste(bpy.types.Menu):
    bl_label = "Copy / Paste"
    bl_idname = "VIEW3D_MT_smart_tabs_copy_paste"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'EXEC_DEFAULT'
        obj = context.active_object
        prefs = get_addon_prefs(context)

        row = layout.row()
        row.enabled = bool(obj and obj.modifiers)
        row.operator("object.smart_tabs_copy_attributes", text="Copy Modifiers", icon='MODIFIER').kind = 'MODIFIERS'
        row = layout.row()
        row.enabled = bool(obj and obj.material_slots)
        row.operator("object.smart_tabs_copy_attributes", text="Copy Materials", icon='MATERIAL').kind = 'MATERIALS'
        if obj and obj.type in SETTINGS_TYPES:
            layout.operator("object.smart_tabs_copy_attributes",
                            text=f"Copy {settings_label(obj)} Settings", icon='PROPERTIES').kind = 'SETTINGS'

        meta = read_attr_clipboard_meta()
        kind = meta.get("kind")
        if kind:
            layout.separator()
            items = [i for i in meta.get("items", []) if i]
            if kind == 'SETTINGS':
                text = f"Paste {items[0] if items else ''} Settings"
            else:
                preview = ", ".join(items[:3]) + ("…" if len(items) > 3 else "")
                text = f"Paste {kind.title()}" + (f" ({preview})" if preview else "")
            layout.operator_context = 'INVOKE_DEFAULT'
            layout.operator("object.smart_tabs_paste_attributes", text=text, icon='PASTEDOWN')
            layout.operator_context = 'EXEC_DEFAULT'

        layout.separator()
        layout.operator("wm.copy_tab_data", text="Copy Objects", icon='COPYDOWN')
        op = layout.operator("wm.paste_tab_data", text="Paste Objects", icon='PASTEDOWN')
        if prefs:
            op.reuse_existing = prefs.reuse_existing_materials
            op.paste_at_cursor = prefs.paste_at_cursor
            op.apply_world = prefs.include_world
            op.link_from_source = prefs.link_from_source


def draw_object_context_copy_paste(self, context):
    layout = self.layout
    layout.separator()
    layout.menu(VIEW3D_MT_smart_tabs_copy_paste.bl_idname, icon='DUPLICATE')


# -------------------------------------------------------------------
# Operators: Project Tab Switching, New, Duplicate, Rename, Open, Close
# -------------------------------------------------------------------

class WM_OT_new_project_tab(bpy.types.Operator):
    """Create a new 'Untitled' project tab in the top bar"""
    bl_idname = "wm.new_project_tab"
    bl_label = "New Project Tab"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        prefs.tab_mode = 'FILES'
        save_current_viewport_to_tab(context)

        stash_active_tab(prefs)

        next_title = generate_next_untitled_name(prefs)
        new_item = prefs.open_files.add()
        new_item.uid = uuid.uuid4().hex[:8]
        new_item.display_name = next_title
        new_item.has_custom_label = False
        new_item.filepath = ""
        new_item.temp_filepath = ""
        new_item.is_virtual_dirty = False

        prefs.active_tab_uid = new_item.uid
        mark_prefs_dirty(context)

        load_ui = not prefs.keep_current_ui
        bpy.ops.wm.read_homefile(use_empty=False, load_ui=load_ui)
        return {'FINISHED'}


class WM_OT_duplicate_project_tab(bpy.types.Operator):
    """Clone a project or Scene tab into a new independent tab"""
    bl_idname = "wm.duplicate_project_tab"
    bl_label = "Duplicate Tab"

    index: IntProperty(
        name="Tab Index",
        description="Index of the tab to duplicate (-1 for currently active tab)",
        default=-1,
    )

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        if prefs.tab_mode == 'SCENES':
            if bpy.ops.scene.new.poll():
                bpy.ops.scene.new(type='FULL_COPY')
                self.report({'INFO'}, f"Duplicated scene to '{context.window.scene.name}'.")
                return {'FINISHED'}
            return {'CANCELLED'}

        active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        src_item = prefs.open_files[target_idx]
        is_duplicating_active = bool(active_item and src_item.uid == active_item.uid)

        save_current_viewport_to_tab(context)

        if is_duplicating_active:
            stash_active_tab(prefs)

        new_uid = uuid.uuid4().hex[:8]
        new_temp_path = os.path.join(get_unsaved_dir(), f"{new_uid}.blend")
        base_title = src_item.display_name or (os.path.basename(src_item.filepath) if src_item.filepath else "Untitled 1")
        dup_title = generate_duplicate_tab_name(prefs, base_title)

        paths_remapped = True
        try:
            if is_duplicating_active:
                internal_save_as_copy(new_temp_path)
            else:
                if has_buffered_changes(src_item):
                    src_disk_path = src_item.temp_filepath
                elif src_item.filepath and os.path.exists(src_item.filepath):
                    src_disk_path = src_item.filepath
                else:
                    src_disk_path = src_item.temp_filepath
                if not src_disk_path or not os.path.exists(src_disk_path):
                    self.report({'ERROR'}, f"Cannot duplicate '{base_title}': source file not found.")
                    return {'CANCELLED'}
                paths_remapped = copy_blend_with_remap(src_disk_path, new_temp_path)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to duplicate tab: {exc}")
            return {'CANCELLED'}

        new_item = prefs.open_files.add()
        new_item.uid = new_uid
        new_item.display_name = dup_title
        new_item.has_custom_label = False
        new_item.filepath = ""
        new_item.temp_filepath = new_temp_path
        new_item.is_virtual_dirty = True
        new_item.is_pinned = False
        copy_viewport_state(src_item, new_item)

        last_idx = len(prefs.open_files) - 1
        insert_idx = min(target_idx + 1, last_idx)
        if last_idx != insert_idx:
            prefs.open_files.move(last_idx, insert_idx)

        dup_item = prefs.open_files[insert_idx]
        mark_prefs_dirty(context)

        # Report before loading: the file load below replaces the current file's data.
        if paths_remapped:
            self.report({'INFO'}, f"Duplicated tab as '{dup_title}'.")
        else:
            self.report(
                {'WARNING'},
                f"Duplicated tab as '{dup_title}', but relative texture/library paths could not be "
                "remapped. Use File > External Data > Make Paths Absolute in the source first if they break.",
            )

        if is_duplicating_active:
            open_tab_item_in_window(prefs, dup_item)
        else:
            bpy.ops.wm.switch_blend_tab('INVOKE_DEFAULT', tab_uid=dup_item.uid)
        return {'FINISHED'}


class WM_OT_rename_project_tab(bpy.types.Operator):
    """Give a project or Scene tab a custom name"""
    bl_idname = "wm.rename_project_tab"
    bl_label = "Rename Tab"

    index: IntProperty(
        name="Tab Index",
        description="Index of the tab to rename (-1 for currently active tab)",
        default=-1,
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    new_name: StringProperty(
        name="Tab Label",
        description="Custom display name for this tab (leave blank to reset to filename or Untitled)",
        default="",
        options={'SKIP_SAVE'},
    )
    lock_custom_name: BoolProperty(
        name="Keep Custom Label After Saving",
        description="Preserve this custom tab label even when the .blend project is saved to disk",
        default=False,
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        if prefs.tab_mode == 'SCENES':
            if context.window and context.window.scene:
                self.new_name = context.window.scene.name
                return context.window_manager.invoke_props_dialog(self, width=320)
            return {'CANCELLED'}

        _active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        self.index = target_idx
        item = prefs.open_files[target_idx]
        self.new_name = item.display_name or (os.path.basename(item.filepath) if item.filepath else "Untitled 1")
        self.lock_custom_name = bool(item.has_custom_label or item.filepath)
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        prefs = get_addon_prefs(context)
        col = layout.column(align=True)

        if prefs and prefs.tab_mode == 'SCENES':
            col.label(text="Enter a new name for the active Scene:", icon='SCENE_DATA')
            col.prop(self, "new_name", text="")
            return

        item = prefs.open_files[self.index] if (prefs and 0 <= self.index < len(prefs.open_files)) else None
        col.label(text="Custom Tab Label:", icon='OUTLINER_DATA_FONT')
        col.prop(self, "new_name", text="")
        col.separator()
        col.prop(self, "lock_custom_name")

        if item and item.filepath:
            sub = col.row()
            sub.enabled = False
            sub.label(text=f"File: {os.path.basename(item.filepath)}", icon='FILE_BLEND')
        else:
            sub = col.row()
            sub.enabled = False
            sub.label(text="Leave empty to reset the name.", icon='INFO')

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        cleaned = self.new_name.strip()

        if prefs.tab_mode == 'SCENES':
            if context.window and context.window.scene and cleaned:
                old_name = context.window.scene.name
                context.window.scene.name = cleaned
                tag_redraw_all_tab_bars(context)
                self.report({'INFO'}, f"Renamed scene '{old_name}' to '{context.window.scene.name}'.")
                return {'FINISHED'}
            return {'CANCELLED'}

        _active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        item = prefs.open_files[target_idx]

        if cleaned:
            item.display_name = cleaned
            item.has_custom_label = bool(self.lock_custom_name or item.filepath)
            self.report({'INFO'}, f"Renamed tab to '{cleaned}'.")
        else:
            item.has_custom_label = False
            if item.filepath:
                item.display_name = os.path.basename(item.filepath)
            else:
                item.display_name = generate_next_untitled_name(prefs, exclude_item=item)
            self.report({'INFO'}, f"Reset tab label to '{item.display_name}'.")

        mark_prefs_dirty(context)
        tag_redraw_all_tab_bars(context)
        return {'FINISHED'}


class WM_OT_switch_blend_tab(bpy.types.Operator):
    """Switch to this tab (right-click for more actions)"""
    bl_idname = "wm.switch_blend_tab"
    bl_label = "Switch Project Tab"

    tab_uid: StringProperty(default="")
    filepath: StringProperty(subtype='FILE_PATH', default="")

    def _resolve_target(self, prefs):
        if self.tab_uid:
            for item in prefs.open_files:
                if item.uid == self.tab_uid:
                    return item
        if self.filepath:
            norm = os.path.normpath(self.filepath)
            for item in prefs.open_files:
                if item.filepath and os.path.normpath(item.filepath) == norm:
                    return item
        return None

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        target_item = self._resolve_target(prefs)
        if not target_item:
            self.report({'ERROR'}, "Target tab no longer exists.")
            return {'CANCELLED'}

        if target_item.filepath and not os.path.exists(target_item.filepath) and not has_buffered_changes(target_item):
            self.report({'ERROR'}, f"File not found on disk: {target_item.filepath}")
            return {'CANCELLED'}

        return self.execute(context)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        target_item = self._resolve_target(prefs)
        if not target_item:
            return {'CANCELLED'}

        if target_item.filepath and not os.path.exists(target_item.filepath) and not has_buffered_changes(target_item):
            self.report({'ERROR'}, f"File not found: {target_item.filepath}")
            return {'CANCELLED'}

        active_item, _ = get_active_tab_item(prefs)
        if active_item and active_item.uid == target_item.uid:
            return {'FINISHED'}

        save_current_viewport_to_tab(context)
        try:
            stash_active_tab(prefs)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not keep the current tab's changes, switch cancelled: {exc}")
            return {'CANCELLED'}

        mark_prefs_dirty(context)
        open_tab_item_in_window(prefs, target_item)
        return {'FINISHED'}


class WM_OT_close_blend_tab(bpy.types.Operator):
    """Close an individual project tab (prompts to save if the tab has unsaved changes)"""
    bl_idname = "wm.close_blend_tab"
    bl_label = "Close Project Tab"

    index: IntProperty(default=-1)

    close_action: EnumProperty(
        name="Action",
        items=[
            ('SAVE_CLOSE', "Save & Close", "Save project changes before closing this tab", 'FILE_TICK', 0),
            ('DISCARD_CLOSE', "Don't Save", "Close tab and discard unsaved changes", 'TRASH', 1),
        ],
        default='SAVE_CLOSE',
        options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not prefs or not (0 <= self.index < len(prefs.open_files)):
            return {'CANCELLED'}

        target_item = prefs.open_files[self.index]
        active_item, _ = get_active_tab_item(prefs)
        is_closing_active = bool(active_item and active_item.uid == target_item.uid)

        if is_tab_item_dirty(target_item, is_closing_active):
            self.close_action = 'SAVE_CLOSE' if is_closing_active else 'DISCARD_CLOSE'
            return context.window_manager.invoke_props_dialog(self, width=360)

        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        prefs = get_addon_prefs(context)
        target_item = prefs.open_files[self.index] if (prefs and 0 <= self.index < len(prefs.open_files)) else None
        name = target_item.display_name if target_item else "Project"
        col = layout.column(align=True)
        col.label(text=f"'{name}' has unsaved changes.", icon='ERROR')
        col.separator()
        col.prop(self, "close_action", expand=True)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs or not (0 <= self.index < len(prefs.open_files)):
            return {'CANCELLED'}

        target_item = prefs.open_files[self.index]
        target_uid = target_item.uid
        active_item, _ = get_active_tab_item(prefs)
        is_closing_active = bool(active_item and active_item.uid == target_uid)

        if is_tab_item_dirty(target_item, is_closing_active) and self.close_action == 'SAVE_CLOSE':
            if not is_closing_active and has_buffered_changes(target_item):
                # A saved project with changes kept in the background: write them to its
                # real file without loading it.
                write_tab_buffer_to_file(target_item)
            elif not is_closing_active:
                # The background tab has to be loaded before it can be saved. Protect the
                # current tab's work first, exactly as a normal tab switch would.
                save_current_viewport_to_tab(context)
                try:
                    stash_active_tab(prefs)
                except Exception as exc:
                    self.report({'ERROR'}, f"Could not keep the current tab's changes: {exc}")
                    return {'CANCELLED'}
                mark_prefs_dirty(context)
                open_tab_item_in_window(prefs, target_item)
                # Open the Save As browser only after the file load has fully finished.
                call_deferred(lambda: bpy.ops.wm.save_as_and_close_tab('INVOKE_DEFAULT', tab_uid=target_uid))
                return {'FINISHED'}

            elif target_item.filepath:
                save_active_tab_to_disk(prefs)
                target_item = prefs.open_files[find_tab_index_by_uid(prefs, target_uid)]
            else:
                # Untitled/duplicated tab: the close happens only if the Save As completes.
                bpy.ops.wm.save_as_and_close_tab('INVOKE_DEFAULT', tab_uid=target_uid)
                return {'FINISHED'}

        remember_closed_tab(target_item)
        remove_tab_temp_buffer(target_item)
        prefs.open_files.remove(self.index)
        mark_prefs_dirty(context)

        if is_closing_active:
            if len(prefs.open_files) > 0:
                next_idx = min(self.index, len(prefs.open_files) - 1)
                next_item = prefs.open_files[next_idx]
                open_tab_item_in_window(prefs, next_item)
            else:
                next_title = generate_next_untitled_name(prefs)
                new_item = prefs.open_files.add()
                new_item.uid = uuid.uuid4().hex[:8]
                new_item.display_name = next_title
                new_item.has_custom_label = False
                prefs.active_tab_uid = new_item.uid
                bpy.ops.wm.read_homefile(use_empty=False, load_ui=not prefs.keep_current_ui)

        return {'FINISHED'}


class WM_OT_save_as_and_close_tab(bpy.types.Operator):
    """Save the active unsaved tab to a new .blend file, then close it (cancelling keeps the tab open)"""
    bl_idname = "wm.save_as_and_close_tab"
    bl_label = "Save & Close Tab"

    tab_uid: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})
    check_existing: BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        idx = find_tab_index_by_uid(prefs, self.tab_uid)
        name = prefs.open_files[idx].display_name if idx != -1 else ""
        for ch in '/\\:*?"<>|':
            name = name.replace(ch, "_")
        name = bpy.path.ensure_ext(name.strip() or "untitled", ".blend")

        cur = bpy.data.filepath
        if cur and not is_temp_buffer_path(cur):
            base_dir = os.path.dirname(cur)
        else:
            base_dir = os.path.expanduser("~")
        self.filepath = os.path.join(base_dir, name)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}
        active_item, _ = get_active_tab_item(prefs)
        if not active_item or active_item.uid != self.tab_uid:
            self.report({'ERROR'}, "The tab to save is no longer the active tab; nothing was closed.")
            return {'CANCELLED'}
        if not self.filepath:
            return {'CANCELLED'}

        filepath = bpy.path.ensure_ext(self.filepath, ".blend")
        try:
            bpy.ops.wm.save_as_mainfile(filepath=filepath)
        except RuntimeError as exc:
            self.report({'ERROR'}, f"Save failed, tab kept open: {exc}")
            return {'CANCELLED'}

        # on_file_saved has re-pointed the tab at the new file (possibly merging it into an
        # existing tab for that path), so close whatever tab is active now.
        close_tab_by_uid_deferred(prefs.active_tab_uid)
        return {'FINISHED'}


class WM_OT_discard_tab_changes(bpy.types.Operator):
    """Throw away this tab's unsaved changes and go back to the saved .blend file"""
    bl_idname = "wm.discard_tab_changes"
    bl_label = "Discard Unsaved Changes"

    index: IntProperty(default=-1)

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs or not (0 <= self.index < len(prefs.open_files)):
            return {'CANCELLED'}
        item = prefs.open_files[self.index]
        if not item.filepath or not os.path.exists(item.filepath):
            self.report({'ERROR'}, "This tab has no saved file to go back to.")
            return {'CANCELLED'}
        active_item, _ = get_active_tab_item(prefs)
        is_active = bool(active_item and active_item.uid == item.uid)
        remove_tab_temp_buffer(item)
        item.temp_filepath = ""
        item.is_virtual_dirty = False
        mark_prefs_dirty(context)
        if is_active:
            bpy.ops.wm.open_mainfile(filepath=item.filepath, load_ui=not prefs.keep_current_ui)
        self.report({'INFO'}, f"Discarded unsaved changes in '{item.display_name}'.")
        return {'FINISHED'}


class WM_OT_save_all_tabs(bpy.types.Operator):
    """Save every tab with unsaved changes to its .blend file. Untitled tabs need a first Save As and are skipped"""
    bl_idname = "wm.save_all_tabs"
    bl_label = "Save All Tabs"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}
        active_item, _ = get_active_tab_item(prefs)
        active_uid = active_item.uid if active_item else ""

        saved, untitled, not_remapped, failed = 0, [], 0, []
        for item in list(prefs.open_files):
            is_active = item.uid == active_uid
            if not is_tab_item_dirty(item, is_active):
                continue
            if not item.filepath:
                untitled.append(item.display_name)
                continue
            if is_active or not has_buffered_changes(item):
                continue
            try:
                if not write_tab_buffer_to_file(item):
                    not_remapped += 1
                saved += 1
            except Exception as exc:
                failed.append(f"{item.display_name}: {exc}")

        if active_item and active_item.filepath and is_tab_item_dirty(active_item, True):
            try:
                save_active_tab_to_disk(prefs)
                saved += 1
            except Exception as exc:
                failed.append(f"{active_item.display_name}: {exc}")

        mark_prefs_dirty(context)
        tag_redraw_all_tab_bars(context)

        if failed:
            self.report({'ERROR'}, f"Saved {saved} tab(s). Could not save: " + "; ".join(failed))
            return {'FINISHED'}
        msg = f"Saved {saved} tab(s)." if saved else "Nothing to save."
        if untitled:
            msg += f" {len(untitled)} Untitled tab(s) need Save As first: " + ", ".join(untitled[:3])
        if not_remapped:
            msg += f" {not_remapped} file(s) were copied without fixing relative texture paths."
        self.report({'WARNING'} if (untitled or not_remapped) else {'INFO'}, msg)
        return {'FINISHED'}


class WM_OT_reopen_closed_tab(bpy.types.Operator):
    """Open the most recently closed project tab again"""
    bl_idname = "wm.reopen_closed_tab"
    bl_label = "Reopen Closed Tab"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}
        open_paths = {os.path.normcase(os.path.normpath(i.filepath)) for i in prefs.open_files if i.filepath}
        entries = load_closed_tabs()
        entry = None
        while entries:
            candidate = entries.pop()
            fp = candidate.get("filepath", "")
            if fp and os.path.exists(fp) and os.path.normcase(os.path.normpath(fp)) not in open_paths:
                entry = candidate
                break
        save_closed_tabs(entries)
        if not entry:
            self.report({'INFO'}, "No closed tabs to reopen.")
            return {'CANCELLED'}

        prefs.tab_mode = 'FILES'
        item = prefs.open_files.add()
        item.uid = uuid.uuid4().hex[:8]
        item.filepath = entry["filepath"]
        item.has_custom_label = bool(entry.get("has_custom_label"))
        item.display_name = (entry.get("display_name") if item.has_custom_label else "") or os.path.basename(entry["filepath"])
        item.is_pinned = bool(entry.get("is_pinned"))
        mark_prefs_dirty(context)
        bpy.ops.wm.switch_blend_tab('INVOKE_DEFAULT', tab_uid=item.uid)
        return {'FINISHED'}


class WM_OT_save_tab_aware(bpy.types.Operator):
    """Save active .blend project (opens Save As dialog automatically for Untitled or Duplicated tabs)"""
    bl_idname = "wm.save_tab_aware"
    bl_label = "Save Project Tab"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        active_item, _ = get_active_tab_item(prefs) if prefs else (None, -1)

        if active_item and active_item.filepath and is_loaded_from_buffer(active_item):
            save_active_tab_to_disk(prefs)
            return {'FINISHED'}
        if (active_item and not active_item.filepath) or not bpy.data.is_saved or is_temp_buffer_path(bpy.data.filepath):
            return bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')

        return bpy.ops.wm.save_mainfile()


# -------------------------------------------------------------------
# Operators: Tab Context Menu, Support Utilities & Batch Actions
# -------------------------------------------------------------------

class WM_OT_copy_support_email(bpy.types.Operator):
    """Copy the Graph Team support email address to the system clipboard"""
    bl_idname = "wm.copy_support_email"
    bl_label = "Copy Support Email"

    def execute(self, context):
        context.window_manager.clipboard = ADDON_CONTACT
        self.report({'INFO'}, f"Copied '{ADDON_CONTACT}' to clipboard.")
        return {'FINISHED'}


class WM_OT_open_tabs_config_folder(bpy.types.Operator):
    """Open the folder where this extension stores sessions, the tab clipboard and unsaved tabs"""
    bl_idname = "wm.open_tabs_config_folder"
    bl_label = "Open Data Folder"

    def execute(self, context):
        cfg_dir = get_config_dir()
        bpy.ops.wm.path_open(filepath=cfg_dir)
        return {'FINISHED'}


class _BulkCloseTabsMixin:
    """Shared logic for closing several tabs at once without silently losing unsaved work.

    Subclasses implement _collect_targets(prefs) -> (target_uids, fallback_uid). If any target
    has unsaved changes, invoke() asks first; the default keeps those tabs open.
    """

    unsaved_action: EnumProperty(
        name="Unsaved Tabs",
        description="What to do with tabs that have unsaved changes",
        items=[
            ('KEEP', "Keep Unsaved Tabs Open", "Close only tabs without unsaved changes", 'LOCKED', 0),
            ('DISCARD', "Discard Unsaved Changes", "Close every tab and permanently lose unsaved changes", 'TRASH', 1),
        ],
        default='KEEP',
        options={'SKIP_SAVE'},
    )

    def _collect_targets(self, prefs):
        raise NotImplementedError

    def _dirty_targets(self, prefs, target_uids):
        active_item, _ = get_active_tab_item(prefs)
        active_uid = active_item.uid if active_item else ""
        return [
            item for item in prefs.open_files
            if item.uid in target_uids and is_tab_item_dirty(item, item.uid == active_uid)
        ]

    def invoke(self, context, event):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}
        target_uids, _fallback = self._collect_targets(prefs)
        if self._dirty_targets(prefs, target_uids):
            self.unsaved_action = 'KEEP'
            return context.window_manager.invoke_props_dialog(self, width=380)
        return self.execute(context)

    def draw(self, context):
        prefs = get_addon_prefs(context)
        layout = self.layout
        col = layout.column(align=True)
        if prefs:
            target_uids, _fallback = self._collect_targets(prefs)
            dirty = self._dirty_targets(prefs, target_uids)
            col.label(text=f"{len(dirty)} tab(s) have unsaved changes:", icon='ERROR')
            for item in dirty[:8]:
                col.label(text=f"    {item.display_name}")
            if len(dirty) > 8:
                col.label(text=f"    ...and {len(dirty) - 8} more")
        col.separator()
        col.prop(self, "unsaved_action", expand=True)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        target_uids, fallback_uid = self._collect_targets(prefs)
        if target_uids is None:
            return {'CANCELLED'}

        kept_dirty = 0
        if self.unsaved_action != 'DISCARD':
            dirty_uids = {item.uid for item in self._dirty_targets(prefs, target_uids)}
            kept_dirty = len(dirty_uids)
            target_uids = target_uids - dirty_uids

        active_item, _ = get_active_tab_item(prefs)
        active_uid = active_item.uid if active_item else ""

        removed = 0
        for idx in range(len(prefs.open_files) - 1, -1, -1):
            item = prefs.open_files[idx]
            if item.uid in target_uids:
                remember_closed_tab(item)
                remove_tab_temp_buffer(item)
                prefs.open_files.remove(idx)
                removed += 1

        if removed:
            mark_prefs_dirty(context)

        msg = f"Closed {removed} tab(s)."
        if kept_dirty:
            msg += f" Kept {kept_dirty} tab(s) with unsaved changes."
        self.report({'INFO'}, msg)

        if active_uid and active_uid in target_uids:
            fb_idx = find_tab_index_by_uid(prefs, fallback_uid)
            if fb_idx != -1:
                open_tab_item_in_window(prefs, prefs.open_files[fb_idx])
        return {'FINISHED'}


class WM_OT_close_other_tabs(_BulkCloseTabsMixin, bpy.types.Operator):
    """Close all unpinned project tabs except the specified tab"""
    bl_idname = "wm.close_other_tabs"
    bl_label = "Close Other Tabs"

    index: IntProperty(default=-1)

    def _collect_targets(self, prefs):
        _active_item, active_idx = get_active_tab_item(prefs)
        keep_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= keep_idx < len(prefs.open_files)):
            return None, ""
        keep_uid = prefs.open_files[keep_idx].uid
        targets = {item.uid for item in prefs.open_files if item.uid != keep_uid and not item.is_pinned}
        return targets, keep_uid


class WM_OT_close_tabs_to_right(_BulkCloseTabsMixin, bpy.types.Operator):
    """Close all unpinned project tabs to the right of the specified tab"""
    bl_idname = "wm.close_tabs_to_right"
    bl_label = "Close Tabs to the Right"

    index: IntProperty(default=-1)

    def _collect_targets(self, prefs):
        _active_item, active_idx = get_active_tab_item(prefs)
        pivot_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= pivot_idx < len(prefs.open_files)):
            return None, ""
        pivot_uid = prefs.open_files[pivot_idx].uid
        targets = {
            item.uid for idx, item in enumerate(prefs.open_files)
            if idx > pivot_idx and not item.is_pinned
        }
        return targets, pivot_uid


class WM_OT_copy_tab_filepath(bpy.types.Operator):
    """Copy the full .blend file path of this tab to the system clipboard"""
    bl_idname = "wm.copy_tab_filepath"
    bl_label = "Copy File Path"

    index: IntProperty(default=-1)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        _active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        item = prefs.open_files[target_idx]
        if not item.filepath:
            self.report({'WARNING'}, f"'{item.display_name}' has not been saved to disk yet.")
            return {'CANCELLED'}

        norm_path = os.path.normpath(item.filepath)
        context.window_manager.clipboard = norm_path
        self.report({'INFO'}, f"Copied path: {norm_path}")
        return {'FINISHED'}


class WM_OT_open_tab_folder(bpy.types.Operator):
    """Open the folder containing this tab's .blend file in the system file browser"""
    bl_idname = "wm.open_tab_folder"
    bl_label = "Open Containing Folder"

    index: IntProperty(default=-1)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        _active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        item = prefs.open_files[target_idx]
        if not item.filepath or not os.path.exists(item.filepath):
            self.report({'WARNING'}, f"'{item.display_name}' is not saved on disk yet.")
            return {'CANCELLED'}

        folder_path = os.path.dirname(os.path.normpath(item.filepath))
        bpy.ops.wm.path_open(filepath=folder_path)
        return {'FINISHED'}


def draw_tab_context_menu_items(layout, prefs, tab_index):
    """Populate a menu layout with context actions for a specific tab index."""
    if not prefs or not (0 <= tab_index < len(prefs.open_files)):
        return

    layout.operator_context = 'INVOKE_DEFAULT'

    item = prefs.open_files[tab_index]
    total = len(prefs.open_files)

    ren_op = layout.operator("wm.rename_project_tab", text="Rename Tab...", icon='OUTLINER_DATA_FONT')
    ren_op.index = tab_index

    dup_op = layout.operator("wm.duplicate_project_tab", text="Duplicate Tab", icon='DUPLICATE')
    dup_op.index = tab_index

    pin_label = "Unpin Tab" if item.is_pinned else "Pin Tab"
    pin_icon = 'UNPINNED' if item.is_pinned else 'PINNED'
    pin_op = layout.operator("wm.toggle_pin_blend_tab", text=pin_label, icon=pin_icon)
    pin_op.index = tab_index

    layout.separator()

    row_left = layout.row()
    row_left.enabled = (tab_index > 0)
    left_op = row_left.operator("wm.move_blend_tab", text="Move Tab Left", icon='TRIA_LEFT')
    left_op.index = tab_index
    left_op.direction = -1

    row_right = layout.row()
    row_right.enabled = (tab_index < total - 1)
    right_op = row_right.operator("wm.move_blend_tab", text="Move Tab Right", icon='TRIA_RIGHT')
    right_op.index = tab_index
    right_op.direction = 1

    layout.separator()

    row_path = layout.row()
    row_path.enabled = bool(item.filepath)
    copy_path_op = row_path.operator("wm.copy_tab_filepath", text="Copy File Path", icon='COPYDOWN')
    copy_path_op.index = tab_index

    row_folder = layout.row()
    row_folder.enabled = bool(item.filepath and os.path.exists(item.filepath))
    folder_op = row_folder.operator("wm.open_tab_folder", text="Open Containing Folder", icon='FILE_FOLDER')
    folder_op.index = tab_index

    active_item, _ = get_active_tab_item(prefs)
    is_active = bool(active_item and active_item.uid == item.uid)
    row_discard = layout.row()
    row_discard.enabled = bool(item.filepath and is_tab_item_dirty(item, is_active))
    discard_op = row_discard.operator("wm.discard_tab_changes", text="Discard Unsaved Changes", icon='LOOP_BACK')
    discard_op.index = tab_index

    layout.separator()

    other_op = layout.operator("wm.close_other_tabs", text="Close Other Tabs", icon='PANEL_CLOSE')
    other_op.index = tab_index

    right_close_row = layout.row()
    right_close_row.enabled = (tab_index < total - 1)
    close_right_op = right_close_row.operator("wm.close_tabs_to_right", text="Close Tabs to the Right", icon='TRIA_RIGHT_BAR')
    close_right_op.index = tab_index

    layout.operator("wm.close_unpinned_tabs", text="Close Unpinned Inactive Tabs", icon='BRUSH_DATA')

    layout.separator()
    close_op = layout.operator("wm.close_blend_tab", text="Close Tab", icon='X')
    close_op.index = tab_index


class WM_OT_tab_context_menu(bpy.types.Operator):
    """Open the Project Tab Context Menu for this tab"""
    bl_idname = "wm.tab_context_menu"
    bl_label = "Tab Context Menu"

    index: IntProperty(default=-1)

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        _active_item, active_idx = get_active_tab_item(prefs)
        target_idx = self.index if (0 <= self.index < len(prefs.open_files)) else active_idx
        if not (0 <= target_idx < len(prefs.open_files)):
            return {'CANCELLED'}

        tab_title = prefs.open_files[target_idx].display_name or "Project Tab"

        def draw_popup(menu_self, _menu_context):
            draw_tab_context_menu_items(menu_self.layout, prefs, target_idx)

        context.window_manager.popup_menu(draw_popup, title=f"Tab: {tab_title}", icon='FILE_BLEND')
        return {'FINISHED'}


def draw_button_context_menu_tab_actions(self, context):
    """Inject Project Tab Context Menu options when right-clicking any project tab button."""
    button_op = getattr(context, "button_operator", None)
    if not button_op:
        return

    prefs = get_addon_prefs(context)
    if not prefs or prefs.tab_mode != 'FILES':
        return

    tab_idx = -1
    if isinstance(button_op, WM_OT_switch_blend_tab):
        tab_idx = find_tab_index_by_uid(prefs, getattr(button_op, "tab_uid", ""))
    elif isinstance(
        button_op,
        (
            WM_OT_close_blend_tab,
            WM_OT_toggle_pin_blend_tab,
            WM_OT_tab_context_menu,
            WM_OT_rename_project_tab,
            WM_OT_duplicate_project_tab,
        ),
    ):
        idx_val = getattr(button_op, "index", -1)
        if 0 <= idx_val < len(prefs.open_files):
            tab_idx = idx_val

    if tab_idx == -1:
        return

    layout = self.layout
    layout.separator()
    item = prefs.open_files[tab_idx]
    layout.label(text=f"Tab: {item.display_name}", icon='FILE_BLEND')
    draw_tab_context_menu_items(layout, prefs, tab_idx)
    layout.separator()


# -------------------------------------------------------------------
# Operators: Search & Tab Cycling
# -------------------------------------------------------------------

def get_searchable_tabs_items(self, context):
    """Build dynamic Enum items for the Quick Search popup with global caching."""
    global _SEARCH_ENUM_CACHE
    _SEARCH_ENUM_CACHE.clear()

    prefs = get_addon_prefs(context)
    if not prefs:
        _SEARCH_ENUM_CACHE.append(('NONE', "No Open Tabs", "", 'INFO', 0))
        return _SEARCH_ENUM_CACHE

    if prefs.tab_mode == 'FILES':
        active_item, _ = get_active_tab_item(prefs)
        for idx, item in enumerate(prefs.open_files):
            is_act = bool(active_item and active_item.uid == item.uid)
            dirty_star = "*" if is_tab_item_dirty(item, is_act) else ""
            pin_prefix = "[Pinned] " if item.is_pinned else ""
            desc = item.filepath if item.filepath else "Unsaved Project Tab"
            _SEARCH_ENUM_CACHE.append(
                (item.uid, f"{pin_prefix}{item.display_name}{dirty_star}", desc, 'FILE_BLEND', idx)
            )
    else:
        for idx, scene in enumerate(bpy.data.scenes):
            _SEARCH_ENUM_CACHE.append(
                (scene.name, f"Scene: {scene.name}", f"Switch to {scene.name}", 'SCENE_DATA', idx)
            )

    if not _SEARCH_ENUM_CACHE:
        _SEARCH_ENUM_CACHE.append(('NONE', "No Open Tabs", "", 'INFO', 0))
    return _SEARCH_ENUM_CACHE


class WM_OT_search_tabs_popup(bpy.types.Operator):
    """Search and switch to any open tab"""
    bl_idname = "wm.search_tabs_popup"
    bl_label = "Switch Project Tab..."
    bl_property = "target_tab"

    target_tab: EnumProperty(
        name="Open Projects",
        items=get_searchable_tabs_items,
    )

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.target_tab or self.target_tab == 'NONE':
            return {'CANCELLED'}

        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        if prefs.tab_mode == 'FILES':
            bpy.ops.wm.switch_blend_tab('INVOKE_DEFAULT', tab_uid=self.target_tab)
        else:
            bpy.ops.wm.switch_scene_tab(scene_name=self.target_tab)
        return {'FINISHED'}


class WM_OT_cycle_tabs(bpy.types.Operator):
    """Cycle forward or backward through open project or Scene tabs"""
    bl_idname = "wm.cycle_tabs"
    bl_label = "Cycle Project/Scene Tabs"

    delta: IntProperty(
        name="Direction",
        description="1 for Next Tab, -1 for Previous Tab",
        default=1,
    )

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        if prefs.tab_mode == 'FILES':
            total = len(prefs.open_files)
            if total <= 1:
                return {'FINISHED'}

            _active_item, current_idx = get_active_tab_item(prefs)
            next_idx = (current_idx + self.delta) % total if current_idx != -1 else 0
            target_item = prefs.open_files[next_idx]

            bpy.ops.wm.switch_blend_tab('INVOKE_DEFAULT', tab_uid=target_item.uid)
            return {'FINISHED'}

        elif prefs.tab_mode == 'SCENES':
            scenes = list(bpy.data.scenes)
            if len(scenes) <= 1:
                return {'FINISHED'}

            active_scene = context.window.scene
            current_idx = scenes.index(active_scene) if active_scene in scenes else 0
            next_idx = (current_idx + self.delta) % len(scenes)
            context.window.scene = scenes[next_idx]
            return {'FINISHED'}

        return {'CANCELLED'}


# -------------------------------------------------------------------
# Operators: Tab Management (Open, Pin, Reorder, Clean, Sessions)
# -------------------------------------------------------------------

class WM_OT_add_blend_tab(bpy.types.Operator):
    """Open an existing .blend file in a new project tab"""
    bl_idname = "wm.add_blend_tab"
    bl_label = "Open Project in New Tab..."

    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})
    switch_immediately: BoolProperty(
        name="Switch to File Immediately",
        default=True,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs or not self.filepath:
            return {'CANCELLED'}

        prefs.tab_mode = 'FILES'
        norm_target = os.path.normpath(self.filepath)
        target_item = None

        for item in prefs.open_files:
            if item.filepath and os.path.normpath(item.filepath) == norm_target:
                target_item = item
                break

        if not target_item:
            target_item = prefs.open_files.add()
            target_item.uid = uuid.uuid4().hex[:8]
            target_item.filepath = self.filepath
            target_item.display_name = os.path.basename(self.filepath)
            target_item.has_custom_label = False
            mark_prefs_dirty(context)

        if self.switch_immediately:
            bpy.ops.wm.switch_blend_tab('INVOKE_DEFAULT', tab_uid=target_item.uid)

        return {'FINISHED'}


class WM_OT_toggle_pin_blend_tab(bpy.types.Operator):
    """Toggle pin lock on a project tab to protect it from bulk closing"""
    bl_idname = "wm.toggle_pin_blend_tab"
    bl_label = "Pin / Unpin Project Tab"

    index: IntProperty()

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if prefs and 0 <= self.index < len(prefs.open_files):
            item = prefs.open_files[self.index]
            item.is_pinned = not item.is_pinned
            mark_prefs_dirty(context)
        return {'FINISHED'}


class WM_OT_move_blend_tab(bpy.types.Operator):
    """Reorder a project tab left/up or right/down in the tab bar"""
    bl_idname = "wm.move_blend_tab"
    bl_label = "Move Project Tab"

    index: IntProperty()
    direction: IntProperty(default=-1)

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}
        new_index = self.index + self.direction
        if 0 <= self.index < len(prefs.open_files) and 0 <= new_index < len(prefs.open_files):
            prefs.open_files.move(self.index, new_index)
            mark_prefs_dirty(context)
        return {'FINISHED'}


class WM_OT_close_unpinned_tabs(_BulkCloseTabsMixin, bpy.types.Operator):
    """Close all unpinned inactive tabs"""
    bl_idname = "wm.close_unpinned_tabs"
    bl_label = "Close Other Unpinned Tabs"

    def _collect_targets(self, prefs):
        active_item, _ = get_active_tab_item(prefs)
        active_uid = active_item.uid if active_item else ""
        targets = {item.uid for item in prefs.open_files if not item.is_pinned and item.uid != active_uid}
        return targets, active_uid


class WM_OT_clean_missing_tabs(bpy.types.Operator):
    """Remove any tabs whose saved .blend file no longer exists on disk"""
    bl_idname = "wm.clean_missing_tabs"
    bl_label = "Clean Missing File Tabs"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        removed = 0
        for idx in range(len(prefs.open_files) - 1, -1, -1):
            item = prefs.open_files[idx]
            if item.filepath and not os.path.exists(item.filepath):
                prefs.open_files.remove(idx)
                removed += 1

        if removed:
            mark_prefs_dirty(context)
        self.report({'INFO'}, f"Removed {removed} missing file tab(s).")
        return {'FINISHED'}


class WM_OT_save_tab_session(bpy.types.Operator):
    """Save the current list of open .blend tabs as a named session preset"""
    bl_idname = "wm.save_tab_session"
    bl_label = "Save Tab Session"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs:
            return {'CANCELLED'}

        session_name = prefs.session_name_input.strip()
        if not session_name:
            self.report({'WARNING'}, "Enter a session name first.")
            return {'CANCELLED'}

        saved_items = [
            {
                "filepath": item.filepath,
                "display_name": item.display_name,
                "has_custom_label": item.has_custom_label,
                "is_pinned": item.is_pinned,
            }
            for item in prefs.open_files
            if item.filepath
        ]
        if not saved_items:
            self.report({'WARNING'}, "Save your projects to disk before saving a session.")
            return {'CANCELLED'}

        sessions_file = get_sessions_filepath()
        data = {}
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data[session_name] = saved_items

        try:
            with open(sessions_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to save session: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Saved session '{session_name}' ({len(saved_items)} projects).")
        return {'FINISHED'}


class WM_OT_load_tab_session(bpy.types.Operator):
    """Load a saved group of .blend project tabs"""
    bl_idname = "wm.load_tab_session"
    bl_label = "Load Tab Session"

    replace_current: BoolProperty(
        name="Replace Current Tabs",
        default=True,
    )

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs or prefs.selected_session == 'NONE':
            self.report({'WARNING'}, "No saved session selected.")
            return {'CANCELLED'}

        sessions_file = get_sessions_filepath()
        if not os.path.exists(sessions_file):
            return {'CANCELLED'}

        try:
            with open(sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self.report({'ERROR'}, f"Could not read sessions file: {exc}")
            return {'CANCELLED'}

        entries = data.get(prefs.selected_session, [])
        if self.replace_current:
            # Replace saved project tabs only. Keep the active tab (it is still open in the
            # window) and any Untitled/duplicated tab, whose work exists only in its buffer.
            active_item, _ = get_active_tab_item(prefs)
            active_uid = active_item.uid if active_item else ""
            for idx in range(len(prefs.open_files) - 1, -1, -1):
                item = prefs.open_files[idx]
                if item.filepath and item.uid != active_uid and not has_buffered_changes(item):
                    prefs.open_files.remove(idx)

        existing_paths = {os.path.normpath(i.filepath) for i in prefs.open_files if i.filepath}
        added = 0
        for entry in entries:
            fp = entry.get("filepath", "")
            if fp and os.path.normpath(fp) not in existing_paths:
                item = prefs.open_files.add()
                item.uid = uuid.uuid4().hex[:8]
                item.filepath = fp
                custom_lbl = entry.get("display_name", "")
                has_custom = bool(entry.get("has_custom_label", False))
                item.has_custom_label = has_custom
                item.display_name = custom_lbl if (has_custom and custom_lbl) else os.path.basename(fp)
                item.is_pinned = bool(entry.get("is_pinned", False))
                existing_paths.add(os.path.normpath(fp))
                added += 1

        mark_prefs_dirty(context)
        self.report({'INFO'}, f"Loaded {added} tab(s) from session '{prefs.selected_session}'.")
        return {'FINISHED'}


class WM_OT_delete_tab_session(bpy.types.Operator):
    """Delete the selected saved tab session preset"""
    bl_idname = "wm.delete_tab_session"
    bl_label = "Delete Saved Session"

    def execute(self, context):
        prefs = get_addon_prefs(context)
        if not prefs or prefs.selected_session == 'NONE':
            return {'CANCELLED'}

        sessions_file = get_sessions_filepath()
        if not os.path.exists(sessions_file):
            return {'CANCELLED'}

        try:
            with open(sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            removed_name = prefs.selected_session
            if removed_name in data:
                del data[removed_name]
                with open(sessions_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                self.report({'INFO'}, f"Deleted session '{removed_name}'.")
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to delete session: {exc}")
            return {'CANCELLED'}

        return {'FINISHED'}


class WM_OT_switch_scene_tab(bpy.types.Operator):
    """Switch active Scene in the current window"""
    bl_idname = "wm.switch_scene_tab"
    bl_label = "Switch Scene Tab"

    scene_name: StringProperty()

    def execute(self, context):
        target_scene = bpy.data.scenes.get(self.scene_name)
        if target_scene:
            context.window.scene = target_scene
            return {'FINISHED'}
        return {'CANCELLED'}


# -------------------------------------------------------------------
# Automatic Tab Synchronization Handlers
# -------------------------------------------------------------------

def ensure_initial_tab_exists():
    """Ensure at least one active tab exists on Blender startup."""
    prefs = get_addon_prefs(bpy.context)
    if not prefs:
        return None

    current_fp = bpy.data.filepath
    if current_fp and not is_temp_buffer_path(current_fp):
        norm_cur = os.path.normpath(current_fp)
        for item in prefs.open_files:
            if item.filepath and os.path.normpath(item.filepath) == norm_cur:
                prefs.active_tab_uid = item.uid
                return None

        new_item = prefs.open_files.add()
        new_item.uid = uuid.uuid4().hex[:8]
        new_item.filepath = current_fp
        new_item.display_name = os.path.basename(current_fp)
        new_item.has_custom_label = False
        prefs.active_tab_uid = new_item.uid
        mark_prefs_dirty(bpy.context)
    elif current_fp:
        # The window already holds a cached Untitled/duplicate buffer (e.g. add-on re-enabled).
        norm_cur = os.path.normcase(os.path.normpath(current_fp))
        for item in prefs.open_files:
            if item.temp_filepath and os.path.normcase(os.path.normpath(item.temp_filepath)) == norm_cur:
                prefs.active_tab_uid = item.uid
                return None
    else:
        # Keep unsaved/duplicated tabs whose buffer still exists on disk so their work can be
        # reopened after a restart. Only drop empty Untitled tabs that never held any data.
        for idx in range(len(prefs.open_files) - 1, -1, -1):
            item = prefs.open_files[idx]
            if item.filepath:
                continue
            if item.temp_filepath and os.path.exists(item.temp_filepath):
                continue
            prefs.open_files.remove(idx)

        next_title = generate_next_untitled_name(prefs)
        new_item = prefs.open_files.add()
        new_item.uid = uuid.uuid4().hex[:8]
        new_item.display_name = next_title
        new_item.has_custom_label = False
        prefs.active_tab_uid = new_item.uid
        mark_prefs_dirty(bpy.context)

    return None


@persistent
def on_file_loaded(_dummy):
    """Sync tab strip whenever a .blend file or new scene is opened."""
    prefs = get_addon_prefs(bpy.context)
    if not prefs:
        return

    current_fp = bpy.data.filepath

    if is_temp_buffer_path(current_fp):
        if prefs.remember_viewport_state:
            bpy.app.timers.register(restore_viewport_from_active_tab, first_interval=0.05)
        return

    if current_fp:
        norm_cur = os.path.normpath(current_fp)
        matched_item = None
        for item in prefs.open_files:
            if item.filepath and os.path.normpath(item.filepath) == norm_cur:
                matched_item = item
                break

        if not matched_item:
            prev_item, _ = get_active_tab_item(prefs)
            if prev_item and not prev_item.filepath and not prev_item.is_virtual_dirty and not prev_item.temp_filepath:
                matched_item = prev_item
            else:
                matched_item = prefs.open_files.add()
                matched_item.uid = uuid.uuid4().hex[:8]

            matched_item.filepath = current_fp
            if not (matched_item.has_custom_label and matched_item.display_name):
                matched_item.display_name = os.path.basename(current_fp)
            matched_item.is_virtual_dirty = False
            mark_prefs_dirty(bpy.context)

        prefs.active_tab_uid = matched_item.uid

        if prefs.remember_viewport_state:
            bpy.app.timers.register(restore_viewport_from_active_tab, first_interval=0.05)
        return

    active_item, _ = get_active_tab_item(prefs)
    if not active_item or active_item.filepath:
        next_title = generate_next_untitled_name(prefs)
        new_item = prefs.open_files.add()
        new_item.uid = uuid.uuid4().hex[:8]
        new_item.display_name = next_title
        new_item.has_custom_label = False
        prefs.active_tab_uid = new_item.uid
        mark_prefs_dirty(bpy.context)


@persistent
def on_file_saved(_dummy):
    """Update tab title & path when a project is saved or Saved As."""
    current_fp = bpy.data.filepath
    if not current_fp:
        return

    prefs = get_addon_prefs(bpy.context)
    if not prefs:
        return

    if is_temp_buffer_path(current_fp):
        if _INTERNAL_SAVE_DEPTH == 0:
            # The user pressed File > Save while an Untitled/duplicated tab's buffer was loaded.
            # Blender wrote into the add-on's cache folder, not a real project file. Keep the
            # tab marked unsaved and ask for a real location.
            item, _ = get_active_tab_item(prefs)
            if item:
                item.is_virtual_dirty = True
                mark_prefs_dirty(bpy.context)
            if item and item.filepath:
                # A saved project with background changes: File > Save goes to its real file.
                uid = item.uid

                def _save_real():
                    p = get_addon_prefs(bpy.context)
                    it, _ = get_active_tab_item(p) if p else (None, -1)
                    if it and it.uid == uid:
                        save_active_tab_to_disk(p)
                call_deferred(_save_real)
            else:
                call_deferred(lambda: bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT'))
        return

    if _INTERNAL_SAVE_DEPTH:
        # The add-on wrote a copy (e.g. Duplicate Tab); the open file and its tab are unchanged.
        return

    norm_cur = os.path.normpath(current_fp)
    active_item, _ = get_active_tab_item(prefs)

    existing_with_path = None
    for item in prefs.open_files:
        if item.filepath and os.path.normpath(item.filepath) == norm_cur:
            existing_with_path = item
            break

    if existing_with_path:
        if active_item and active_item.uid != existing_with_path.uid and not active_item.filepath:
            if active_item.has_custom_label and active_item.display_name:
                existing_with_path.display_name = active_item.display_name
                existing_with_path.has_custom_label = True
            remove_tab_temp_buffer(active_item)
            idx = list(prefs.open_files).index(active_item)
            prefs.open_files.remove(idx)
        if not (existing_with_path.has_custom_label and existing_with_path.display_name):
            existing_with_path.display_name = os.path.basename(current_fp)
        remove_tab_temp_buffer(existing_with_path)
        existing_with_path.temp_filepath = ""
        existing_with_path.is_virtual_dirty = False
        prefs.active_tab_uid = existing_with_path.uid
    elif active_item:
        remove_tab_temp_buffer(active_item)
        active_item.temp_filepath = ""
        active_item.filepath = current_fp
        if not (active_item.has_custom_label and active_item.display_name):
            active_item.display_name = os.path.basename(current_fp)
        active_item.is_virtual_dirty = False
        prefs.active_tab_uid = active_item.uid
    else:
        new_item = prefs.open_files.add()
        new_item.uid = uuid.uuid4().hex[:8]
        new_item.filepath = current_fp
        new_item.display_name = os.path.basename(current_fp)
        new_item.has_custom_label = False
        prefs.active_tab_uid = new_item.uid

    save_current_viewport_to_tab(bpy.context)
    mark_prefs_dirty(bpy.context)


# -------------------------------------------------------------------
# UI: Project Tab Strip, Menu, N-Panel, & About/Support Sub-Panel
# -------------------------------------------------------------------

class VIEW3D_MT_smart_tabs_menu(bpy.types.Menu):
    """Smart Tabs dropdown menu"""
    bl_label = "Smart Tabs"
    bl_idname = "VIEW3D_MT_smart_tabs_menu"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        prefs = get_addon_prefs(context)

        layout.operator("wm.new_project_tab", text="New Tab", icon='FILE_NEW')
        dup_op = layout.operator("wm.duplicate_project_tab", text="Duplicate Tab", icon='DUPLICATE')
        dup_op.index = -1
        ren_op = layout.operator("wm.rename_project_tab", text="Rename Tab...", icon='OUTLINER_DATA_FONT')
        ren_op.index = -1
        layout.operator("wm.add_blend_tab", text="Open in New Tab...", icon='FILE_FOLDER')
        layout.operator("wm.search_tabs_popup", text="Search Tabs...", icon='VIEWZOOM')
        row = layout.row()
        row.enabled = bool(load_closed_tabs())
        row.operator("wm.reopen_closed_tab", text="Reopen Closed Tab", icon='LOOP_BACK')
        layout.operator("wm.save_all_tabs", text="Save All Tabs", icon='FILE_TICK')
        layout.separator()

        if prefs and prefs.tab_mode == 'FILES':
            _active_item, active_idx = get_active_tab_item(prefs)
            if active_idx != -1:
                ctx_op = layout.operator("wm.tab_context_menu", text="Active Tab Actions...", icon='PREFERENCES')
                ctx_op.index = active_idx
                layout.separator()

        layout.operator("wm.copy_tab_data", text="Copy to Tab Clipboard", icon='COPYDOWN')
        layout.operator("wm.paste_tab_data", text="Paste from Tab Clipboard", icon='PASTEDOWN')
        layout.separator()

        if prefs:
            layout.prop(prefs, "tab_mode", text="Mode")
            layout.prop(prefs, "keep_current_ui")
            layout.prop(prefs, "remember_viewport_state")
            layout.prop(prefs, "reuse_existing_materials")
            layout.separator()

        layout.operator("wm.close_other_tabs", text="Close Other Tabs", icon='PANEL_CLOSE').index = -1
        layout.operator("wm.close_unpinned_tabs", text="Close Unpinned Inactive Tabs", icon='BRUSH_DATA')
        layout.operator("wm.clean_missing_tabs", text="Clean Missing File Tabs", icon='TRASH')


def render_project_tab_strip(layout, context, prefs):
    """Draw the cohesive top-bar Project Tab Strip."""
    row = layout.row(align=True)
    row.separator(factor=1.0)

    if prefs.tab_mode == 'FILES':
        active_item, _ = get_active_tab_item(prefs)

        for idx, item in enumerate(prefs.open_files):
            is_active = bool(active_item and item.uid == active_item.uid)
            dirty_star = "*" if is_tab_item_dirty(item, is_active) else ""
            base_title = item.display_name or (os.path.basename(item.filepath) if item.filepath else "Untitled 1")
            tab_label = f" {base_title}{dirty_star} "

            tab_unit = row.row(align=True)
            tab_icon = (
                'PINNED'
                if item.is_pinned
                else ('FILE_BLEND' if item.filepath else 'FILE_NEW')
            )

            switch_op = tab_unit.operator(
                "wm.switch_blend_tab",
                text=tab_label,
                depress=is_active,
                emboss=True,
                icon=tab_icon,
            )
            switch_op.tab_uid = item.uid
            switch_op.filepath = item.filepath

            close_op = tab_unit.operator(
                "wm.close_blend_tab",
                text="",
                icon='X',
                depress=is_active,
                emboss=True,
            )
            close_op.index = idx

            row.separator(factor=0.3)

        active_uid = active_item.uid if active_item else ""
        unsaved = [it for it in prefs.open_files if is_tab_item_dirty(it, it.uid == active_uid)]
        if any(it.uid != active_uid for it in unsaved):
            row.operator("wm.save_all_tabs", text=f"{len(unsaved)} unsaved", icon='ERROR')
            row.separator(factor=0.3)

        row.operator("wm.new_project_tab", text="", icon='ADD', emboss=True)
        dup_top = row.operator("wm.duplicate_project_tab", text="", icon='DUPLICATE', emboss=True)
        dup_top.index = -1
        row.menu("VIEW3D_MT_smart_tabs_menu", text="", icon='DOWNARROW_HLT')

    elif prefs.tab_mode == 'SCENES':
        active_scene = context.window.scene
        for scene in bpy.data.scenes:
            is_active = (scene == active_scene)
            op = row.operator(
                "wm.switch_scene_tab",
                text=f" {scene.name} ",
                depress=is_active,
                emboss=True,
                icon='SCENE_DATA',
            )
            op.scene_name = scene.name
            row.separator(factor=0.3)

        row.operator("scene.new", text="", icon='ADD', emboss=True)
        dup_scn = row.operator("wm.duplicate_project_tab", text="", icon='DUPLICATE', emboss=True)
        dup_scn.index = -1
        row.menu("VIEW3D_MT_smart_tabs_menu", text="", icon='DOWNARROW_HLT')


def draw_view3d_smart_tabs(self, context):
    prefs = get_addon_prefs(context)
    if not prefs:
        return
    render_project_tab_strip(self.layout, context, prefs)


def draw_window_menu_tabs(self, context):
    """List all open project tabs in Blender's top 'Window' menu."""
    prefs = get_addon_prefs(context)
    if not prefs or len(prefs.open_files) == 0:
        return

    layout = self.layout
    layout.separator()
    active_item, _ = get_active_tab_item(prefs)

    for item in prefs.open_files:
        is_active = bool(active_item and item.uid == active_item.uid)
        star = "*" if is_tab_item_dirty(item, is_active) else ""
        op = layout.operator(
            "wm.switch_blend_tab",
            text=f"{item.display_name}{star}",
            icon='CHECKMARK' if is_active else 'FILE_BLEND',
        )
        op.tab_uid = item.uid
        op.filepath = item.filepath


class VIEW3D_PT_smart_tabs_panel(bpy.types.Panel):
    """Sidebar panel for managing project tabs, context actions, clipboard options, and sessions"""
    bl_label = "Smart Tabs"
    bl_idname = "VIEW3D_PT_smart_tabs_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Smart Tabs"

    def draw(self, context):
        layout = self.layout
        prefs = get_addon_prefs(context)
        if not prefs:
            layout.label(text="Enable as an Extension to access preferences.", icon='INFO')
            return

        top_row = layout.row(align=True)
        top_row.operator("wm.new_project_tab", text="New", icon='FILE_NEW')
        dup_btn = top_row.operator("wm.duplicate_project_tab", text="Duplicate", icon='DUPLICATE')
        dup_btn.index = -1
        ren_btn = top_row.operator("wm.rename_project_tab", text="Rename", icon='OUTLINER_DATA_FONT')
        ren_btn.index = -1
        top_row.operator("wm.add_blend_tab", text="Open", icon='FILE_FOLDER')
        top_row.operator("wm.search_tabs_popup", text="", icon='VIEWZOOM')

        disp_box = layout.box()
        disp_box.prop(prefs, "tab_mode", expand=True)

        if prefs.tab_mode == 'FILES':
            list_box = layout.box()
            list_box.label(text="Open Tabs:", icon='FILE_BLEND')

            if not prefs.open_files:
                list_box.label(text="No tabs open.")
            else:
                active_item, _ = get_active_tab_item(prefs)
                for idx, item in enumerate(prefs.open_files):
                    is_active = bool(active_item and item.uid == active_item.uid)
                    star = "*" if is_tab_item_dirty(item, is_active) else ""
                    row = list_box.row(align=True)

                    pin_op = row.operator(
                        "wm.toggle_pin_blend_tab",
                        text="",
                        icon='PINNED' if item.is_pinned else 'UNPINNED',
                        depress=item.is_pinned,
                    )
                    pin_op.index = idx

                    switch_op = row.operator(
                        "wm.switch_blend_tab",
                        text=f"{item.display_name}{star}",
                        depress=is_active,
                    )
                    switch_op.tab_uid = item.uid
                    switch_op.filepath = item.filepath

                    ren_row_op = row.operator("wm.rename_project_tab", text="", icon='OUTLINER_DATA_FONT')
                    ren_row_op.index = idx

                    dup_row_op = row.operator("wm.duplicate_project_tab", text="", icon='DUPLICATE')
                    dup_row_op.index = idx

                    ctx_row_op = row.operator("wm.tab_context_menu", text="", icon='DOWNARROW_HLT')
                    ctx_row_op.index = idx

                    close_op = row.operator("wm.close_blend_tab", text="", icon='X')
                    close_op.index = idx

                maint_row = list_box.row(align=True)
                maint_row.operator("wm.close_other_tabs", text="Close Others", icon='PANEL_CLOSE').index = -1
                maint_row.operator("wm.close_unpinned_tabs", text="Close Unpinned", icon='BRUSH_DATA')
                maint_row.operator("wm.clean_missing_tabs", text="Clean Missing", icon='TRASH')
                save_row = list_box.row(align=True)
                save_row.operator("wm.save_all_tabs", text="Save All", icon='FILE_TICK')
                reopen = save_row.row(align=True)
                reopen.enabled = bool(load_closed_tabs())
                reopen.operator("wm.reopen_closed_tab", text="Reopen Closed", icon='LOOP_BACK')

        clip_box = layout.box()
        clip_box.label(text="Asset & Material Transfer:", icon='MATERIAL')
        btn_row = clip_box.row(align=True)
        btn_row.scale_y = 1.15
        btn_row.operator("wm.copy_tab_data", text="Copy", icon='COPYDOWN')
        btn_row.operator("wm.paste_tab_data", text="Paste", icon='PASTEDOWN')

        col_clip = clip_box.column(align=True)
        col_clip.prop(prefs, "override_standard_copy_paste")
        col_clip.prop(prefs, "reuse_existing_materials")
        col_clip.prop(prefs, "include_modifier_targets")
        col_clip.prop(prefs, "include_all_materials")
        col_clip.prop(prefs, "include_world")
        col_clip.prop(prefs, "paste_at_cursor")
        col_clip.prop(prefs, "link_from_source")

        opt_box = layout.box()
        opt_box.label(text="Workspace & Viewport Memory:", icon='VIEW3D')
        col_opt = opt_box.column(align=True)
        col_opt.prop(prefs, "auto_save_on_switch")
        col_opt.prop(prefs, "keep_current_ui")
        col_opt.prop(prefs, "remember_viewport_state")

        sess_box = layout.box()
        sess_box.label(text="Project Tab Sessions:", icon='BOOKMARKS')
        save_row = sess_box.row(align=True)
        save_row.prop(prefs, "session_name_input", text="")
        save_row.operator("wm.save_tab_session", text="Save", icon='FILE_TICK')

        load_row = sess_box.row(align=True)
        load_row.prop(prefs, "selected_session", text="")
        load_row.operator("wm.load_tab_session", text="Load", icon='IMPORT')
        load_row.operator("wm.delete_tab_session", text="", icon='TRASH')


class VIEW3D_PT_smart_tabs_about_panel(bpy.types.Panel):
    """Collapsible About & Support sub-panel displaying Graph Team metadata and support tools"""
    bl_label = "About & Support"
    bl_idname = "VIEW3D_PT_smart_tabs_about_panel"
    bl_parent_id = "VIEW3D_PT_smart_tabs_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Smart Tabs"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_about_support_ui(self.layout)


# -------------------------------------------------------------------
# Keymaps & Registration
# -------------------------------------------------------------------

addon_keymaps = []

classes = (
    TAB_PG_file_item,
    TAB_AddonPreferences,
    WM_OT_copy_tab_data,
    WM_OT_paste_tab_data,
    WM_OT_new_project_tab,
    WM_OT_duplicate_project_tab,
    WM_OT_rename_project_tab,
    WM_OT_switch_blend_tab,
    OBJECT_OT_smart_tabs_copy_attributes,
    OBJECT_OT_smart_tabs_paste_attributes,
    VIEW3D_MT_smart_tabs_copy_paste,
    WM_OT_discard_tab_changes,
    WM_OT_close_blend_tab,
    WM_OT_save_as_and_close_tab,
    WM_OT_save_tab_aware,
    WM_OT_copy_support_email,
    WM_OT_open_tabs_config_folder,
    WM_OT_close_other_tabs,
    WM_OT_close_tabs_to_right,
    WM_OT_copy_tab_filepath,
    WM_OT_open_tab_folder,
    WM_OT_tab_context_menu,
    WM_OT_search_tabs_popup,
    WM_OT_cycle_tabs,
    WM_OT_add_blend_tab,
    WM_OT_toggle_pin_blend_tab,
    WM_OT_move_blend_tab,
    WM_OT_close_unpinned_tabs,
    WM_OT_clean_missing_tabs,
    WM_OT_save_all_tabs,
    WM_OT_reopen_closed_tab,
    WM_OT_save_tab_session,
    WM_OT_load_tab_session,
    WM_OT_delete_tab_session,
    WM_OT_switch_scene_tab,
    VIEW3D_MT_smart_tabs_menu,
    VIEW3D_PT_smart_tabs_panel,
    VIEW3D_PT_smart_tabs_about_panel,
)


def register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return

    km_win = kc.keymaps.new(name='Window', space_type='EMPTY')

    kmi_next = km_win.keymap_items.new(WM_OT_cycle_tabs.bl_idname, 'TAB', 'PRESS', ctrl=True)
    kmi_next.properties.delta = 1
    addon_keymaps.append((km_win, kmi_next))

    kmi_pgdn = km_win.keymap_items.new(WM_OT_cycle_tabs.bl_idname, 'PAGE_DOWN', 'PRESS', ctrl=True)
    kmi_pgdn.properties.delta = 1
    addon_keymaps.append((km_win, kmi_pgdn))

    kmi_prev = km_win.keymap_items.new(WM_OT_cycle_tabs.bl_idname, 'TAB', 'PRESS', ctrl=True, shift=True)
    kmi_prev.properties.delta = -1
    addon_keymaps.append((km_win, kmi_prev))

    kmi_pgup = km_win.keymap_items.new(WM_OT_cycle_tabs.bl_idname, 'PAGE_UP', 'PRESS', ctrl=True)
    kmi_pgup.properties.delta = -1
    addon_keymaps.append((km_win, kmi_pgup))

    kmi_search = km_win.keymap_items.new(WM_OT_search_tabs_popup.bl_idname, 'T', 'PRESS', ctrl=True, shift=True)
    addon_keymaps.append((km_win, kmi_search))

    kmi_altv = km_win.keymap_items.new(WM_OT_search_tabs_popup.bl_idname, 'V', 'PRESS', alt=True)
    addon_keymaps.append((km_win, kmi_altv))

    kmi_new_tab = km_win.keymap_items.new(WM_OT_new_project_tab.bl_idname, 'N', 'PRESS', ctrl=True, alt=True)
    addon_keymaps.append((km_win, kmi_new_tab))

    kmi_dup_tab = km_win.keymap_items.new(WM_OT_duplicate_project_tab.bl_idname, 'D', 'PRESS', ctrl=True, alt=True)
    kmi_dup_tab.properties.index = -1
    addon_keymaps.append((km_win, kmi_dup_tab))

    kmi_ren_tab = km_win.keymap_items.new(WM_OT_rename_project_tab.bl_idname, 'R', 'PRESS', ctrl=True, alt=True)
    kmi_ren_tab.properties.index = -1
    addon_keymaps.append((km_win, kmi_ren_tab))

    kmi_open_tab = km_win.keymap_items.new(WM_OT_add_blend_tab.bl_idname, 'O', 'PRESS', ctrl=True, shift=True)
    addon_keymaps.append((km_win, kmi_open_tab))

    kmi_save = km_win.keymap_items.new(WM_OT_save_tab_aware.bl_idname, 'S', 'PRESS', ctrl=True)
    addon_keymaps.append((km_win, kmi_save))

    kmi_save_all = km_win.keymap_items.new(WM_OT_save_all_tabs.bl_idname, 'S', 'PRESS', ctrl=True, alt=True)
    addon_keymaps.append((km_win, kmi_save_all))

    kmi_reopen = km_win.keymap_items.new(WM_OT_reopen_closed_tab.bl_idname, 'T', 'PRESS', ctrl=True, alt=True)
    addon_keymaps.append((km_win, kmi_reopen))

    kmi_alt_copy = km_win.keymap_items.new(WM_OT_copy_tab_data.bl_idname, 'C', 'PRESS', ctrl=True, alt=True)
    addon_keymaps.append((km_win, kmi_alt_copy))

    kmi_alt_paste = km_win.keymap_items.new(WM_OT_paste_tab_data.bl_idname, 'V', 'PRESS', ctrl=True, alt=True)
    addon_keymaps.append((km_win, kmi_alt_paste))

    km_3d = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
    kmi_3d_copy = km_3d.keymap_items.new(WM_OT_copy_tab_data.bl_idname, 'C', 'PRESS', ctrl=True)
    addon_keymaps.append((km_3d, kmi_3d_copy))

    kmi_3d_paste = km_3d.keymap_items.new(WM_OT_paste_tab_data.bl_idname, 'V', 'PRESS', ctrl=True)
    addon_keymaps.append((km_3d, kmi_3d_paste))

    km_out = kc.keymaps.new(name='Outliner', space_type='OUTLINER')
    kmi_out_copy = km_out.keymap_items.new(WM_OT_copy_tab_data.bl_idname, 'C', 'PRESS', ctrl=True)
    addon_keymaps.append((km_out, kmi_out_copy))

    kmi_out_paste = km_out.keymap_items.new(WM_OT_paste_tab_data.bl_idname, 'V', 'PRESS', ctrl=True)
    addon_keymaps.append((km_out, kmi_out_paste))


def unregister_keymaps():
    for km, kmi in addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.VIEW3D_HT_header.append(draw_view3d_smart_tabs)
    bpy.types.TOPBAR_MT_window.append(draw_window_menu_tabs)
    if hasattr(bpy.types, "UI_MT_button_context_menu"):
        bpy.types.UI_MT_button_context_menu.append(draw_button_context_menu_tab_actions)
    bpy.types.VIEW3D_MT_object_context_menu.append(draw_object_context_copy_paste)
    bpy.types.OUTLINER_MT_object.append(draw_object_context_copy_paste)
    register_keymaps()

    if on_file_loaded not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_file_loaded)
    if on_file_saved not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(on_file_saved)

    bpy.app.timers.register(ensure_initial_tab_exists, first_interval=0.1)


def unregister():
    unregister_keymaps()
    bpy.types.OUTLINER_MT_object.remove(draw_object_context_copy_paste)
    bpy.types.VIEW3D_MT_object_context_menu.remove(draw_object_context_copy_paste)

    if on_file_saved in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(on_file_saved)
    if on_file_loaded in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_file_loaded)

    if hasattr(bpy.types, "UI_MT_button_context_menu"):
        bpy.types.UI_MT_button_context_menu.remove(draw_button_context_menu_tab_actions)
    bpy.types.TOPBAR_MT_window.remove(draw_window_menu_tabs)
    bpy.types.VIEW3D_HT_header.remove(draw_view3d_smart_tabs)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
