from sqlalchemy import CheckConstraint, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.models.base import Base, CreatedAt, NullableJSONB, UUIDPk


class Task(UUIDPk, CreatedAt, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("task_type IN ('command','agent')", name="task_type"),
        # Agent runs need minutes; command tasks keep the S1 limit.
        CheckConstraint(
            "timeout_s >= 1 AND timeout_s <= CASE task_type WHEN 'agent' THEN 1800 ELSE 300 END",
            name="timeout_s",
        ),
        CheckConstraint("(task_type = 'command') = (command IS NOT NULL)", name="command"),
        CheckConstraint("task_type <> 'agent' OR instructions IS NOT NULL", name="instructions"),
        CheckConstraint("task_type = 'agent' OR test_command IS NULL", name="test_command"),
        CheckConstraint("memory_mb BETWEEN 64 AND 1024", name="memory_mb"),
        CheckConstraint("cpu_limit >= 0.1 AND cpu_limit <= 2.0", name="cpu_limit"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="command")
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    # command tasks: the argv to run. agent tasks: NULL (the agent decides).
    command: Mapped[list[str] | None] = mapped_column(NullableJSONB)
    # agent tasks: the prompt given to the agent.
    instructions: Mapped[str | None] = mapped_column(Text)
    # agent tasks: argv behind the run_tests tool (the model can't choose it).
    test_command: Mapped[list[str] | None] = mapped_column(NullableJSONB)
    files: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_limit: Mapped[float] = mapped_column(Float, nullable=False)
