#!/usr/bin/env python3
"""
Test script to verify folder creation logic for file uploads and translations.
Run inside the aiDocuMines Docker container or with proper Django settings.

Usage:
    python manage.py shell < test_folder_creation.py
    OR
    python test_folder_creation.py (if Django is set up)
"""
import os
import sys
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aiDocuMines.settings')
django.setup()

from django.contrib.auth import get_user_model
from document_operations.models import Folder, FileFolderLink
from document_operations.utils import get_or_create_folder_tree, register_file_folder_link
from core.models import File

User = get_user_model()


def test_get_or_create_folder_tree():
    """Test the folder tree creation function"""
    print("\n=== Testing get_or_create_folder_tree ===\n")
    
    # Get or create a test user
    user, _ = User.objects.get_or_create(
        email="test_folder_user@test.com",
        defaults={"first_name": "Test", "last_name": "User"}
    )
    
    project_id = "test_project_123"
    service_id = "test_service_456"
    
    # Test case 1: Normal folder path
    path_parts = ["20250101", "translations", "Spanish"]
    print(f"Test 1: Creating folder tree for path_parts={path_parts}")
    
    folder = get_or_create_folder_tree(
        path_parts, user=user, project_id=project_id, service_id=service_id
    )
    
    print(f"  Created folder: id={folder.id}, name={folder.name}")
    print(f"  Parent: {folder.parent.name if folder.parent else 'None'}")
    
    # Verify the hierarchy
    current = folder
    hierarchy = []
    while current:
        hierarchy.insert(0, current.name)
        current = current.parent
    print(f"  Full hierarchy: {' -> '.join(hierarchy)}")
    assert hierarchy == ["20250101", "translations", "Spanish"], f"Expected hierarchy mismatch: {hierarchy}"
    print("  Test 1 PASSED\n")
    
    # Test case 2: Empty path parts
    path_parts_empty = [""]
    print(f"Test 2: Creating folder tree for empty path_parts={path_parts_empty}")
    
    folder_empty = get_or_create_folder_tree(
        path_parts_empty, user=user, project_id=project_id, service_id=service_id
    )
    print(f"  Result: folder={folder_empty}")
    if folder_empty:
        print(f"  Warning: Created folder with empty name! id={folder_empty.id}, name='{folder_empty.name}'")
    else:
        print("  Result is None as expected for empty path")
    print()
    
    # Test case 3: Verify root folders are returned by API-style query
    print("Test 3: Querying root folders (parent=None)")
    root_folders = Folder.objects.filter(
        user=user,
        project_id=project_id,
        service_id=service_id,
        parent=None,
        is_trashed=False
    )
    print(f"  Found {root_folders.count()} root folder(s):")
    for rf in root_folders:
        print(f"    - id={rf.id}, name='{rf.name}'")
    print()


def test_register_file_folder_link():
    """Test the register_file_folder_link function"""
    print("\n=== Testing register_file_folder_link ===\n")
    
    # Check if there are any files to test with
    files = File.objects.all()[:5]
    if not files:
        print("No files in database to test with")
        return
    
    for file_obj in files:
        print(f"File: id={file_obj.id}, filepath={file_obj.filepath}")
        print(f"  project_id={file_obj.project_id}, service_id={file_obj.service_id}")
        
        # Show existing folder links
        links = file_obj.folder_links.all()
        print(f"  Existing folder links: {links.count()}")
        for link in links:
            folder = link.folder
            hierarchy = []
            current = folder
            while current:
                hierarchy.insert(0, current.name)
                current = current.parent
            print(f"    - link_id={link.id}, folder='{' -> '.join(hierarchy)}'")
        
        # Test path parsing
        if file_obj.filepath and file_obj.project_id and file_obj.service_id:
            split_key = f"{file_obj.project_id}/{file_obj.service_id}/"
            if split_key in file_obj.filepath:
                relative_path = file_obj.filepath.split(split_key, 1)[-1]
                folder_parts = os.path.dirname(relative_path).split("/")
                print(f"  Expected folder_parts: {folder_parts}")
            else:
                print(f"  Cannot parse path: split key '{split_key}' not in '{file_obj.filepath}'")
        print()


def main():
    print("=" * 60)
    print("Folder Creation Test Script")
    print("=" * 60)
    
    test_get_or_create_folder_tree()
    test_register_file_folder_link()
    
    print("\n" + "=" * 60)
    print("Tests completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()

