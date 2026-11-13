from django.db import models
from django.conf import settings
from core.models import File
# from fernet_fields import EncryptedTextField
# from encrypted_model_fields.fields import EncryptedCharField
from core.fields.encrypted_char import EncryptedCharField
from urllib.parse import quote_plus

User = settings.AUTH_USER_MODEL


class DatabaseConnection(models.Model):
    """
    Stores secure connection info to a remote database.
    """
    DATABASE_CHOICES = [
        ("postgres", "PostgreSQL"),
        ("mysql", "MySQL"),
        ("sqlite", "SQLite"),
        ("mssql", "SQL Server"),
        ("oracle", "Oracle"),
    ]

    name = models.CharField(max_length=255)
    database_type = models.CharField(max_length=20, choices=DATABASE_CHOICES)
    host = models.CharField(max_length=255)
    port = models.IntegerField()
    username = models.CharField(max_length=128)
    password = EncryptedCharField(max_length=255)
    database_name = models.CharField(max_length=255)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='db_connections')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.database_type})"
    
    def build_connection_uri(self):
        try:
            username = quote_plus(str(self.username))
            decrypted_password = str(self.password)  # This should trigger decryption
            password = quote_plus(decrypted_password)

            if self.database_type == "postgres":
                uri = f"postgresql://{username}:{password}@{self.host}:{self.port}/{self.database_name}"
            elif self.database_type == "mysql":
                uri = f"mysql+pymysql://{username}:{password}@{self.host}:{self.port}/{self.database_name}"
            elif self.database_type == "sqlite":
                uri = f"sqlite:///{self.database_name}"
            else:
                raise ValueError(f"Unsupported database type: {self.database_type}")

            return uri
        except Exception:
            raise

    '''
    def build_connection_uri(self):
        encoded_username = quote_plus(str(self.username))
        encoded_password = quote_plus(str(self.password))

        print(f"[🔐 DB URI DEBUG] username={self.username} (encoded={encoded_username}) password={self.password} (encoded={encoded_password})")

        if self.database_type == "postgres":
            uri = f"postgresql://{encoded_username}:{encoded_password}@{self.host}:{self.port}/{self.database_name}"
        elif self.database_type == "mysql":
            uri = f"mysql+pymysql://{encoded_username}:{encoded_password}@{self.host}:{self.port}/{self.database_name}"
        elif self.database_type == "mongodb":
            uri = f"mongodb://{encoded_username}:{encoded_password}@{self.host}:{self.port}/{self.database_name}"
        elif self.database_type == "mariadb":
            uri = f"mariadb+pymysql://{encoded_username}:{encoded_password}@{self.host}:{self.port}/{self.database_name}"
        elif self.database_type == "sqlite":
            uri = f"sqlite:///{self.database_name}"
        else:
            uri = None

        print(f"[🔐 DEBUG] user={self.username}, password={self.password}, uri={uri}")

        print(f"[🔌 Final URI]: {uri}")
        return uri
    '''

class Topic(models.Model):
    """
    Represents a logical grouping based on a project and service identifier.
    """
    name = models.CharField(max_length=100)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    user = models.ForeignKey(User, related_name='topics', on_delete=models.CASCADE)
    db_connection = models.ForeignKey(
        DatabaseConnection, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='topics'
    )
    chat_date = models.DateTimeField(auto_now_add=True)
    files = models.ManyToManyField(File, blank=True, related_name='linked_topics')

    def __str__(self):
        return f"{self.name} - Project: {self.project_id}, Service: {self.service_id}"

    @property
    def has_data_source(self):
        return self.files.exists() or self.db_connection is not None


class Query(models.Model):
    """
    Represents a query-response pair under a topic.
    """
    topic = models.ForeignKey(Topic, related_name='queries', on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    file = models.ForeignKey(File, related_name='queries', on_delete=models.SET_NULL, null=True, blank=True)
    query_text = models.TextField()
    response_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Query by {self.user.email if self.user else 'Unknown'} - {self.query_text[:30]}"


