import unittest
from unittest.mock import Mock, patch

from meal_management.database import Database, Session, retry_transaction
from meal_management.errors import DomainError


class TransactionTests(unittest.TestCase):
    def test_successful_transaction_commits_then_closes(self):
        connection = Mock()
        events = []
        connection.commit.side_effect = lambda: events.append("commit")
        connection.close.side_effect = lambda: events.append("close")
        database = Database(connection_factory=lambda: connection)
        with database.transaction() as session:
            self.assertIsInstance(session, Session)
            events.append("body")
        self.assertEqual(events, ["body", "commit", "close"])
        connection.start_transaction.assert_called_once_with(isolation_level="READ COMMITTED")
        connection.rollback.assert_not_called()

    def test_body_failure_rolls_back_and_propagates(self):
        connection = Mock()
        database = Database(connection_factory=lambda: connection)
        error = RuntimeError("intentional unit test failure")
        with self.assertRaises(RuntimeError) as caught:
            with database.transaction():
                raise error
        self.assertIs(caught.exception, error)
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_failed_commit_does_not_return_success(self):
        connection = Mock()
        connection.commit.side_effect = RuntimeError("intentional commit failure")
        database = Database(connection_factory=lambda: connection)
        with self.assertRaisesRegex(RuntimeError, "commit failure"):
            with database.transaction():
                pass
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_rollback_failure_preserves_original_error(self):
        connection = Mock()
        connection.rollback.side_effect = RuntimeError("rollback failed")
        database = Database(connection_factory=lambda: connection)
        with self.assertRaisesRegex(ValueError, "original error"):
            with database.transaction():
                raise ValueError("original error")
        connection.close.assert_called_once_with()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.session = Session(self.connection)

    def test_parameters_are_passed_separately_from_sql(self):
        malicious_value = "' OR 1=1; DROP TABLE employees; --"
        sql = "SELECT id FROM employees WHERE employee_code = %s"
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.session.one(sql, [malicious_value]))
        self.cursor.execute.assert_called_once_with(sql, (malicious_value,))
        self.cursor.close.assert_called_once_with()

    def test_failed_sql_always_closes_cursor(self):
        self.cursor.execute.side_effect = RuntimeError("intentional query failure")
        with self.assertRaises(RuntimeError):
            self.session.execute("UPDATE employees SET is_active = %s WHERE id = %s", (0, 7))
        self.cursor.close.assert_called_once_with()


class TransactionRetryTests(unittest.TestCase):
    def test_known_transaction_deadlock_is_retried(self):
        deadlock = RuntimeError("intentional transaction deadlock")
        deadlock.errno = 1213
        operation = Mock(side_effect=[deadlock, 73])
        decorated = retry_transaction(operation)
        with patch("meal_management.database.time.sleep"):
            self.assertEqual(decorated(), 73)
        self.assertEqual(operation.call_count, 2)

    def test_ambiguous_commit_failure_is_not_retried(self):
        lost_connection = RuntimeError("connection lost while committing")
        lost_connection.errno = 2013
        connection = Mock()
        connection.commit.side_effect = lost_connection
        factory = Mock(return_value=connection)
        database = Database(connection_factory=factory)
        calls = []

        @retry_transaction
        def operation():
            with database.transaction():
                calls.append("operation")
            return "approved"

        with self.assertRaises(RuntimeError) as caught:
            operation()
        self.assertIs(caught.exception, lost_connection)
        self.assertEqual(calls, ["operation"])
        factory.assert_called_once_with()
        connection.commit.assert_called_once_with()

    def test_repeated_deadlock_stops_after_three_attempts(self):
        deadlock = RuntimeError("intentional persistent deadlock")
        deadlock.errno = 1213
        operation = Mock(side_effect=deadlock)
        decorated = retry_transaction(operation)
        with patch("meal_management.database.time.sleep"):
            with self.assertRaises(DomainError) as caught:
                decorated()
        self.assertEqual(caught.exception.code, "TRANSACTION_RETRY_REQUIRED")
        self.assertEqual(operation.call_count, 3)


if __name__ == "__main__":
    unittest.main()
