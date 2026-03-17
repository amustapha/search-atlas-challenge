from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.conf import settings
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from library.models import Author, Book, Member, Loan
from library.tasks import check_overdue_loans, send_loan_notification
from library.serializers import LoanExtensionSerializer
from library.utils import default_loan_expiry_date, today


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_author(first="Test", last="Author"):
    return Author.objects.create(first_name=first, last_name=last)


def make_book(author, isbn="1234567890123", available_copies=2):
    return Book.objects.create(
        title="Test Book",
        author=author,
        isbn=isbn,
        genre="fiction",
        available_copies=available_copies,
    )


def make_user(username="alice", email="alice@example.com"):
    return User.objects.create_user(username=username, email=email, password="pass")


def make_member(user):
    return Member.objects.create(user=user)


def make_loan(book, member, days_offset=7, is_returned=False):
    """Create a loan whose due_date is offset from today."""
    loan = Loan.objects.create(book=book, member=member, is_returned=is_returned)
    loan.due_date = today() + timedelta(days=days_offset)
    loan.save()
    return loan


# ---------------------------------------------------------------------------
# Model: Loan.is_overdue
# ---------------------------------------------------------------------------


DUE_DATE = date(2026, 3, 1)


class LoanIsOverdueTests(TestCase):
    def setUp(self):
        author = make_author()
        book = make_book(author)
        user = make_user()
        member = make_member(user)
        self.loan = make_loan(book, member, days_offset=0)
        self.loan.due_date = DUE_DATE
        self.loan.save()

    def test_is_overdue_when_today_is_past_due_date(self):
        with patch("library.models.today", return_value=date(2026, 3, 2)):
            self.assertTrue(self.loan.is_overdue)

    def test_not_overdue_when_today_equals_due_date(self):
        with patch("library.models.today", return_value=date(2026, 3, 1)):
            self.assertFalse(self.loan.is_overdue)

    def test_not_overdue_when_today_is_before_due_date(self):
        with patch("library.models.today", return_value=date(2026, 2, 28)):
            self.assertFalse(self.loan.is_overdue)


# ---------------------------------------------------------------------------
# Model: Loan.extend_due_date
# ---------------------------------------------------------------------------


class LoanExtendDueDateTests(TestCase):
    def setUp(self):
        author = make_author()
        book = make_book(author)
        user = make_user()
        member = make_member(user)
        self.loan = make_loan(book, member, days_offset=7)
        self.original_due = self.loan.due_date

    def test_extends_due_date_by_given_days(self):
        self.loan.extend_due_date(5)
        self.assertEqual(self.loan.due_date, self.original_due + timedelta(days=5))

    def test_extension_is_persisted_to_database(self):
        self.loan.extend_due_date(5)
        refreshed = Loan.objects.get(pk=self.loan.pk)
        self.assertEqual(refreshed.due_date, self.original_due + timedelta(days=5))

    def test_extend_due_date_returns_loan_instance(self):
        result = self.loan.extend_due_date(5)
        self.assertEqual(result, self.loan)


# ---------------------------------------------------------------------------
# Utils: default_loan_expiry_date
# ---------------------------------------------------------------------------


class DefaultLoanExpiryDateTests(TestCase):
    def test_returns_today_plus_configured_duration(self):
        expected = today() + timedelta(days=settings.DEFAULT_DUE_DURATION)
        self.assertEqual(default_loan_expiry_date(), expected)


# ---------------------------------------------------------------------------
# Celery task: check_overdue_loans
# ---------------------------------------------------------------------------


