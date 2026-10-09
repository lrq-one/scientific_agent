# Cancellation consistency

`tests/test_p1_langgraph_integration.py:517` validates cancellation against the isolated PostgreSQL conversation repository. A waiting task is cancelled immediately; a running task transitions `running → cancelling → cancelled`; both reject `finish_task_with_answer` after cancellation. At least two persisted `CANCELLED` events remain in the conversation record.

This test validates the repository/status boundary. It does not claim that an arbitrary external database driver can interrupt an already-issued SQL statement. The runtime must still report that boundary accurately, and no event deletion is used to simulate cancellation.
