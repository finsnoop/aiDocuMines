from django.test import TestCase
from django.contrib.auth import get_user_model
from unittest.mock import MagicMock, patch
import os

from document_operations.models import Folder, FileFolderLink
from custom_authentication.models import Client
from document_operations.utils import get_or_create_folder_tree, register_file_folder_link


User = get_user_model()


class GetOrCreateFolderTreeTestCase(TestCase):
    """Tests for the get_or_create_folder_tree function"""

    def setUp(self):
        """Create test user for all tests"""
        self.user = User.objects.create_user(
            client=Client.objects.create(name="Folder tests"),
            email="test@example.com",
            password="testpass123"
        )
        self.project_id = "test_project_123"
        self.service_id = "test_service_456"

    def test_creates_single_folder(self):
        """Test creating a single folder"""
        path_parts = ["folder1"]
        folder = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        self.assertIsNotNone(folder)
        self.assertEqual(folder.name, "folder1")
        self.assertIsNone(folder.parent)

    def test_creates_nested_folders(self):
        """Test creating nested folder hierarchy: 20250101/translations/Spanish"""
        path_parts = ["20250101", "translations", "Spanish"]
        folder = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # Verify the returned folder is the leaf (Spanish)
        self.assertEqual(folder.name, "Spanish")

        # Verify parent is "translations"
        self.assertIsNotNone(folder.parent)
        self.assertEqual(folder.parent.name, "translations")

        # Verify grandparent is "20250101"
        self.assertIsNotNone(folder.parent.parent)
        self.assertEqual(folder.parent.parent.name, "20250101")

        # Verify root has no parent
        self.assertIsNone(folder.parent.parent.parent)

    def test_idempotent_folder_creation(self):
        """Test that calling with same path_parts doesn't create duplicates"""
        path_parts = ["20250101", "translations", "Spanish"]

        folder1 = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )
        folder2 = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # Should return the same folder
        self.assertEqual(folder1.id, folder2.id)

        # Should only have 3 folders total
        folder_count = Folder.objects.filter(
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        ).count()
        self.assertEqual(folder_count, 3)

    def test_empty_path_parts(self):
        """Test with empty path parts (edge case)"""
        path_parts = []
        folder = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # Empty path_parts should return None (no folders created)
        self.assertIsNone(folder)

    def test_single_empty_string_path_part(self):
        """Test with single empty string (happens with os.path.dirname('file.pdf').split('/'))"""
        path_parts = [""]
        folder = get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # This creates a folder with empty name - might be a bug!
        # Document current behavior
        self.assertIsNotNone(folder)
        self.assertEqual(folder.name, "")

    def test_root_folder_query(self):
        """Test that root folders can be queried with parent=None"""
        # Create nested structure
        path_parts = ["20250101", "translations", "Spanish"]
        get_or_create_folder_tree(
            path_parts,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # Create another root folder
        path_parts2 = ["another_root", "subfolder"]
        get_or_create_folder_tree(
            path_parts2,
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id
        )

        # Query root folders
        root_folders = Folder.objects.filter(
            user=self.user,
            project_id=self.project_id,
            service_id=self.service_id,
            parent=None,
            is_trashed=False
        )

        # Should have exactly 2 root folders: "20250101" and "another_root"
        self.assertEqual(root_folders.count(), 2)
        root_names = set(root_folders.values_list('name', flat=True))
        self.assertEqual(root_names, {"20250101", "another_root"})