class CheckOverdueLoansTests(TestCase):
    # Loans are due on March 1; tests patch "today" to a fixed date.
    OVERDUE_TODAY = date(2026, 3, 2)  # day after due date → overdue
    NOT_OVERDUE_TODAY = date(2026, 2, 28)  # day before due date → not overdue

    def setUp(self):
        author = make_author()
        user1 = make_user(username="alice", email="alice@example.com")
        user2 = make_user(username="bob", email="bob@example.com")
        self.member1 = make_member(user1)
        self.member2 = make_member(user2)
        self.book1 = make_book(author, isbn="1111111111111")
        self.book2 = make_book(author, isbn="2222222222222")
        self.loan1 = make_loan(self.book1, self.member1, days_offset=0)
        self.loan2 = make_loan(self.book2, self.member2, days_offset=0)
        self.loan1.due_date = DUE_DATE
        self.loan1.save()
        self.loan2.due_date = DUE_DATE
        self.loan2.save()

    @patch("library.tasks.send_mail")
    def test_sends_email_for_each_overdue_loan(self, mock_send_mail):
        with patch("library.tasks.today", return_value=self.OVERDUE_TODAY):
            check_overdue_loans()
        self.assertEqual(mock_send_mail.call_count, 2)

    @patch("library.tasks.send_mail")
    def test_does_not_email_when_loans_not_yet_overdue(self, mock_send_mail):
        with patch("library.tasks.today", return_value=self.NOT_OVERDUE_TODAY):
            check_overdue_loans()
        mock_send_mail.assert_not_called()

    @patch("library.tasks.send_mail")
    def test_does_not_email_for_returned_overdue_loan(self, mock_send_mail):
        self.loan1.is_returned = True
        self.loan1.save()
        with patch("library.tasks.today", return_value=self.OVERDUE_TODAY):
            check_overdue_loans()
        self.assertEqual(mock_send_mail.call_count, 1)

    @patch("library.tasks.send_mail")
    def test_sends_overdue_reminder_subject(self, mock_send_mail):
        self.loan2.is_returned = True
        self.loan2.save()
        with patch("library.tasks.today", return_value=self.OVERDUE_TODAY):
            check_overdue_loans()
        _, kwargs = mock_send_mail.call_args
        self.assertEqual(kwargs["subject"], "Reminder: Book Loan is Overdue")

    @patch("library.tasks.send_mail")
    def test_emails_correct_member(self, mock_send_mail):
        self.loan2.is_returned = True
        self.loan2.save()
        with patch("library.tasks.today", return_value=self.OVERDUE_TODAY):
            check_overdue_loans()
        _, kwargs = mock_send_mail.call_args
        self.assertIn("alice@example.com", kwargs["recipient_list"])

    def test_task_is_registered_as_shared_task(self):
        self.assertTrue(callable(check_overdue_loans))
        self.assertTrue(hasattr(check_overdue_loans, "delay"))


# ---------------------------------------------------------------------------
# View: LoanViewSet.extend_due_date
# ---------------------------------------------------------------------------


class LoanExtendDueDateViewTests(APITestCase):
    def setUp(self):
        author = make_author()
        book = make_book(author)
        user = make_user()
        member = make_member(user)
        self.active_loan = make_loan(book, member, days_offset=7)
        self.overdue_loan = make_loan(book, member, days_offset=-1)

    def test_extend_active_loan_returns_200(self):
        resp = self.client.post(
            f"/api/loans/{self.active_loan.pk}/extend_due_date/",
            {"additional_days": 5},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_extend_active_loan_updates_due_date(self):
        original_due = self.active_loan.due_date
        self.client.post(
            f"/api/loans/{self.active_loan.pk}/extend_due_date/",
            {"additional_days": 5},
        )
        self.active_loan.refresh_from_db()
        self.assertEqual(self.active_loan.due_date, original_due + timedelta(days=5))

    def test_extend_overdue_loan_returns_400(self):
        resp = self.client.post(
            f"/api/loans/{self.overdue_loan.pk}/extend_due_date/",
            {"additional_days": 5},
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# View: MemberViewSet.top_active
# ---------------------------------------------------------------------------


class TopActiveMembersViewTests(APITestCase):
    def setUp(self):
        author = make_author()
        self.members = []
        for i in range(6):
            user = make_user(username=f"user{i}", email=f"user{i}@example.com")
            member = make_member(user)
            self.members.append(member)
            book = make_book(author, isbn=f"100000000{i:04d}")
            # member i has i active loans
            for j in range(i):
                extra_book = make_book(author, isbn=f"200{i:02d}{j:04d}")
                make_loan(extra_book, member)

    def test_returns_200(self):
        resp = self.client.get("/api/members/top-active/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_returns_at_most_five_members(self):
        resp = self.client.get("/api/members/top-active/")
        self.assertLessEqual(len(resp.data), 5)

    def test_members_ordered_by_active_loans_descending(self):
        resp = self.client.get("/api/members/top-active/")
        counts = [m["active_loans"] for m in resp.data]
        self.assertEqual(counts, sorted(counts, reverse=True))
