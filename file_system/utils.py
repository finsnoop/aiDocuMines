# file_system/utils.py
import os
from django.db.models import Q  # if you need it elsewhere
from core.models import File
from document_operations.utils import get_user_accessible_file_ids
from django.contrib.auth import get_user_model

User = get_user_model()


def _clean_tree(nodes):
    """Remove inaccessible files (id < 0) and prune empty folders."""
    cleaned = []
    for node in nodes:
        if node["type"] == "file":
            if isinstance(node["id"], int) and node["id"] >= 0:
                cleaned.append(node)
        elif node["type"] == "folder":
            children = _clean_tree(node.get("children", []))
            if children:
                node["children"] = children
                cleaned.append(node)
    return cleaned


def get_user_file_tree(user, base_upload_dir=None):
    if not base_upload_dir:
        base_upload_dir = os.path.abspath(os.path.join("media", "uploads"))

    file_tree = []

    # 🔐 Files user can access: owned + shared
    accessible_ids = get_user_accessible_file_ids(user)
    all_files = File.objects.filter(id__in=accessible_ids)
    file_map = {os.path.normpath(os.path.abspath(f.filepath)): f.id for f in all_files}

    def resolve_folder_name(folder_name):
        """If folder_name is a UUID, look up the human-readable project name."""
        import uuid
        try:
            uuid.UUID(folder_name)
            from core.models import Project
            project = Project.objects.filter(id=folder_name).first()
            if project:
                return project.project_name
        except (ValueError, Exception):
            pass
        return folder_name

    def build_tree(abs_dir, rel_path_parts):
        items = []
        try:
            entries = os.listdir(abs_dir)
        except FileNotFoundError:
            return []

        for entry in sorted(entries):
            abs_path = os.path.join(abs_dir, entry)
            rel_path = os.path.join(*rel_path_parts, entry)

            if os.path.isdir(abs_path):
                display_name = resolve_folder_name(entry)
                items.append({
                    "id": entry,
                    "name": display_name,
                    "type": "folder",
                    "children": build_tree(abs_path, rel_path_parts + [entry])
                })
            else:
                norm_path = os.path.normpath(os.path.abspath(abs_path))
                file_id = file_map.get(norm_path, -1)
                download_url = f"/media/uploads/{rel_path}".replace("\\", "/")
                items.append({
                    "id": file_id,
                    "name": entry,
                    "type": "file",
                    "download_url": download_url,
                })

        return items

    # 1️⃣ Actual physical folder (user uploads)
    try:
        client_ids = os.listdir(base_upload_dir)
    except FileNotFoundError:
        client_ids = []

    for client_id in client_ids:
        client_dir = os.path.join(base_upload_dir, client_id)
        if not os.path.isdir(client_dir):
            continue

        user_dir = os.path.join(client_dir, str(user.id))
        if os.path.isdir(user_dir):
            children = build_tree(user_dir, [client_id, str(user.id)])
            children = _clean_tree(children)
            file_tree.extend(children)

    # 2️⃣ Virtual folders for shared files
    seen_virtual_paths = set()

    for file in all_files:
        abs_path = os.path.normpath(os.path.abspath(file.filepath))

        try:
            rel_path = abs_path.split("uploads" + os.sep, 1)[1]
        except IndexError:
            continue

        parts = rel_path.split(os.sep)
        if len(parts) < 3:
            continue

        # 🚩 Skip files you own — they're already in the physical tree!
        if file.user_id == user.id:
            continue

        filename = parts[-1]

        # Display path skips parts[0] (client) and parts[1] (owner_user_id)
        display_parts = parts[2:-1]

        # Dedup by display path + filename
        display_key = tuple(display_parts + [filename])
        if display_key in seen_virtual_paths:
            continue
        seen_virtual_paths.add(display_key)

        # Walk/create folders in file_tree according to display_parts
        current_level = file_tree
        for i, part in enumerate(display_parts):
            match = next((item for item in current_level
                          if item["type"] == "folder" and item["name"] == part), None)
            if not match:
                new_folder = {
                    "id": f"{'/'.join(display_parts[:i+1])}",
                    "name": part,
                    "type": "folder",
                    "children": []
                }
                current_level.append(new_folder)
                current_level = new_folder["children"]
            else:
                current_level = match["children"]

        current_level.append({
            "id": file.id,
            "name": filename,
            "type": "file",
            "download_url": f"/media/uploads/{rel_path}".replace("\\", "/")
        })

    return file_tree
