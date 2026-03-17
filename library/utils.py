from datetime import timedelta

from django.utils import timezone
from django.conf import settings


def default_loan_expiry_date():
    return timezone.now().date() + timedelta(days=settings.DEFAULT_DUE_DURATION)
