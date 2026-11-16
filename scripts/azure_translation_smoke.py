import os
import time
import datetime
import configparser
from dotenv import load_dotenv
from icecream import ic
from azure.core.credentials import AzureKeyCredential
from azure.ai.translation.document import DocumentTranslationClient
from azure.storage.blob import BlobServiceClient, generate_container_sas, ContainerSasPermissions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

# Load environment variables
load_dotenv()

# Logger setup
ic("Loading Azure configuration...")

# Load Azure config from `config.ini`
config = configparser.ConfigParser()
config.read("config.ini")
client_name = "devtests"

# Use ENV variables first, then fallback to `config.ini`
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME") or config[client_name]["storage_account_name"]
STORAGE_URL = os.getenv("STORAGE_URL") or config[client_name]["storage_url"]
STORAGE_ACCOUNT_KEY = os.getenv("STORAGE_ACCOUNT_KEY")
CONNECTION_STRING = os.getenv("CONNECTION_STRING") or config[client_name]["connection_string"]
TRANSLATOR_TEXT_KEY = os.getenv("TRANSLATOR_DOCUMENT_KEY")
TRANSLATOR_TEXT_ENDPOINT = os.getenv("TRANSLATOR_DOCUMENT_ENDPOINT") or config[client_name]["translator_document_endpoint"]

# Initialize Azure clients
blob_service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
translation_client = DocumentTranslationClient(TRANSLATOR_TEXT_ENDPOINT, AzureKeyCredential(TRANSLATOR_TEXT_KEY))

ic("Azure configuration loaded successfully!")


def force_delete_container(blob_service_client, container_name):
    """ Tries to delete a container forcefully and ensures it's completely removed before proceeding. """
    ic(f"Attempting to delete container: {container_name}...")

    container_client = blob_service_client.get_container_client(container_name)

    try:
        container_client.delete_container()
        ic(f"Deleted container: {container_name}")
    except ResourceNotFoundError:
        ic(f"Container '{container_name}' does not exist, skipping delete.")
        return
    except ResourceExistsError:
        ic(f"Container '{container_name}' is already being deleted. Waiting for Azure to complete the process...")

    # Wait for the container to be fully deleted
    wait_time = 5
    max_wait_time = 300  # 5 minutes
    elapsed_time = 0

    while container_client.exists():
        if elapsed_time >= max_wait_time:
            ic(f"ERROR: Container '{container_name}' is still being deleted after {max_wait_time} seconds. Aborting.")
            exit(1)

        ic(f"Waiting for '{container_name}' to be fully deleted... Retrying in {wait_time} seconds.")
        time.sleep(wait_time)
        elapsed_time += wait_time
        wait_time = min(wait_time * 2, 30)  # Exponential backoff, max 30 seconds


def ensure_container_exists(blob_service_client, container_name):
    """ Ensures an Azure Blob Storage container exists. """
    container_client = blob_service_client.get_container_client(container_name)

    if container_client.exists():
        ic(f"Container '{container_name}' already exists.")
        return

    force_delete_container(blob_service_client, container_name)

    ic(f"Creating container: {container_name}")
    container_client.create_container()
    ic(f"Created container: {container_name}")


# Generate a SAS token for Azure Blob Storage
def generate_sas(storage_account_name, storage_account_key, container_name, storage_url):
    """ Generates a SAS token for the container. """
    ic(f"Generating SAS token for {container_name}...")

    sas_permissions = ContainerSasPermissions(read=True, write=True, delete=True, list=True, add=True, create=True)

    sas_token = generate_container_sas(
        storage_account_name, container_name,
        account_key=storage_account_key, permission=sas_permissions,
        start=datetime.datetime.utcnow(),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)  # 2-hour validity
    )

    full_sas_url = f"{storage_url}{container_name}?{sas_token}"
    
    ic(f"Auto generated sas_url: {full_sas_url}")
    
    return full_sas_url


# Upload files to Azure Blob Storage
def upload_files_to_container(folder_src, blob_service_client, container_name):
    """ Uploads files to an Azure Blob Storage container. """
    ensure_container_exists(blob_service_client, container_name)
    container_client = blob_service_client.get_container_client(container_name)

    files = [f for f in os.listdir(folder_src) if os.path.isfile(os.path.join(folder_src, f)) and f != ".DS_Store"]
    ic(f"Uploading {len(files)} files to {container_name}...")

    for filename in files:
        filepath = os.path.join(folder_src, filename)
        with open(filepath, "rb") as data:
            container_client.upload_blob(name=filename, data=data, overwrite=True)
            ic(f"Uploaded: {filename}")


# Translate files using Azure Document Translation API
def translate_files(input_sas_url, output_sas_url, translator_endpoint, translator_key):
    """ Translates documents in Azure Blob Storage. """
    ic("Starting document translation...")

    client = DocumentTranslationClient(translator_endpoint, AzureKeyCredential(translator_key))
    poller = client.begin_translation(input_sas_url, output_sas_url, target_language="fr")  # Translate to French

    while not poller.done():
        ic(f"Translation in progress... Status: {poller.status()}")
        time.sleep(5)

    ic("Translation completed!")
    return poller.result()


# Download translated files from Azure
def download_files(output_container, download_folder, blob_service_client):
    """ Downloads translated files from Azure Blob Storage. """
    ic(f"Downloading files from {output_container}...")

    output_container_client = blob_service_client.get_container_client(output_container)
    os.makedirs(download_folder, exist_ok=True)

    for blob in output_container_client.list_blobs():
        blob_client = output_container_client.get_blob_client(blob.name)
        local_path = os.path.join(download_folder, blob.name)

        with open(local_path, "wb") as file:
            file.write(blob_client.download_blob().readall())

        ic(f"Downloaded: {local_path}")


# Main Execution
if __name__ == "__main__":
    start_time = time.time()

    # Define Containers and Folders
    SOURCE_FOLDER = "./data/indata"
    TRANSLATED_FOLDER = "./data/outdata"
    INPUT_CONTAINER = f"translations-source-{int(time.time())}"
    OUTPUT_CONTAINER = f"translations-target-{int(time.time())}"

    # Ensure containers exist
    ensure_container_exists(blob_service_client, INPUT_CONTAINER)
    ensure_container_exists(blob_service_client, OUTPUT_CONTAINER)

    # Upload Files
    upload_files_to_container(SOURCE_FOLDER, blob_service_client, INPUT_CONTAINER)

    # Generate SAS URLs
    input_sas_url = generate_sas(STORAGE_ACCOUNT_NAME, STORAGE_ACCOUNT_KEY, INPUT_CONTAINER, STORAGE_URL)
    output_sas_url = generate_sas(STORAGE_ACCOUNT_NAME, STORAGE_ACCOUNT_KEY, OUTPUT_CONTAINER, STORAGE_URL)

    # Translate Documents
    translate_files(input_sas_url, output_sas_url, TRANSLATOR_TEXT_ENDPOINT, TRANSLATOR_TEXT_KEY)

    # Download Translated Files
    download_files(OUTPUT_CONTAINER, TRANSLATED_FOLDER, blob_service_client)
    
    time.sleep(5)
    
    # Delete the container blobs in the storage account after a successful download
    force_delete_container(blob_service_client, INPUT_CONTAINER)
    force_delete_container(blob_service_client, OUTPUT_CONTAINER)

    duration = time.time() - start_time
    ic(f"Translation process completed in {duration:.2f} seconds!")
