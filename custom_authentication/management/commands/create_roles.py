from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.apps import apps

class Command(BaseCommand):
    help = 'Creates default roles and optionally assigns permissions'

    def handle(self, *args, **kwargs):
        roles = {
            'Admin': [],
            'Developer': [],
            'Manager': [],
            'Guest': [],
            'Client': [],
        }

        for role_name, perms in roles.items():
            group, created = Group.objects.get_or_create(name=role_name)
            if created:
                self.stdout.write(self.style.SUCCESS(f'Created role "{role_name}"'))
            else:
                self.stdout.write(self.style.WARNING(f'ℹ Role "{role_name}" already exists'))

            # Optional: assign specific permissions
            for perm_codename in perms:
                try:
                    app_label, model_name, codename = perm_codename.split('.')
                    model = apps.get_model(app_label, model_name)
                    content_type = ContentType.objects.get_for_model(model)
                    perm = Permission.objects.get(content_type=content_type, codename=codename)
                    group.permissions.add(perm)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Failed to assign permission {perm_codename}: {str(e)}'))

