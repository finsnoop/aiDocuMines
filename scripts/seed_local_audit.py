"""Create fictional local fixtures only; refuses every other environment."""
import hashlib
import os
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.conf import settings
from custom_authentication.models import Client
from core.models import File, Run, Matter
from document_operations.models import FileFolderLink
from document_operations.utils import get_or_create_folder_tree
import requests

if os.getenv("LOCAL_AUDIT") != "true":
    raise RuntimeError("This seed is for the isolated localhost audit stack only")
client, _ = Client.objects.get_or_create(name="Harper Lowe Demo")
user, created = get_user_model().objects.get_or_create(email="admin@harperlowe.demo",
    defaults={"client":client,"username":"admin-harperlowe-demo"})
user.client=client
user.set_password(os.environ["AUDIT_LOGIN_PASSWORD"])
user.is_staff=True
user.is_2fa_enabled=False
user.two_factor_enabled=False
user.save()
user.groups.add(Group.objects.get_or_create(name="Admin")[0])
Matter.objects.get_or_create(client=client,name="Local audit",defaults={"created_by":user})
folder=get_or_create_folder_tree(["Contracts"],user=user,project_id="Local audit",service_id="matter")
root=Path(settings.MEDIA_ROOT)/"local-audit"
root.mkdir(parents=True,exist_ok=True)
text="Demo services agreement. Either party may terminate on thirty days written notice. The contract price is ZAR 125000. Contact: legal@example.invalid."
path=root/"demo-agreement.txt"
path.write_text(text)
run=Run.objects.create(user=user,status="Completed")
file, _=File.objects.get_or_create(user=user,filename=path.name,project_id="Local audit",service_id="matter",
    defaults={"run":run,"filepath":str(path),"content":text,"file_size":path.stat().st_size,
              "md5_hash":hashlib.md5(path.read_bytes()).hexdigest(),"status":"Completed"})
FileFolderLink.objects.get_or_create(file=file,folder=folder)
from docx import Document
word = Document()
word.add_heading("Demo services agreement", level=1)
word.add_paragraph(text)
word_path = root / "demo-agreement.docx"
word.save(word_path)
word_file, _ = File.objects.get_or_create(user=user, filename=word_path.name,
    project_id="Local audit", service_id="matter", defaults={"run":run,"filepath":str(word_path),
    "content":text,"file_size":word_path.stat().st_size,"md5_hash":hashlib.md5(word_path.read_bytes()).hexdigest(),
    "status":"Completed"})
FileFolderLink.objects.get_or_create(file=word_file,folder=folder)
response=requests.post("http://node:7000/api/user",timeout=10,json={
    "username":user.username,"email":user.email,"password":os.environ["AUDIT_LOGIN_PASSWORD"],
    "client_id":"local-public-client"})
if response.status_code not in (200,201,409):
    raise RuntimeError("Could not seed the local Node registry")
print("Local fictional account and agreement are ready.")
