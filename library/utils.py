from datetime import timedelta

from django.utils import timezone
from django.conf import settings


def today():
    return timezone.now().date()


def default_loan_expiry_date():
    return today() + timedelta(days=settings.DEFAULT_DUE_DURATION)
