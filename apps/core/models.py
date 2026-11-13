from django.db import models


class Product(models.Model):
    """A product folder created on disk (see ``file_service.create_product_folder``)."""

    folder_name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.folder_name


class PriceQuote(models.Model):
    """Persisted result of a price-range calculation."""

    qty_string = models.CharField(max_length=255)
    cost = models.FloatField()
    profit_max = models.FloatField()
    profit_min = models.FloatField()
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.qty_string} @ {self.cost}"


class CommandJob(models.Model):
    """Async job record for long-running / desktop commands."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    command = models.CharField(max_length=255)
    params = models.JSONField(default=dict)
    kind = models.CharField(max_length=16, default="sync")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    result = models.JSONField(default=dict, blank=True)
    log = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.command} ({self.status})"


class NodeServiceAccount(models.Model):
    """Credentials the Python server uses to obtain a JWT from the Node API.

    The Node server owns authentication.  This row stores the email/password of a
    Node user whose role is admin / system-admin / developer; they are exchanged
    for a JWT via ``POST /trade_business/panel/auth/getToken``, which is cached in
    ``token`` until ``token_expires_at``.
    """

    name = models.CharField(max_length=100, unique=True)
    email = models.EmailField()
    password = models.CharField(max_length=255)  # replayable: sent to Node verbatim
    role = models.CharField(max_length=50, blank=True)  # informational, set on login
    token = models.TextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.email})"
