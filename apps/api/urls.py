from django.urls import path

from . import views

urlpatterns = [
    path("commands/", views.list_commands, name="command-list"),
    path("commands/<str:name>/", views.run_command, name="command-run"),
]
