"""Core domain layer (business logic) shared by the CLI and the web app.

Importing this package loads the environment (``.env``) via
:mod:`apps.core.config`, so any module that relies on ``os.getenv`` /
``config.get`` can assume the values are already available.
"""

from apps.core import config  # noqa: F401
