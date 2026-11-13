from rest_framework.permissions import BasePermission, SAFE_METHODS
from document_operations.models import EffectiveAccess, FileFolderLink, Folder, FileAccessEntry
from core.models import File


# --- Core Role Checks ---

class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_superuser


class IsAdminUserOnly(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_staff and not request.user.is_superuser


class IsRegularUser(BasePermission):
    def has_permission(self, request, view):
        return request.user and not request.user.is_staff and not request.user.is_superuser


# --- Group Role Checks ---

class IsGroupMember(BasePermission):
    group_name = None  # Override in subclasses

    def has_permission(self, request, view):
        return request.user and self.group_name and request.user.groups.filter(name=self.group_name).exists()


class IsManager(IsGroupMember):
    group_name = 'Manager'


class IsDeveloper(IsGroupMember):
    group_name = 'Developer'


class IsClient(IsGroupMember):
    group_name = 'Client'


# --- Combined Role Check Utility ---

#class HasAnyRole(BasePermission):
#    roles = []  # Can be: "superuser", "admin", "group:GroupName"

#    def has_permission(self, request, view):
#        if not request.user or not request.user.is_authenticated:
#            return False

#        for role in self.roles:
#            if role == 'superuser' and request.user.is_superuser:
#                return True
#            elif role == 'admin' and request.user.is_staff:
#                return True
#            elif role.startswith('group:'):
#                group_name = role.split(':', 1)[1]
#                if request.user.groups.filter(name=group_name).exists():
#                    return True
#        return False

class HasAnyRole(BasePermission):
    roles = []  # e.g. ["superuser", "admin", "group:Client"]

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        roles = get_user_roles(request.user)
        for role in self.roles:
            if role == 'superuser' and request.user.is_superuser:
                return True
            elif role == 'admin' and ('Admin' in roles or 'SuperAdmin' in roles):
                return True
            elif role.startswith('group:'):
                group_name = role.split(':', 1)[1]
                if group_name in roles:
                    return True

        return False

# --- Reusable Composites (Just Inherit) ---

class IsClientOrAdmin(HasAnyRole):
    roles = ['group:Client', 'admin']
    

class IsClientOrAdminOrSuperUser(HasAnyRole):
    roles = ['group:Client', 'admin', 'superuser']
    
class IsAdminOrSuperUser(HasAnyRole):
    roles = ['admin', 'superuser']

class IsClientOrAdminOrManager(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:SuperAdmin']
    
class IsClientOrAdminOrManagerOrSuperUser(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:SuperAdmin', 'superuser']
    
class IsClientOrAdminOrDeveloper(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Developer', 'group:SuperAdmin']
    
class IsClientOrAdminOrManagerOrDeveloper(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:Developer', 'group:SuperAdmin']

class IsClientOrSuperUser(HasAnyRole):
    roles = ['group:Client', 'superuser']


class IsClientOrManager(HasAnyRole):
    roles = ['group:Client', 'group:Manager']


class IsClientOrDeveloper(HasAnyRole):
    roles = ['group:Client', 'group:Developer']


class IsClientOrAdminOrManager(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:SuperAdmin']


class IsClientOrAdminOrDeveloper(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Developer', 'group:SuperAdmin']


class IsClientOrAdminOrManagerOrDeveloper(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:Developer', 'group:SuperAdmin']


class IsClientOrAdminOrManagerOrDeveloperOrSuperUser(HasAnyRole):
    roles = ['group:Client', 'admin', 'group:Manager', 'group:Developer', 'group:SuperAdmin', 'superuser']


# --- 1. Guest Role ---
class IsGuest(BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.groups.filter(name='Guest').exists()


# --- 2. Group Member (Any Group Member: Client, Manager, Developer, Guest, etc.) ---
class IsGroupMemberAny(BasePermission):
    """Grants access if user belongs to any group (Guest, Client, Manager, Developer, etc.)"""
    def has_permission(self, request, view):
        return request.user and request.user.groups.exists()


# --- 3. Strict Manager (Only Manager, Not Admin or Superuser) ---
class IsStrictManager(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user and
            not request.user.is_staff and
            not request.user.is_superuser and
            request.user.groups.filter(name='Manager').exists()
        )


# --- 4. Active Client (Client group + is_active = True) ---
class IsActiveClient(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user and
            request.user.is_active and
            request.user.groups.filter(name='Client').exists()
        )


# --- 5. Dynamic Role Checker Factory ---
def require_roles(*roles):
    """
    Usage:
        class MyView(APIView):
            permission_classes = [require_roles('admin', 'group:Manager', 'superuser')]
    """
    class DynamicRolePermission(BasePermission):
        def has_permission(self, request, view):
            if not request.user or not request.user.is_authenticated:
                return False

            for role in roles:
                if role == 'superuser' and request.user.is_superuser:
                    return True
                elif role == 'admin' and request.user.is_staff:
                    return True
                elif role.startswith('group:'):
                    group_name = role.split(':', 1)[1]
                    if request.user.groups.filter(name=group_name).exists():
                        return True
            return False

    return DynamicRolePermission

class IsFirstAdminOrSuperUser(BasePermission):
    """
    Grants access if:
    - No superuser exists (first-time setup), or
    - Authenticated user is a superuser
    """
    def has_permission(self, request, view):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        superuser_exists = User.objects.filter(is_superuser=True).exists()

        if not superuser_exists:
            return True  # allow open access for first admin creation

        return request.user and request.user.is_authenticated and request.user.is_superuser


# --- Document Operations Permissions ---

class IsOwner(BasePermission):
    """Allows access only to the owner of the file or folder."""
    def has_object_permission(self, request, view, obj):
        if hasattr(obj, 'user'):
            return obj.user == request.user
        if hasattr(obj, 'file'):
            return obj.file.user == request.user
        return False


class HasEffectiveAccess(BasePermission):
    """Grants or denies access based on effective access rules."""
    def has_object_permission(self, request, view, obj):
        access = None

        if isinstance(obj, Folder):
            access = EffectiveAccess.objects.filter(user=request.user, folder=obj).first()
        elif isinstance(obj, File):
            access = EffectiveAccess.objects.filter(user=request.user, file=obj).first()
        elif isinstance(obj, FileFolderLink):
            access = EffectiveAccess.objects.filter(user=request.user, file=obj.file).first()

        if not access:
            return False

        method_map = {
            'GET': 'can_download',
            'POST': 'can_share',
            'PUT': 'can_rename',
            'PATCH': 'can_move',
            'DELETE': 'can_delete',
        }

        required = method_map.get(request.method)
        return getattr(access, required, False) if required else True


class HasAnyAccess(BasePermission):
    """Checks if user has any access (owner via EffectiveAccess or sharee via FileAccessEntry)."""
    def has_object_permission(self, request, view, obj):
        if isinstance(obj, Folder):
            if EffectiveAccess.objects.filter(user=request.user, folder=obj).exists():
                return True
            return FileAccessEntry.objects.filter(user=request.user, file_link__folder=obj).exists()
        elif isinstance(obj, File):
            if EffectiveAccess.objects.filter(user=request.user, file=obj).exists():
                return True
            return FileAccessEntry.objects.filter(user=request.user, file_link__file=obj).exists()
        elif isinstance(obj, FileFolderLink):
            if EffectiveAccess.objects.filter(user=request.user, file=obj.file).exists():
                return True
            return FileAccessEntry.objects.filter(user=request.user, file_link=obj).exists()
        return False


class IsReadOnly(BasePermission):
    """Allows only GET, HEAD, and OPTIONS methods."""
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


class IsSuperSuperUser(BasePermission):
    """
    Grants access only if:
    - User is a Django superuser
    - AND belongs to the group 'SuperSuperUser'
    """
    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_superuser
            and request.user.groups.filter(name="SuperSuperUser").exists()
        )


# Import additional permission classes from the comprehensive RBAC module
from .rbac import (
    IsAdminOrManagerMutation,
    RequireAdminOrManagerMutation,
    HasPermission,
    HasAnyPermissions,
    HasAllPermissions,
    CanViewDocuments,
    CanCreateDocuments,
    CanSubmitOCR,
    CanSubmitTranslation,
    CanSubmitAnonymization,
    CanManageUsers,
    CanManageBackups,
    CanViewAuditLogs,
    CanManageRoles,
    CanManageIntegrations,
    CanSearch,
    seed_permissions_and_roles,
    PERMISSION_DEFINITIONS,
    ROLE_DEFINITIONS,
    get_role_permissions,
    get_permission_codenames,
    get_user_roles,
    get_user_role_permissions,
)

