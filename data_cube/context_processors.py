from django.conf import settings


def feature_flags(request):
    """Expose deployment feature flags to templates."""
    return {
        'halo_enabled': settings.HALO_ENABLED,
    }
