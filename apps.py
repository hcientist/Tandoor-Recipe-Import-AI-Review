from django.apps import AppConfig


# Tandoor loads the plugin class as dir(module)[1], so the class name must sort right after "AppConfig".
class BookmarkletAiFirstPassConfig(AppConfig):
    name = 'recipes.plugins.ai_first_pass'
    verbose_name = 'AI first pass for bookmarklet imports'
    VERSION = '0.1.0'
    base_url = 'plugin/ai-first-pass/'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from . import patch
        patch.install()
