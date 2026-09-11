from django.apps import AppConfig

class TrackingConfig(AppConfig):
    name = 'tracking'
    def ready(self):
        from . import signals
