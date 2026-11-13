# integrations/nextcloud_webdav.py

import os
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
import requests
from django.contrib.auth import get_user_model


NEXTCLOUD_DAV_PATH = "/remote.php/dav/files"


def _get_admin_credentials():
    return (
        os.getenv("NEXTCLOUD_ADMIN_USER", "michael.kateregga@datasqan.com"),
        os.getenv("NEXTCLOUD_ADMIN_PASS", "superpassword"),
    )


def _get_base_url():
    return os.getenv("NEXTCLOUD_URL", "https://nextcloud.apps.datasqan.com")


def _get_nc_username(user):
    """Return the Nextcloud username for an aiDocuMines user."""
    return user.email


def _get_user_connection_override(user):
    """
    Check if the user has a custom NextcloudConnection.
    Returns (server_url, username, password) or None if using managed.
    """
    try:
        conn = user.nextcloud_connection
        if conn.connection_type == "custom" and conn.custom_server_url and conn.custom_username and conn.custom_password:
            return (conn.custom_server_url.rstrip("/"), conn.custom_username, conn.custom_password)
    except Exception:
        pass
    return None


def _dav_url(user):
    """Full WebDAV endpoint for a user's files."""
    override = _get_user_connection_override(user)
    if override:
        server_url, nc_user, _ = override
        return f"{server_url}{NEXTCLOUD_DAV_PATH}/{nc_user}/"
    nc_user = _get_nc_username(user)
    return f"{_get_base_url()}{NEXTCLOUD_DAV_PATH}/{nc_user}/"


def _get_auth(user):
    """Return (username, password) tuple for WebDAV requests — prefers user override."""
    override = _get_user_connection_override(user)
    if override:
        _, nc_user, nc_pass = override
        return (nc_user, nc_pass)
    return _get_admin_credentials()


def _propfind_xml():
    return """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
    <d:prop>
        <d:displayname/>
        <d:getcontenttype/>
        <d:getcontentlength/>
        <d:getlastmodified/>
        <d:resourcetype/>
    </d:prop>
</d:propfind>"""


def _parse_propfind(xml_content, base_url):
    """Parse a WebDAV PROPFIND multistat response into a list of file dicts."""
    ns = {
        "d": "DAV:",
    }
    root = ET.fromstring(xml_content)
    items = []

    for response_elem in root.findall("d:response", ns):
        href = response_elem.find("d:href", ns)
        if href is None or not href.text:
            continue
        href_text = href.text.rstrip("/")

        # Skip the directory itself (base URL)
        if href_text.rstrip("/") == base_url.rstrip("/"):
            continue

        propstat = response_elem.find("d:propstat", ns)
        if propstat is None:
            continue
        prop = propstat.find("d:prop", ns)
        if prop is None:
            continue

        displayname_el = prop.find("d:displayname", ns)
        contenttype_el = prop.find("d:getcontenttype", ns)
        contentlength_el = prop.find("d:getcontentlength", ns)
        lastmodified_el = prop.find("d:getlastmodified", ns)
        resourcetype_el = prop.find("d:resourcetype", ns)

        is_collection = resourcetype_el is not None and resourcetype_el.find("d:collection", ns) is not None
        name = displayname_el.text if displayname_el is not None and displayname_el.text else href_text.split("/")[-1]

        item = {
            "id": href_text,
            "name": name,
            "type": "folder" if is_collection else "file",
            "path": href_text,
            "mime_type": None if is_collection else (contenttype_el.text if contenttype_el is not None else "application/octet-stream"),
            "size": None if is_collection else (int(contentlength_el.text) if contentlength_el is not None and contentlength_el.text else 0),
            "modified_date": lastmodified_el.text if lastmodified_el is not None else None,
            "can_download": not is_collection,
        }
        items.append(item)

    return items


def list_files(user, folder_path=""):
    """
    List files and folders in a user's Nextcloud directory via WebDAV PROPFIND.

    Returns a list of dicts with keys: id, name, type, path, mime_type, size, modified_date, can_download.
    """
    dav_root = _dav_url(user)
    url = urljoin(dav_root, folder_path)

    auth_user, auth_pass = _get_auth(user)

    resp = requests.request(
        "PROPFIND",
        url,
        auth=(auth_user, auth_pass),
        data=_propfind_xml(),
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        timeout=30,
    )

    if resp.status_code == 207:
        return _parse_propfind(resp.content, url)
    if resp.status_code in (404,):
        return []
    resp.raise_for_status()
    return []


def download_file(user, file_path):
    """
    Download a file from Nextcloud via WebDAV GET.

    Returns the raw file content.
    """
    dav_root = _dav_url(user)
    url = urljoin(dav_root, file_path)

    auth_user, auth_pass = _get_auth(user)
    resp = requests.get(url, auth=(auth_user, auth_pass), timeout=120)
    resp.raise_for_status()
    return resp.content


def upload_file(user, dest_path, content):
    """
    Upload content to Nextcloud via WebDAV PUT.

    dest_path is relative to the user's DAV root (e.g. "Documents/report.pdf").
    Returns True on success.
    """
    dav_root = _dav_url(user)
    url = urljoin(dav_root, dest_path)

    auth_user, auth_pass = _get_auth(user)
    resp = requests.put(url, auth=(auth_user, auth_pass), data=content, timeout=120)

    if resp.status_code in (201, 204):
        _scan_user(user)
        return True

    resp.raise_for_status()


def ensure_folder(user, folder_path):
    """Create a folder (and parents) in Nextcloud via WebDAV MKCOL."""
    dav_root = _dav_url(user)
    url = urljoin(dav_root, folder_path)

    auth_user, auth_pass = _get_auth(user)
    resp = requests.request("MKCOL", url, auth=(auth_user, auth_pass), timeout=30)

    if resp.status_code in (201, 405):
        return True
    resp.raise_for_status()


def delete_file(user, file_path):
    """Delete a file or folder in Nextcloud via WebDAV DELETE."""
    dav_root = _dav_url(user)
    url = urljoin(dav_root, file_path)

    auth_user, auth_pass = _get_auth(user)
    resp = requests.delete(url, auth=(auth_user, auth_pass), timeout=30)

    if resp.status_code in (204, 404):
        _scan_user(user)
        return True
    resp.raise_for_status()


def test_connection(server_url, username, password):
    """
    Verify Nextcloud WebDAV credentials by performing a PROPFIND on the root.
    Returns True if the connection is valid, raises an Exception otherwise.
    """
    dav_url = f"{server_url.rstrip('/')}{NEXTCLOUD_DAV_PATH}/{username}/"
    resp = requests.request(
        "PROPFIND",
        dav_url,
        auth=(username, password),
        data=_propfind_xml(),
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        timeout=15,
    )
    if resp.status_code == 207:
        return True
    if resp.status_code == 401:
        raise PermissionError("Invalid credentials — check your username and app password.")
    if resp.status_code == 404:
        raise ConnectionError("User not found on the server — check the username.")
    resp.raise_for_status()
    return True


def _scan_user(user):
    """Run OCC files:scan for the user via docker exec."""
    nc_user = _get_nc_username(user)
    container = os.getenv("NEXTCLOUD_DOCKER_CONTAINER", "srv-captain--nextcloud")
    try:
        import subprocess
        subprocess.run(
            ["docker", "exec", "-u", "www-data", container, "php", "occ", "files:scan", nc_user],
            capture_output=True,
            timeout=60,
        )
    except Exception:
        pass
